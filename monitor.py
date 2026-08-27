"""Overvåker NFFs videresalgskatalog og varsler via ntfy når det dukker opp
ledige billetter.

Katalogen hentes som JSON fra endepunktet som backer resale-siden. Tidligere
ble siden rendret i headless Chromium og antallet lest ut av DOM-en; det er
borte nå. Strukturerte tall fjerner hele klassen av feil som fulgte av å
tolke HTML, og hver sjekk tar brøkdelen av tiden.
"""
import sys
import time
import signal
import argparse
from collections import deque
from datetime import datetime

import requests

API_URL = "https://resale.fotball.no/list/resale/resaleProductCatalog.json"
NTFY_URL = "https://ntfy.sh/nff-resale-billetter"
POLL_INTERVAL = 10       # sekunder mellom hver sjekk
API_TIMEOUT = 15         # sekunder
CHECK_WATCHDOG = 60      # sekunder før en hengende sjekk avbrytes

API_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://resale.fotball.no/list/resaleProducts/?lang=no",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}

# En sjekk regnes som "blind" når vi ikke fikk lest antallet. Vi varsler
# både ved sammenhengende blindhet og ved vedvarende blaffing, slik at et
# API som feiler annenhver gang ikke går under radaren.
BLIND_ALERT_AFTER = 3         # blinde sjekker på rad
BLIND_WINDOW = 20             # størrelse på glidende vindu
BLIND_WINDOW_THRESHOLD = 10   # blinde sjekker i vinduet som utløser varsel
BLIND_REALERT_EVERY = 180     # ~30 min ved 10s intervall
RECOVERY_AFTER_GOOD = 10      # gode sjekker på rad før friskmelding

# Etter en blind periode er baselinen ukjent. Da varsler vi på ethvert
# positivt antall (heller ett unødvendig varsel enn ett tapt), men demper
# gjentakelser av nøyaktig samme antall innenfor dette vinduet.
REPEAT_ALERT_COOLDOWN = 300   # sekunder

HAS_ALARM = hasattr(signal, "SIGALRM")


class ApiShapeError(Exception):
    """Svaret fra API-et hadde ikke formen vi forventer."""


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
    """
    # Title er en HTTP-header og må være ASCII; selve meldingen sendes som
    # UTF-8 og tåler æ/ø/å.
    safe_title = title.encode("ascii", "replace").decode("ascii")
    body = f"{message} {API_HEADERS['Referer']} ({datetime.now():%Y-%m-%d %H:%M:%S})"
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


def item(name, venue, count, detail, raw=None):
    return {
        "name": (name or "").strip(),
        "venue": (venue or "").strip(),
        "count": count,
        "detail": detail,
        "raw": raw,
    }


def api_count(product):
    """Henter antall ledige billetter fra ett produkt.

    availableQuantity er hovedsignalet; ticketCount er null i praksis. Er
    BEGGE null, er antallet ukjent - ikke null. Den forskjellen er hele
    grunnen til at varsler tidligere kunne forsvinne i stillhet.
    """
    for field in ("availableQuantity", "ticketCount"):
        value = product.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if value < 0:
                continue
            return value, field
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip()), f"{field} (streng)"
    return None, "availableQuantity og ticketCount mangler"


def parse_api_payload(data):
    """Gjør API-svaret om til normaliserte produkter + litt metadata.

    Kaster ApiShapeError hvis strukturen ikke er som forventet. Da telles
    sjekken som blind og du får beskjed, i stedet for at vi rapporterer
    null billetter fordi feltene har byttet navn.
    """
    if not isinstance(data, dict):
        raise ApiShapeError(f"forventet objekt, fikk {type(data).__name__}")
    if "topicWithProductsList" not in data:
        raise ApiShapeError("mangler topicWithProductsList")
    groups = data.get("topicWithProductsList") or []
    if not isinstance(groups, list):
        raise ApiShapeError("topicWithProductsList er ikke en liste")

    items = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for product in (group.get("products") or []):
            if not isinstance(product, dict):
                continue
            count, detail = api_count(product)
            items.append(item(product.get("name"), product.get("venue"),
                              count, detail, raw=product))

    # Alle produkter uten lesbart antall betyr at feltene har byttet navn.
    if items and all(i["count"] is None for i in items):
        raise ApiShapeError(f"ingen av {len(items)} produkter hadde lesbart antall")

    resale_items = data.get("resaleItems") or []
    meta = {
        "produkter": len(items),
        "resaleItems": len(resale_items) if isinstance(resale_items, list) else "?",
        "seatRelease": bool(data.get("seatRelease")),
    }
    return items, meta


def fetch(session):
    """Henter katalogen som JSON. Sender de samme headerne nettleseren
    bruker, slik at et eventuelt bot-filter ikke avviser oss."""
    resp = session.get(API_URL, timeout=API_TIMEOUT, headers=API_HEADERS)
    resp.raise_for_status()
    return parse_api_payload(resp.json())


def product_key(entry):
    """Nøkkel for å spore ett arrangement over tid.

    Returnerer None når navnet mangler. Da kan vi ikke skille produktene fra
    hverandre, og alle ville kollapset til samme nøkkel - det ville skjult
    ekte økninger helt lydløst. Uten navn regnes produktet som uleselig.
    """
    name = (entry.get("name") or "").strip()
    if not name:
        return None
    return f"{name}|{(entry.get('venue') or '').strip()}"


def format_breakdown(items):
    if not items:
        return "ingen arrangementer i katalogen"
    parts = []
    for entry in items:
        navn = entry["name"] or "(uten navn)"
        loc = f" @ {entry['venue']}" if entry["venue"] else ""
        antall = "ULESELIG" if entry["count"] is None else f"{entry['count']} billett(er)"
        parts.append(f"{navn}{loc}: {antall}")
    return "; ".join(parts)


def format_increases(increases):
    parts = []
    for key, before, now in increases:
        navn, _, sted = key.partition("|")
        loc = f" @ {sted}" if sted else ""
        forrige = "ukjent" if before is None else str(before)
        parts.append(f"{navn}{loc}: {forrige} -> {now} billett(er)")
    return "; ".join(parts)


def new_state():
    return {
        # nøkkel -> sist sikkert observerte antall, eller None = ukjent
        # (produktet var uleselig, så vi vet ikke hva som skjedde imens)
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
    lenge nok. Kalles for HVER sjekk - også de som kastet unntak. Uten det
    ville et API som endrer struktur gått helt stille forbi, og stillhet er
    ikke til å skille fra "ingen billetter".
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

    # Friskmelding krever en solid serie gode sjekker. Uten det ville et API
    # som blaffer sendt NEDE og friskmelding om hverandre i det uendelige, og
    # push-spam ender med at topicen dempes - da er også det ekte
    # billettvarselet borte.
    if not blind and not window_blind and state["good_streak"] >= RECOVERY_AFTER_GOOD:
        if state["blind_alerted"]:
            log("Katalogen leses normalt igjen - overvåkingen er tilbake i drift")
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
        # ntfy-feil gi en halvtimes ekstra stillhet.
        state["blind_alerted"] = True
        state["checks_since_blind_alert"] = 0
        log("ntfy: blind-varsel sendt")
    except Exception as e:
        log(f"Blind-varsel feilet - {type(e).__name__}: {e} (prøver igjen neste sjekk)")


def collect_counts(items):
    """Slår sammen produkter til {nøkkel: antall}, og rapporterer hvilke
    som ikke lot seg lese.

    Produkter med samme nøkkel summeres. Ville vi latt det siste overskrive
    det første, kunne en økning på det første blitt usynlig - og i motsatt
    rekkefølge ville baselinen blitt nullstilt hver sjekk og gitt varsel i
    det uendelige.
    """
    counts = {}
    unreadable_keys = set()
    unreadable = []
    for entry in items:
        key = product_key(entry)
        if key is None or entry["count"] is None:
            unreadable.append(entry)
            if key is not None:
                unreadable_keys.add(key)
            continue
        counts[key] = counts.get(key, 0) + entry["count"]
    return counts, unreadable_keys, unreadable


def find_increases(state, counts, unreadable_keys, now_ts):
    """Finner arrangementer som har fått flere billetter siden sist.

    Er baselinen ukjent (produktet var uleselig forrige gang), varsler vi på
    ethvert positivt antall. Vi vet ikke om antallet falt til null imens, og
    et unødvendig varsel er langt billigere enn et tapt. Gjentakelser av
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


def run_check(items, meta, debug, state, now_ts=None):
    """Vurderer ett sett produkter og varsler. Returnerer True hvis blind."""
    now_ts = time.monotonic() if now_ts is None else now_ts
    counts, unreadable_keys, unreadable = collect_counts(items)
    total = sum(counts.values())

    # En tom katalog er legitim: API-et sa uttrykkelig fra ved å svare med
    # feltet til stede og lista tom. Manglet feltet, hadde parse_api_payload
    # allerede kastet.
    blind = bool(unreadable)
    reason = ""
    if unreadable:
        navnloese = sum(1 for e in unreadable if not e["name"])
        reason = f"klarer ikke lese {len(unreadable)} produkt(er)"
        if navnloese:
            reason += f" ({navnloese} mangler navn - API-et kan ha endret struktur)"

    status = f"Sjekk - {len(items)} produkt(er), {total} ledige billetter"
    if unreadable:
        status += f", {len(unreadable)} ULESELIG"
    log(f"{status} ({format_breakdown(items)})")

    if blind or debug:
        diag(f"meta={meta}")
        for entry in items:
            if entry["count"] is None or not entry["name"] or debug:
                diag(f"'{entry['name']}' felt={entry['detail']} raa={entry['raw']!r}")

    # --- Billettvarsel FØRST ---
    # Diagnosevarsler må aldri stå foran i køen: er ntfy treg, ville de
    # forsinket det ene varselet som faktisk haster.
    increases = find_increases(state, counts, unreadable_keys, now_ts)

    delivered = True
    if increases:
        gained = sum(now - (before or 0) for _k, before, now in increases)
        message = (f"LEDIGE resale-billetter! {gained} ny(e) billett(er): "
                   f"{format_increases(increases)}.")
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


def one_check(session, debug, state):
    """Kjører én sjekk. Returnerer (ok, blind)."""
    if HAS_ALARM:
        signal.signal(signal.SIGALRM, _watchdog)
        signal.alarm(CHECK_WATCHDOG)
    try:
        items, meta = fetch(session)
        return True, run_check(items, meta, debug, state)
    except Exception as e:
        log(f"Error - {type(e).__name__}: {e}")
        # En strukturendring gir unntak her, ikke et uleselig tall. Uten
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

    session = requests.Session()
    state = new_state()
    log(f"Starter overvåking av {API_URL} (intervall {args.interval}s)")

    while True:
        started = time.monotonic()
        ok, blind = one_check(session, args.debug, state)

        if args.once:
            return 0 if (ok and not blind) else 1

        # Trekk fra tiden sjekken tok, ellers blir perioden lengre enn valgt.
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
