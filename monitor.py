import os
import re
import sys
import time
import argparse
from collections import deque
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
NUMBER_TIMEOUT = 3000   # ms ekstra å vente på at selve antallet rendres

# En sjekk regnes som "blind" når vi ikke fikk lest antallet. Vi varsler
# både ved sammenhengende blindhet og ved vedvarende blaffing, slik at en
# side som feiler annenhver gang ikke går under radaren.
BLIND_ALERT_AFTER = 3        # blinde sjekker på rad
BLIND_WINDOW = 20            # størrelse på glidende vindu
BLIND_WINDOW_THRESHOLD = 10  # blinde sjekker i vinduet som utløser varsel
BLIND_REALERT_EVERY = 90     # ~30 min ved 20s intervall

# Hvis lista er tom, men sidens HTML likevel er stor, har vi trolig ikke
# fått tak i produktene - da er "ingen billetter" ikke til å stole på.
LIST_LEN_SUSPICIOUS = 1500

# "0 billetter", "1 billett", "12 billetter". (?<!\d) hindrer at vi plukker
# halen av et større tall.
TICKET_RE = re.compile(r"(?<!\d)(\d+)\s*billett", re.IGNORECASE)
# "3 av 10 billetter" / "Kun 2 igjen av 10 billetter" -> det FØRSTE tallet
# er antallet ledige. Tillater noen få ord (uten sifre) mellom tallet og
# "av", slik at "2 igjen av 10" ikke leses som 10.
RANGE_RE = re.compile(r"(?<!\d)(\d+)\b[^\d]{0,15}?\b(?:av|/)\s*\d+\s*billett",
                      re.IGNORECASE)
# Et element som inneholder BARE et tall, f.eks. "3".
BARE_NUMBER_RE = re.compile(r"^\s*(\d+)\s*$")
# Eksplisitt "det finnes ingen". Brukes KUN i siste runde, når ingen kilde
# ga et tall - da kan den ikke maskere et ekte antall.
SOLD_OUT_RE = re.compile(r"utsolgt|ingen\s+billett|sold\s*out", re.IGNORECASE)
# Fraser der tallet IKKE er et antall ledige billetter.
NOISE_RES = (
    re.compile(r"\b(?:maks|maksimalt)\b\s*:?\s*\d+\s*billett\w*", re.IGNORECASE),
    re.compile(r"\d+\s*billett\w*\s*per\s+\w+", re.IGNORECASE),
)
# Tusenskille: "1 234" / "1.234" -> "1234", slik at vi ikke leser 234.
THOUSAND_SEP_RE = re.compile(r"(?<=\d)[\s .,](?=\d{3}(?!\d))")


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}: {message}", flush=True)


def diag(message):
    log(f"[DIAG] {message}")


def notify(title, message, priority="5", tags="soccer,rotating_light", attempts=3):
    """Sender push via ntfy, med noen få forsøk ved forbigående feil.

    Kaster hvis alle forsøk feiler - kallstedet MÅ da la være å avansere
    tilstanden, ellers går varselet tapt for godt.
    """
    # Title er en HTTP-header og må være ASCII; selve meldingen sendes som
    # UTF-8 og tåler æ/ø/å.
    safe_title = title.encode("ascii", "replace").decode("ascii")
    body = f"{message} {URL} ({datetime.now():%Y-%m-%d %H:%M:%S})"
    last_error = None
    for attempt in range(attempts):
        try:
            resp = requests.post(
                NTFY_URL,
                headers={"Title": safe_title, "Priority": priority, "Tags": tags},
                data=body.encode("utf-8"),
                timeout=30,
            )
            resp.raise_for_status()
            return
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise last_error


def clean(text):
    """Normaliserer en kildetekst før tolkning: fjerner tusenskille og
    fraser der tallet ikke betyr «ledige billetter»."""
    if not text:
        return ""
    text = THOUSAND_SEP_RE.sub("", text)
    for noise in NOISE_RES:
        text = noise.sub(" ", text)
    return text.strip()


def resolve_count(product):
    """Finner antall ledige billetter i ett produktkort.

    Returnerer (antall, kilde). `antall` er None når antallet ikke lar seg
    lese entydig - og None er noe helt ANNET enn 0. Behandler man dem likt,
    ser en ødelagt selektor ut som "ingen billetter" i loggen, og varselet
    uteblir uten at noe ser galt ut.

    Er en kilde flertydig (flere ulike tall foran "billett"), gir vi opp i
    stedet for å gjette - et feil tall låser varslingen permanent, mens
    None utløser blind-varsling.
    """
    sources = (
        (".resale-availability .resale-list-number", product.get("number")),
        (".resale-list-number", product.get("looseNumber")),
        (".resale-availability", product.get("availability")),
        ("kortets tekst", product.get("cardText")),
    )

    for source, raw in sources:
        text = clean(raw)
        if not text:
            continue
        m = RANGE_RE.search(text)
        if m:
            return int(m.group(1)), f"{source} (N av M)"
        values = {int(v) for v in TICKET_RE.findall(text)}
        if len(values) == 1:
            return values.pop(), source
        if len(values) > 1:
            return None, f"{source} (flertydig: {sorted(values)})"
        m = BARE_NUMBER_RE.match(text)
        if m:
            return int(m.group(1)), f"{source} (blankt tall)"

    # Siste runde. Vi kommer bare hit når INGEN kilde ga et tall, så et
    # "utsolgt" her kan ikke skygge for et ekte antall i en bredere kilde.
    for source, raw in sources:
        text = clean(raw)
        if text and SOLD_OUT_RE.search(text):
            return 0, f"{source} (utsolgt)"

    return None, "ingen treff"


def product_key(product):
    return f"{product['name']}|{product['venue']}"


def scrape(page):
    """Laster resale-siden i en ekte nettleser, venter til lista er rendret
    av JavaScript, og returnerer (produkter, sidetilstand).

    Siden er JavaScript-rendret: rå-HTML inneholder bare en «Laster opp»-
    spinner og Mustache-maler, så antallet finnes ikke før JS har kjørt.
    """
    page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

    page.wait_for_selector(
        "#list_all_tickets .product, #notification_no_ticket_on_sales:not(.hidden)",
        timeout=PAGE_TIMEOUT,
    )

    # Antallet kan komme i et eget AJAX-kall etter at kortet er tegnet. Vent
    # kort på at selve ANTALL-elementet har et siffer - ikke bare at kortet
    # har det, for da teller dato og pris med og vakten blir virkningsløs.
    try:
        page.wait_for_function(
            "() => {"
            " const c = document.querySelectorAll('#list_all_tickets .product');"
            " if (c.length === 0) return true;"
            " return Array.from(c).every(e => {"
            "  const n = e.querySelector('.resale-availability .resale-list-number, .resale-list-number');"
            "  return n !== null && /\\d/.test(n.textContent || '');"
            " }); }",
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


def format_increases(increases):
    parts = []
    for product, before, now in increases:
        loc = f" @ {product['venue']}" if product["venue"] else ""
        parts.append(f"{product['name']}{loc}: {before} -> {now} billett(er)")
    return "; ".join(parts)


def new_state():
    return {
        "last_counts": {},
        "blind_streak": 0,
        "blind_window": deque(maxlen=BLIND_WINDOW),
        "blind_alerted": False,
        "checks_since_blind_alert": 0,
    }


def register_check(state, blind, reason):
    """Fører blind-regnskapet og varsler når overvåkingen har vært blind
    lenge nok. Kalles for HVER sjekk - også de som kastet unntak, ellers
    ville en strukturendring (som gir timeout, ikke uleselig tall) gå helt
    stille forbi.
    """
    state["blind_window"].append(bool(blind))
    state["checks_since_blind_alert"] += 1

    if blind:
        state["blind_streak"] += 1
    else:
        state["blind_streak"] = 0

    window_blind = sum(state["blind_window"]) >= BLIND_WINDOW_THRESHOLD
    streak_blind = state["blind_streak"] >= BLIND_ALERT_AFTER

    # Frisk igjen: både sammenhengende og glidende mål må være rene.
    if not blind and not window_blind:
        if state["blind_alerted"]:
            log("Antallet er leselig igjen - overvåkingen er tilbake i normal drift")
            try:
                notify(
                    "NFF Resale - varsling virker igjen",
                    "Overvåkingen leser billettantallet som normalt igjen.",
                    priority="2",
                    tags="white_check_mark",
                )
            except Exception as e:
                log(f"Friskmelding feilet - {type(e).__name__}: {e}")
            state["blind_alerted"] = False
        return

    if not (streak_blind or window_blind):
        return

    first_time = not state["blind_alerted"]
    if not (first_time or state["checks_since_blind_alert"] >= BLIND_REALERT_EVERY):
        return

    blinde_i_vindu = sum(state["blind_window"])
    try:
        notify(
            "NFF Resale - VARSLING NEDE",
            f"Overvåkingen er blind: {reason}. "
            f"{state['blind_streak']} sjekker på rad, "
            f"{blinde_i_vindu} av de siste {len(state['blind_window'])}. "
            f"Se [DIAG] i Railway-loggen.",
            priority="4",
            tags="warning",
        )
        # Teller nullstilles KUN ved levert varsel, ellers ville en enkelt
        # ntfy-feil gi 30 minutters ekstra stillhet.
        state["blind_alerted"] = True
        state["checks_since_blind_alert"] = 0
        log("ntfy: blind-varsel sendt")
    except Exception as e:
        log(f"Blind-varsel feilet - {type(e).__name__}: {e} (prøver igjen neste sjekk)")


def run_check(page, debug, state):
    products, page_state = scrape(page)

    resolved = [(p, *resolve_count(p)) for p in products]
    unreadable = [r for r in resolved if r[1] is None]
    total = sum(c for _p, c, _s in resolved if c is not None)

    # Tom liste er bare troverdig når siden selv sier at det ikke er noe til
    # salgs OG lista faktisk er tom. Er HTML-en stor mens vi ikke finner
    # produkter, har strukturen endret seg.
    list_len = page_state.get("listLen", 0)
    no_products_anomaly = not products and (
        not page_state.get("noTicketBanner") or list_len > LIST_LEN_SUSPICIOUS
    )
    in_queue = bool(page_state.get("queue"))
    blind = bool(unreadable) or no_products_anomaly or in_queue

    if in_queue:
        reason = "siden viser venterom/kø"
    elif no_products_anomaly:
        reason = f"fant ingen arrangementer (listLen={list_len})"
    elif unreadable:
        reason = f"klarer ikke lese antallet for {len(unreadable)} arrangement(er)"
    else:
        reason = ""

    status = f"Sjekk - {len(products)} produkt(er), {total} ledige billetter"
    if unreadable:
        status += f", {len(unreadable)} med ULESELIG antall"
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

    register_check(state, blind, reason)

    # --- Varsle om ledige billetter, per arrangement ---
    # En ren sum ville skjult at ett arrangement stiger mens et annet
    # synker; da kan ekte nye billetter forsvinne i støyen.
    prev_counts = state["last_counts"]
    new_counts = {}
    increases = []
    for product, count, _source in resolved:
        key = product_key(product)
        if count is None:
            # Uleselig nå: behold sist kjente verdi, slik at et blaff ikke
            # ser ut som 0 og gir falskt varsel når det blir leselig igjen.
            if key in prev_counts:
                new_counts[key] = prev_counts[key]
            continue
        new_counts[key] = count
        before = prev_counts.get(key, 0)
        if count > before:
            increases.append((product, before, count))

    if increases:
        gained = sum(now - before for _p, before, now in increases)
        message = (
            f"LEDIGE resale-billetter! {gained} ny(e) billett(er): "
            f"{format_increases(increases)}."
        )
        try:
            notify("NFF Resale - Ledige billetter!", message)
            log("ntfy: billettvarsel sendt")
            state["last_counts"] = new_counts
        except Exception as e:
            # Ikke avanser tilstanden - da ville varselet vært tapt for
            # godt. Neste sjekk ser samme økning og prøver på nytt.
            log(f"Billettvarsel feilet - {type(e).__name__}: {e} (prøver igjen neste sjekk)")
    else:
        state["last_counts"] = new_counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true",
                        help="logg [DIAG] for hver sjekk, ikke bare ved feil")
    parser.add_argument("-i", "--interval", type=int, default=POLL_INTERVAL,
                        help="sekunder mellom hver sjekk")
    parser.add_argument("--once", action="store_true",
                        help="kjør én sjekk og avslutt (exit 1 ved feil)")
    parser.add_argument("--test-notify", action="store_true",
                        help="send en testmelding til ntfy og avslutt")
    args = parser.parse_args()

    if args.test_notify:
        try:
            notify("NFF Resale - test", "Testmelding fra monitor.py.",
                   priority="3", tags="white_check_mark")
            log("ntfy: testmelding sendt OK")
            return 0
        except Exception as e:
            log(f"Testmelding FEILET - {type(e).__name__}: {e}")
            return 1

    launch_kwargs = {"headless": True}
    if CHROMIUM_PATH:
        launch_kwargs["executable_path"] = CHROMIUM_PATH

    state = new_state()
    log(f"Starter overvåking av {URL} (intervall {args.interval}s)")

    while True:
        started = time.monotonic()
        ok = True
        try:
            # Start og stopp nettleseren per sjekk for å holde minnebruken nede.
            with sync_playwright() as pw:
                browser = pw.chromium.launch(**launch_kwargs)
                context = browser.new_context(user_agent="Mozilla/5.0")
                page = context.new_page()
                try:
                    run_check(page, args.debug, state)
                except Exception:
                    # Logg hva nettleseren faktisk ser FØR den lukkes.
                    log_page_diagnostics(page)
                    raise
                finally:
                    # En feilende close() skal ikke erstatte den egentlige
                    # feilen i loggen.
                    try:
                        browser.close()
                    except Exception as e:
                        log(f"browser.close() feilet - {type(e).__name__}: {e}")
        except Exception as e:
            ok = False
            log(f"Error - {type(e).__name__}: {e}")
            # En strukturendring gir timeout her, ikke et uleselig tall.
            # Uten dette ville den vanligste bruddformen gått helt stille.
            register_check(state, True, f"sjekken feilet ({type(e).__name__})")

        if args.once:
            return 0 if ok else 1

        # Trekk fra tiden sjekken tok, ellers blir perioden 25-30s i praksis.
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
