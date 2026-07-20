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
POLL_INTERVAL = 60      # sekunder mellom hver sjekk
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

with sync_playwright() as pw:
    browser = pw.chromium.launch(**launch_kwargs)
    context = browser.new_context(user_agent="Mozilla/5.0")
    page = context.new_page()
    print(f"{datetime.now()}: Chromium startet, begynner å sjekke...", flush=True)

    while True:
        try:
            products = scrape_products(page, debug)
            total = total_available(products)
            breakdown = format_breakdown(products)

            if last_total is None:
                print(f"{datetime.now()}: Initial load - {total} ledige billetter ({breakdown})")
            elif total != last_total:
                print(f"{datetime.now()}: ENDRING: {last_total} -> {total} billetter ({breakdown})")
                if total > last_total:
                    message = f"LEDIGE resale-billetter til Ullevål! Nå {total} billett(er) tilgjengelig: {breakdown}."
                elif total == 0:
                    message = "Resale-billetter til Ullevål er utsolgt igjen (0 billetter)."
                else:
                    message = f"Antall resale-billetter endret seg til {total}: {breakdown}."
                try:
                    send_notification(message)
                    print(f"{datetime.now()}: ntfy notification sent")
                except Exception as e:
                    print(f"{datetime.now()}: Notification failed - {e}")
            else:
                print(f"{datetime.now()}: Ingen endring - {total} billett(er)")

            last_total = total
        except Exception as e:
            print(f"{datetime.now()}: Error - {e}")

        time.sleep(interval)
