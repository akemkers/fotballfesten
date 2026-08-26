import os
import time
import argparse
import re
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

# Valgfri override for Chromium-binæren (nyttig i miljøer der Playwright
# ikke finner nettleseren selv). På egen maskin: kjør `playwright install
# chromium` én gang, så trengs ikke denne.
CHROMIUM_PATH = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")

URL = "https://resale.fotball.no/list/resaleProducts/?lang=no"
NTFY_URL = "https://ntfy.sh/nff-resale-billetter"
POLL_INTERVAL = 20      # sekunder mellom hver sjekk
PAGE_TIMEOUT = 30000    # ms å vente på at lista rendres

# Matcher "0 billetter", "1 billett", "12 billetter" osv.
TICKET_RE = re.compile(r"(\d+)\s*billett", re.IGNORECASE)


def debug_log(message, enabled):
    if enabled:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [DEBUG] {message}")


def send_notification(message):
    headers = {
        "Title": "NFF Resale - Ledige billetter!",
        "Priority": "5",
        "Tags": "soccer,rotating_light",
    }
    body = f"{message} {URL} ({datetime.now():%Y-%m-%d %H:%M:%S})"
    requests.post(NTFY_URL, headers=headers, data=body.encode("utf-8"), timeout=30)


def scrape_products(page, debug):
    """Laster resale-siden i en ekte nettleser, venter til lista er rendret
    av JavaScript, og returnerer en liste med (navn, sted, antall billetter).

    Siden er JavaScript-rendret: rå-HTML inneholder bare en «Laster opp»-
    spinner og Mustache-maler, så antallet finnes ikke før JS har kjørt.
    """
    page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

    # Vent til enten produktkort ELLER "ingen billetter"-beskjeden er synlig,
    # slik at vi vet at JS har rendret ferdig.
    page.wait_for_selector(
        "#list_all_tickets .product, #notification_no_ticket_on_sales:not(.hidden)",
        timeout=PAGE_TIMEOUT,
    )

    products = page.eval_on_selector_all(
        "#list_all_tickets .product",
        """els => els.map(el => ({
            name: (el.querySelector('.resale-list-name')?.textContent || '').trim(),
            venue: (el.querySelector('.resale-list-venue')?.textContent || '').trim(),
            number: (el.querySelector('.resale-availability .resale-list-number')?.textContent || '').trim()
        }))""",
    )

    results = []
    for p in products:
        m = TICKET_RE.search(p["number"])
        count = int(m.group(1)) if m else 0
        results.append((p["name"], p["venue"], count))

    debug_log(f"Rendret {len(results)} produkt(er): {results}", debug)
    return results


def log_page_diagnostics(page):
    """Rapporterer hva nettleseren faktisk ser når scraping feiler, slik at
    vi kan skille mellom venterom/kø, samtykke-vegg og strukturendring."""
    try:
        title = page.title()
    except Exception:
        title = "(ukjent)"
    try:
        info = page.evaluate(
            """() => ({
                products: document.querySelectorAll('#list_all_tickets .product').length,
                loaderVisible: !!document.querySelector('#list_all_tickets #loading_mark'),
                noTickets: !!document.querySelector('#notification_no_ticket_on_sales:not(.hidden)'),
                listLen: (document.querySelector('#list_all_tickets')?.innerHTML || '').length,
                queue: /waiting room|begrenset adgang|kjøpsvindu|venterom|queue/i
                    .test(document.body ? document.body.innerText : '')
            })"""
        )
    except Exception as e:
        info = f"(kunne ikke inspisere: {e})"
    print(f"{datetime.now()}: [DIAG] tittel='{title}' {info}", flush=True)


def total_available(products):
    return sum(count for _, _, count in products)


def format_breakdown(products):
    if not products:
        return "ingen arrangementer oppført"
    parts = []
    for name, venue, count in products:
        loc = f" @ {venue}" if venue else ""
        parts.append(f"{name}{loc}: {count} billett(er)")
    return "; ".join(parts)


parser = argparse.ArgumentParser()
parser.add_argument("-d", "--debug", action="store_true")
parser.add_argument("-i", "--interval", type=int, default=POLL_INTERVAL,
                    help="sekunder mellom hver sjekk")
args = parser.parse_args()
debug = args.debug
interval = args.interval

last_total = None

launch_kwargs = {"headless": True}
if CHROMIUM_PATH:
    launch_kwargs["executable_path"] = CHROMIUM_PATH

print(f"{datetime.now()}: Starter overvåking av {URL} (intervall {interval}s)", flush=True)

while True:
    try:
        # Start og stopp nettleseren per sjekk for å holde minnebruken nede.
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(user_agent="Mozilla/5.0")
            page = context.new_page()
            try:
                products = scrape_products(page, debug)
            except Exception:
                # Logg hva nettleseren faktisk ser FØR den lukkes.
                log_page_diagnostics(page)
                raise
            finally:
                browser.close()

        total = total_available(products)
        breakdown = format_breakdown(products)

        # Behandle "ny oppstart" (last_total is None) som at det var 0 fra før,
        # slik at billetter som ALLEREDE er tilgjengelige når tjenesten
        # (re)starter også utløser et varsel.
        prev = 0 if last_total is None else last_total
        became_available = total > 0 and prev == 0
        increased = prev > 0 and total > prev

        print(f"{datetime.now()}: Sjekk - {len(products)} produkt(er), {total} ledige billetter ({breakdown})", flush=True)

        message = None
        if became_available:
            message = f"LEDIGE resale-billetter til Ullevål! {total} billett(er) tilgjengelig: {breakdown}."
        elif increased:
            message = f"Flere resale-billetter til Ullevål: {prev} -> {total}: {breakdown}."

        if message:
            try:
                send_notification(message)
                print(f"{datetime.now()}: ntfy notification sent", flush=True)
            except Exception as e:
                print(f"{datetime.now()}: Notification failed - {e}", flush=True)

        last_total = total
    except Exception as e:
        print(f"{datetime.now()}: Error - {type(e).__name__}: {e}", flush=True)

    time.sleep(interval)
