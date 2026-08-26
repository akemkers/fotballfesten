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
NUMBER_TIMEOUT = 5000   # ms ekstra å vente på at selve antallet rendres

# Hvor mange sjekker på rad med uleselig antall før vi varsler om at
# overvåkingen er blind, og hvor ofte varselet gjentas mens den er det.
BLIND_ALERT_AFTER = 3
BLIND_REALERT_EVERY = 90   # ~30 min ved 20s intervall

# Matcher "0 billetter", "1 billett", "12 billetter" osv.
TICKET_RE = re.compile(r"(\d+)\s*billett", re.IGNORECASE)
# Matcher et element som inneholder BARE et tall, f.eks. "3".
BARE_NUMBER_RE = re.compile(r"^\s*(\d+)\s*$")
# Eksplisitt "det finnes ingen" - trygt å tolke som 0.
SOLD_OUT_RE = re.compile(r"utsolgt|ingen\s+billett|sold\s*out", re.IGNORECASE)


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}: {message}", flush=True)


def diag(message):
    log(f"[DIAG] {message}")


def notify(title, message, priority="5", tags="soccer,rotating_light"):
    """Sender push via ntfy. Kaster ved HTTP-feil, slik at en avvist melding
    (429/5xx) ikke blir logget som vellykket."""
    body = f"{message} {URL} ({datetime.now():%Y-%m-%d %H:%M:%S})"
    resp = requests.post(
        NTFY_URL,
        headers={"Title": title, "Priority": priority, "Tags": tags},
        data=body.encode("utf-8"),
        timeout=30,
    )
    resp.raise_for_status()


def resolve_count(product):
    """Finner antall ledige billetter i ett produktkort.

    Returnerer (antall, kilde). `antall` er None når antallet ikke lar seg
    lese - og None er noe helt ANNET enn 0. Behandler man dem likt (slik
    koden gjorde før), ser en ødelagt selektor ut som "ingen billetter" i
    loggen, og varselet uteblir uten at noe ser galt ut.

    Kildene prøves fra smalest til bredest, slik at et dedikert antall-felt
    vinner over fritekst i kortet.
    """
    candidates = (
        (".resale-availability .resale-list-number", product.get("number")),
        (".resale-list-number", product.get("looseNumber")),
        (".resale-availability", product.get("availability")),
        ("kortets tekst", product.get("cardText")),
    )
    for source, text in candidates:
        if not text:
            continue
        m = TICKET_RE.search(text)
        if m:
            return int(m.group(1)), source
        m = BARE_NUMBER_RE.match(text)
        if m:
            return int(m.group(1)), f"{source} (blankt tall)"
        if SOLD_OUT_RE.search(text):
            return 0, f"{source} (utsolgt)"
    return None, "ingen treff"


def scrape(page):
    """Laster resale-siden i en ekte nettleser, venter til lista er rendret
    av JavaScript, og returnerer (produkter, sidetilstand).

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

    # Selve antallet kan komme i et eget AJAX-kall etter at kortet er tegnet.
    # Vent en kort stund på at hvert kort inneholder minst ett siffer, ellers
    # risikerer vi å lese et tomt felt og tolke det som 0. Best effort - vi
    # går videre uansett, og resolve_count skiller uansett tomt fra null.
    try:
        page.wait_for_function(
            "() => { const c = document.querySelectorAll('#list_all_tickets .product');"
            " return c.length === 0 || Array.from(c).every(e => /\\d/.test(e.innerText || '')); }",
            timeout=NUMBER_TIMEOUT,
        )
    except Exception:
        pass

    products = page.eval_on_selector_all(
        "#list_all_tickets .product",
        """els => els.map(el => {
            const t = sel => {
                const n = el.querySelector(sel);
                return n ? (n.textContent || '').trim() : null;
            };
            const flat = s => (s || '').replace(/\\s+/g, ' ').trim();
            return {
                name: t('.resale-list-name') || '',
                venue: t('.resale-list-venue') || '',
                number: t('.resale-availability .resale-list-number'),
                looseNumber: t('.resale-list-number'),
                availability: flat(t('.resale-availability')) || null,
                cardText: flat(el.innerText),
                cardHtml: flat(el.innerHTML).slice(0, 600)
            };
        })""",
    )

    state = page.evaluate(
        """() => ({
            noTicketBanner: !!document.querySelector('#notification_no_ticket_on_sales:not(.hidden)'),
            listLen: (document.querySelector('#list_all_tickets')?.innerHTML || '').length,
            queue: /waiting room|begrenset adgang|kjøpsvindu|venterom|queue/i
                .test(document.body ? document.body.innerText : '')
        })"""
    )
    return products, state


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
    diag(f"tittel='{title}' {info}")


def format_breakdown(resolved):
    if not resolved:
        return "ingen arrangementer oppført"
    parts = []
    for product, count, _source in resolved:
        loc = f" @ {product['venue']}" if product["venue"] else ""
        antall = "ULESELIG" if count is None else f"{count} billett(er)"
        parts.append(f"{product['name']}{loc}: {antall}")
    return "; ".join(parts)


def run_check(page, debug, state_holder):
    products, page_state = scrape(page)

    resolved = [(p, *resolve_count(p)) for p in products]
    unknown = [r for r in resolved if r[1] is None]
    total = sum(count for _p, count, _s in resolved if count is not None)

    # Null produkter er bare troverdig når siden selv sier at det ikke er noe
    # til salgs. Ellers har vi mest sannsynlig ikke fått tak i lista.
    no_products_anomaly = not products and not page_state.get("noTicketBanner")
    blind = bool(unknown) or no_products_anomaly

    status = f"Sjekk - {len(products)} produkt(er), {total} ledige billetter"
    if unknown:
        status += f", {len(unknown)} med ULESELIG antall"
    log(f"{status} ({format_breakdown(resolved)})")

    if blind or debug:
        diag(f"side={page_state}")
        for product, count, source in resolved:
            if count is None or debug:
                diag(
                    f"'{product['name']}' kilde={source} "
                    f"number={product['number']!r} loose={product['looseNumber']!r} "
                    f"avail={product['availability']!r} tekst={product['cardText']!r}"
                )
                if count is None:
                    diag(f"'{product['name']}' html={product['cardHtml']!r}")

    # --- Varsle når overvåkingen ikke klarer å lese antallet ---
    if blind:
        state_holder["unknown_streak"] += 1
        streak = state_holder["unknown_streak"]
        due = streak == BLIND_ALERT_AFTER or (
            streak > BLIND_ALERT_AFTER
            and (streak - BLIND_ALERT_AFTER) % BLIND_REALERT_EVERY == 0
        )
        if due:
            grunn = (
                "fant ingen arrangementer og siden sier ikke at det er tomt"
                if no_products_anomaly
                else f"klarer ikke lese billettantallet for {len(unknown)} arrangement(er)"
            )
            try:
                # NB: Title-headeren må være ASCII (HTTP-header), men selve
                # meldingen sendes som UTF-8 og tåler æ/ø/å.
                notify(
                    "NFF Resale - VARSLING NEDE",
                    f"Overvåkingen er blind: {grunn} ({streak} sjekker på rad). "
                    f"Siden har trolig endret struktur – se [DIAG] i Railway-loggen.",
                    priority="4",
                    tags="warning",
                )
                log("ntfy: blind-varsel sendt")
            except Exception as e:
                log(f"Blind-varsel feilet - {type(e).__name__}: {e}")
    else:
        if state_holder["unknown_streak"] >= BLIND_ALERT_AFTER:
            log("Antallet er leselig igjen - overvåkingen er tilbake i normal drift")
        state_holder["unknown_streak"] = 0

    # --- Varsle om ledige billetter ---
    # Behandle "ny oppstart" (last_total is None) som at det var 0 fra før,
    # slik at billetter som ALLEREDE er tilgjengelige når tjenesten
    # (re)starter også utløser et varsel.
    prev = 0 if state_holder["last_total"] is None else state_holder["last_total"]
    became_available = total > 0 and prev == 0
    increased = prev > 0 and total > prev

    message = None
    if became_available:
        message = (
            f"LEDIGE resale-billetter til Ullevål! {total} billett(er) tilgjengelig: "
            f"{format_breakdown(resolved)}."
        )
    elif increased:
        message = (
            f"Flere resale-billetter til Ullevål: {prev} -> {total}: "
            f"{format_breakdown(resolved)}."
        )

    if message:
        try:
            notify("NFF Resale - Ledige billetter!", message)
            log("ntfy: billettvarsel sendt")
        except Exception as e:
            log(f"Billettvarsel feilet - {type(e).__name__}: {e}")

    state_holder["last_total"] = total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true",
                        help="logg [DIAG] for hver sjekk, ikke bare ved feil")
    parser.add_argument("-i", "--interval", type=int, default=POLL_INTERVAL,
                        help="sekunder mellom hver sjekk")
    parser.add_argument("--once", action="store_true",
                        help="kjør én sjekk og avslutt (nyttig for testing)")
    parser.add_argument("--test-notify", action="store_true",
                        help="send en testmelding til ntfy og avslutt")
    args = parser.parse_args()

    if args.test_notify:
        try:
            notify("NFF Resale - test", "Testmelding fra monitor.py.",
                   priority="3", tags="white_check_mark")
            log("ntfy: testmelding sendt OK")
        except Exception as e:
            log(f"Testmelding FEILET - {type(e).__name__}: {e}")
        return

    launch_kwargs = {"headless": True}
    if CHROMIUM_PATH:
        launch_kwargs["executable_path"] = CHROMIUM_PATH

    state_holder = {"last_total": None, "unknown_streak": 0}

    log(f"Starter overvåking av {URL} (intervall {args.interval}s)")

    while True:
        try:
            # Start og stopp nettleseren per sjekk for å holde minnebruken nede.
            with sync_playwright() as pw:
                browser = pw.chromium.launch(**launch_kwargs)
                context = browser.new_context(user_agent="Mozilla/5.0")
                page = context.new_page()
                try:
                    run_check(page, args.debug, state_holder)
                except Exception:
                    # Logg hva nettleseren faktisk ser FØR den lukkes.
                    log_page_diagnostics(page)
                    raise
                finally:
                    browser.close()
        except Exception as e:
            log(f"Error - {type(e).__name__}: {e}")

        if args.once:
            return

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
