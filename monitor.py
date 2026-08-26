import os
import re
import sys
import time
import signal
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
POLL_INTERVAL = 20       # sekunder mellom hver sjekk
PAGE_TIMEOUT = 30000     # ms å vente på at lista rendres
NUMBER_TIMEOUT = 2000    # ms ekstra å vente på at selve antallet rendres
EMPTY_GRACE = 1500       # ms å vente på produkter før "tom liste" godtas
CHECK_WATCHDOG = 120     # sekunder før en hengende sjekk avbrytes

# En sjekk regnes som "blind" når vi ikke fikk lest antallet. Vi varsler
# både ved sammenhengende blindhet og ved vedvarende blaffing, slik at en
# side som feiler annenhver gang ikke går under radaren.
BLIND_ALERT_AFTER = 3         # blinde sjekker på rad
BLIND_WINDOW = 20             # størrelse på glidende vindu
BLIND_WINDOW_THRESHOLD = 10   # blinde sjekker i vinduet som utløser varsel
BLIND_REALERT_EVERY = 90      # ~30 min ved 20s intervall
RECOVERY_AFTER_GOOD = 10      # gode sjekker på rad før friskmelding

# Etter en blind periode er baselinen ukjent. Da varsler vi på ethvert
# positivt antall (heller ett unødvendig varsel enn ett tapt), men demper
# gjentakelser av nøyaktig samme antall innenfor dette vinduet.
REPEAT_ALERT_COOLDOWN = 300   # sekunder

# "0 billetter", "1 billett", "12 billetter". (?<!\d) hindrer at vi plukker
# halen av et større tall.
TICKET_RE = re.compile(r"(?<!\d)(\d+)\s*billett", re.IGNORECASE)
# "3 av 10 billetter" / "Kun 2 igjen av 10 billetter" -> første tall er
# antallet ledige. Mellomrommet tillater noen få ord, men ikke sifre,
# linjeskift eller setningstegn - ellers plukker den tall fra forrige
# setning ("Sete 9 - resten av 40 billetter").
RANGE_RE = re.compile(r"(?<!\d)(\d+)\b[A-Za-zÀ-ÿ ]{0,12}?\b(?:av|/)\s*\d+\s*billett",
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
# "av 12 billetter" uten teller foran: 12 er totalen, ikke antall ledige.
DENOMINATOR_RE = re.compile(r"\b(?:av|/)\s*$", re.IGNORECASE)
# Tusenskille: "1 234" / "1.234" -> "1234", slik at vi ikke leser 234.
THOUSAND_SEP_RE = re.compile(r"(?<=\d)[\s .,](?=\d{3}(?!\d))")

HAS_ALARM = hasattr(signal, "SIGALRM")


class CheckTimeout(Exception):
    """Sjekken brukte lengre tid enn CHECK_WATCHDOG."""


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}: {message}", flush=True)


def diag(message):
    log(f"[DIAG] {message}")


def notify(title, message, priority="5", tags="soccer,rotating_light",
           attempts=3, timeout=30):
    """Sender push via ntfy, med noen få forsøk ved forbigående feil.

    Kaster hvis alle forsøk feiler - kallstedet MÅ da la være å avansere
    tilstanden, ellers går varselet tapt for godt.

    Diagnosevarsler bør kalles med attempts=1 og kort timeout: de er ikke
    verdt å blokkere et billettvarsel for.
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
                timeout=timeout,
            )
            resp.raise_for_status()
            return
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
    raise last_error


def notify_diagnostic(title, message, tags):
    """Diagnosevarsel: ett forsøk, kort timeout. Skal aldri stå i veien for
    et billettvarsel."""
    notify(title, message, priority="3", tags=tags, attempts=1, timeout=10)


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
        values = set()
        saw_denominator = False
        for hit in TICKET_RE.finditer(text):
            # "... av 12 billetter" - tallet er totalen, ikke det ledige.
            if DENOMINATOR_RE.search(text[:hit.start()]):
                saw_denominator = True
                continue
            values.add(int(hit.group(1)))
        if len(values) == 1:
            return values.pop(), source
        if len(values) > 1:
            return None, f"{source} (flertydig: {sorted(values)})"
        if saw_denominator:
            return None, f"{source} (kun total, ingen teller)"
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
    """Nøkkel for å spore ett arrangement over tid.

    Returnerer None når navnet mangler. Da kan vi ikke skille kortene fra
    hverandre, og alle ville kollapset til samme nøkkel - det ville skjult
    ekte økninger helt lydløst. Uten navn regnes kortet som uleselig.
    """
    name = (product.get("name") or "").strip()
    if not name:
        return None
    return f"{name}|{(product.get('venue') or '').strip()}"


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

    # "Ingen billetter"-banneret kan være rendret fra serveren og skjules av
    # JS først etter at dataene er lastet. Uten denne nådetiden ville vi
    # godta en tom liste som sannhet og logge rolige nuller i det uendelige.
    try:
        page.wait_for_selector("#list_all_tickets .product", timeout=EMPTY_GRACE)
    except Exception:
        pass

    # Antallet kan komme i et eget AJAX-kall etter at kortet er tegnet. Vent
    # kort på at ANTALL-elementet har et siffer - ikke bare at kortet har
    # det, for da teller dato og pris med og vakten blir virkningsløs.
    # `some` framfor `every`: ett utsolgt kort uten antall-element skal ikke
    # koste full timeout ved hver eneste sjekk.
    try:
        page.wait_for_function(
            "() => {"
            " const c = document.querySelectorAll('#list_all_tickets .product');"
            " if (c.length === 0) return true;"
            " return Array.from(c).some(e => {"
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
        """() => {
            const list = document.querySelector('#list_all_tickets');
            const html = list ? list.innerHTML : '';
            const body = document.body ? document.body.innerText : '';
            return {
                noTicketBanner: !!document.querySelector('#notification_no_ticket_on_sales:not(.hidden)'),
                listLen: html.length,
                // Strukturelt signal: sier siden "tomt" mens lista likevel
                // inneholder produkt-markup, har vi ikke fatt tak i kortene.
                productMarkup: /class="[^"]*\\bproduct\\b/.test(html),
                queue: /waiting\\s*room|venterom|queue-it|du er i k(?:\\u00f8|o)|plass i k(?:\\u00f8|o)en/i
                    .test(body)
            };
        }"""
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
                names: Array.from(document.querySelectorAll('#list_all_tickets .resale-list-name')).length
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
        navn = (product["name"] or "").strip() or "(uten navn)"
        loc = f" @ {product['venue']}" if product["venue"] else ""
        antall = "ULESELIG" if count is None else f"{count} billett(er)"
        parts.append(f"{navn}{loc}: {antall}")
    return "; ".join(parts)


def format_increases(increases):
    parts = []
    for product, before, now in increases:
        navn = (product["name"] or "").strip() or "(uten navn)"
        loc = f" @ {product['venue']}" if product["venue"] else ""
        forrige = "ukjent" if before is None else str(before)
        parts.append(f"{navn}{loc}: {forrige} -> {now} billett(er)")
    return "; ".join(parts)


def new_state():
    return {
        # nøkkel -> sist sikkert observerte antall, eller None = ukjent
        # (kortet var uleselig, så vi vet ikke hva som skjedde imens)
        "baselines": {},
        # nøkkel -> (antall, tidspunkt) for sist SENDTE varsel
        "alerted": {},
        "blind_streak": 0,
        "good_streak": 0,
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
        state["good_streak"] = 0
    else:
        state["blind_streak"] = 0
        state["good_streak"] += 1

    window_blind = sum(state["blind_window"]) >= BLIND_WINDOW_THRESHOLD
    streak_blind = state["blind_streak"] >= BLIND_ALERT_AFTER

    # Friskmelding krever en solid serie gode sjekker. Uten det ville en
    # side som blaffer sendt NEDE og friskmelding om hverandre i det
    # uendelige, og push-spam ender med at topicen dempes - da er også det
    # ekte billettvarselet borte.
    if not blind and not window_blind and state["good_streak"] >= RECOVERY_AFTER_GOOD:
        if state["blind_alerted"]:
            log("Antallet er leselig igjen - overvåkingen er tilbake i normal drift")
            try:
                notify_diagnostic(
                    "NFF Resale - varsling virker igjen",
                    "Overvåkingen leser billettantallet som normalt igjen.",
                    "white_check_mark",
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
    if not reason:
        reason = f"{blinde_i_vindu} av de siste {len(state['blind_window'])} sjekkene var blinde"
    try:
        notify_diagnostic(
            "NFF Resale - VARSLING NEDE",
            f"Overvåkingen er blind: {reason}. "
            f"{state['blind_streak']} sjekker på rad, "
            f"{blinde_i_vindu} av de siste {len(state['blind_window'])}. "
            f"Se [DIAG] i Railway-loggen.",
            "warning",
        )
        # Teller nullstilles KUN ved levert varsel, ellers ville en enkelt
        # ntfy-feil gi 30 minutters ekstra stillhet.
        state["blind_alerted"] = True
        state["checks_since_blind_alert"] = 0
        log("ntfy: blind-varsel sendt")
    except Exception as e:
        log(f"Blind-varsel feilet - {type(e).__name__}: {e} (prøver igjen neste sjekk)")


def collect_counts(resolved):
    """Slår sammen produktkort til {nøkkel: antall}, og rapporterer hvilke
    kort som ikke lot seg lese.

    Kort med samme nøkkel summeres. Ville vi latt det siste kortet
    overskrive det første, kunne en økning på det første blitt usynlig -
    og i motsatt rekkefølge ville baselinen blitt nullstilt hver sjekk og
    gitt varsel i det uendelige.
    """
    counts = {}
    unreadable_keys = set()
    unreadable_cards = []
    for product, count, _source in resolved:
        key = product_key(product)
        if key is None or count is None:
            unreadable_cards.append(product)
            if key is not None:
                unreadable_keys.add(key)
            continue
        counts[key] = counts.get(key, 0) + count
    return counts, unreadable_keys, unreadable_cards


def find_increases(state, counts, unreadable_keys, now_ts):
    """Finner arrangementer som har fått flere billetter siden sist.

    Er baselinen ukjent (kortet var uleselig forrige gang), varsler vi på
    ethvert positivt antall. Vi vet ikke om antallet falt til null imens,
    og et unødvendig varsel er langt billigere enn et tapt. Gjentakelser av
    nøyaktig samme antall dempes innenfor REPEAT_ALERT_COOLDOWN.
    """
    increases = []
    for key, count in counts.items():
        if key in unreadable_keys:
            continue
        baseline = state["baselines"].get(key, 0)
        if baseline is None:
            if count <= 0:
                continue
            previous = state["alerted"].get(key)
            if (previous and previous[0] == count
                    and now_ts - previous[1] < REPEAT_ALERT_COOLDOWN):
                continue
        elif count <= baseline:
            continue
        increases.append((key, baseline, count))
    return increases


def run_check(page, debug, state, now_ts=None):
    """Kjører én sjekk. Returnerer True hvis sjekken var blind."""
    now_ts = time.monotonic() if now_ts is None else now_ts
    products, page_state = scrape(page)

    resolved = [(p, *resolve_count(p)) for p in products]
    counts, unreadable_keys, unreadable_cards = collect_counts(resolved)
    total = sum(counts.values())

    # Tom liste er bare troverdig når siden selv sier at det ikke er noe til
    # salgs OG lista faktisk ikke inneholder produkt-markup. Finner vi
    # markup uten å få ut kort, har strukturen endret seg.
    no_products_anomaly = not products and (
        not page_state.get("noTicketBanner") or page_state.get("productMarkup")
    )
    in_queue = bool(page_state.get("queue"))
    blind = bool(unreadable_cards) or no_products_anomaly or in_queue

    if in_queue:
        reason = "siden viser venterom/kø"
    elif no_products_anomaly:
        reason = (f"fant ingen arrangementer (listLen={page_state.get('listLen')}, "
                  f"produktmarkup={page_state.get('productMarkup')})")
    elif unreadable_cards:
        navnloese = sum(1 for p in unreadable_cards if not (p.get("name") or "").strip())
        reason = f"klarer ikke lese {len(unreadable_cards)} kort"
        if navnloese:
            reason += f" ({navnloese} mangler navn - navneselektoren kan være brutt)"
    else:
        reason = ""

    status = f"Sjekk - {len(products)} produkt(er), {total} ledige billetter"
    if unreadable_cards:
        status += f", {len(unreadable_cards)} ULESELIG"
    log(f"{status} ({format_breakdown(resolved)})")

    if blind or debug:
        diag(f"side={page_state}")
        for product, count, source in resolved:
            if count is None or not (product["name"] or "").strip() or debug:
                diag(
                    f"'{product['name']}' kilde={source} "
                    f"number={product['number']!r} loose={product['looseNumber']!r} "
                    f"avail={product['availability']!r} tekst={product['cardText']!r}"
                )
                if count is None:
                    diag(f"'{product['name']}' html={product['cardHtml']!r}")

    # --- Billettvarsel FØRST ---
    # Diagnosevarsler må aldri stå foran i køen: er ntfy treg, ville de
    # forsinket det ene varselet som faktisk haster.
    increases = find_increases(state, counts, unreadable_keys, now_ts)

    delivered = True
    if increases:
        detaljer = [
            (next(p for p, _c, _s in resolved if product_key(p) == key), before, now)
            for key, before, now in increases
        ]
        gained = sum(now - (before or 0) for _k, before, now in increases)
        message = (
            f"LEDIGE resale-billetter! {gained} ny(e) billett(er): "
            f"{format_increases(detaljer)}."
        )
        try:
            notify("NFF Resale - Ledige billetter!", message)
            log("ntfy: billettvarsel sendt")
            for key, _before, now in increases:
                state["alerted"][key] = (now, now_ts)
        except Exception as e:
            # Ikke avanser baselinen - da ville varselet vært tapt for godt.
            # Neste sjekk ser samme økning og prøver på nytt.
            delivered = False
            log(f"Billettvarsel feilet - {type(e).__name__}: {e} (prøver igjen neste sjekk)")

    if delivered:
        new_baselines = {}
        for key, count in counts.items():
            # Uleselig nå -> baselinen er ukjent, ikke gammel verdi og ikke
            # 0. Å bære den gamle verdien videre kunne skjult en ekte
            # økning; å sette 0 ville gitt varsel på hvert eneste blaff.
            new_baselines[key] = None if key in unreadable_keys else count
        for key in unreadable_keys:
            new_baselines.setdefault(key, None)
        state["baselines"] = new_baselines

    register_check(state, blind, reason)
    return blind


def _watchdog(_signum, _frame):
    raise CheckTimeout(f"sjekken oversteg {CHECK_WATCHDOG}s")


def one_check(launch_kwargs, debug, state):
    """Kjører én sjekk i en fersk nettleser. Returnerer (ok, blind)."""
    if HAS_ALARM:
        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(CHECK_WATCHDOG)
    try:
        # Start og stopp nettleseren per sjekk for å holde minnebruken nede.
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(user_agent="Mozilla/5.0")
                page = context.new_page()
                try:
                    return True, run_check(page, debug, state)
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
        log(f"Error - {type(e).__name__}: {e}")
        # En strukturendring gir timeout her, ikke et uleselig tall. Uten
        # dette ville den vanligste bruddformen gått helt stille.
        register_check(state, True, f"sjekken feilet ({type(e).__name__})")
        return False, True
    finally:
        if HAS_ALARM:
            signal.alarm(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--debug", action="store_true",
                        help="logg [DIAG] for hver sjekk, ikke bare ved feil")
    parser.add_argument("-i", "--interval", type=int, default=POLL_INTERVAL,
                        help="sekunder mellom hver sjekk")
    parser.add_argument("--once", action="store_true",
                        help="kjør én sjekk og avslutt (exit 1 ved feil eller blind sjekk)")
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
        ok, blind = one_check(launch_kwargs, args.debug, state)

        if args.once:
            return 0 if (ok and not blind) else 1

        # Trekk fra tiden sjekken tok, ellers blir perioden 25-30s i praksis.
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
