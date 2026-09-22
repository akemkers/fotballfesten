"""Varsler via ntfy når det dukker opp ledige resale-billetter hos NFF.

Katalogen hentes som JSON én gang i sekundet. Antallet ledige ligger i
`availableQuantity` per arrangement.

To ting er verdt å vite før du endrer noe her:

1. `availableQuantity` kan være `null`. Null er IKKE null billetter - det
   betyr at vi ikke fikk lest antallet. Tolkes de to likt, ser en ødelagt
   overvåker ut som helt normal drift, og varselet uteblir uten at noe ser
   galt ut i loggen. Det var den opprinnelige feilen i dette prosjektet.

2. Derfor sier scriptet fra når det ikke får lest katalogen. Stillhet fra
   ntfy skal bety «ingen billetter», ikke «overvåkingen er død».
"""
import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

API_URL = "https://resale.fotball.no/list/resale/resaleProductCatalog.json"
PAGE_URL = "https://resale.fotball.no/list/resaleProducts/?lang=no"
# Topicen er offentlig: alle som kjenner navnet kan lese og poste. Sett
# NTFY_URL til en topic med et vanskelig navn for å holde den for deg selv.
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/nff-resale-billetter")

POLL_INTERVAL = 1        # sekunder mellom hver sjekk
REQUEST_TIMEOUT = 10     # sekunder
BLIND_AFTER = 60         # sekunder sammenhengende feil før vi varsler
BLIND_REPEAT = 1800      # sekunder mellom gjentatte «nede»-varsler
LOG_EVERY = 300          # sekunder mellom ellers uendrede statuslinjer

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": PAGE_URL,
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}: {message}", flush=True)


def send(title, message, priority="5", tags="soccer,rotating_light"):
    """Sender push via ntfy. Returnerer False ved feil, slik at kallstedet
    kan la være å oppdatere tilstanden - da prøver neste sjekk på nytt.

    Tittelen er en HTTP-header og må være ren ASCII. Meldingen sendes som
    UTF-8 og tåler æ/ø/å. Trykk på varselet åpner resale-siden.
    """
    try:
        resp = requests.post(
            NTFY_URL,
            headers={"Title": title, "Priority": priority, "Tags": tags,
                     "Click": PAGE_URL},
            data=message.encode("utf-8"),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log(f"ntfy feilet - {type(e).__name__}: {e}")
        return False


# --- Lesing av katalogen ---

def _is_count(value):
    # bool er en underklasse av int i Python, så True må avvises eksplisitt.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def read_counts(data):
    """Returnerer ({arrangement: antall}, antall uleselige).

    Kaster hvis svaret ikke har forventet form - da telles sjekken som en
    feil, i stedet for at vi rapporterer null billetter fordi feltene har
    byttet navn.
    """
    counts = {}
    unreadable = 0
    for group in data["topicWithProductsList"]:
        for product in group.get("products") or []:
            name = (product.get("name") or "").strip()
            quantity = product.get("availableQuantity")
            if not name or not _is_count(quantity):
                unreadable += 1
                continue
            key = f"{name} @ {(product.get('venue') or '?').strip()}"
            # Flere oppføringer av samme arrangement summeres. Lot vi den
            # siste overskrive den første, kunne en økning på den første
            # blitt usynlig.
            counts[key] = counts.get(key, 0) + quantity
    return counts, unreadable


def check(session):
    """Henter katalogen. Returnerer (antall-per-arrangement, feilbeskrivelse).

    - Alt lest:          (antall, None)
    - Noe uleselig:      (antall for de lesbare, feil)
    - Henting feilet:    (None, feil)

    Ett uleselig arrangement skal ikke gjøre oss blinde for de andre, så de
    lesbare antallene sendes videre selv om det også rapporteres en feil.
    """
    try:
        resp = session.get(API_URL, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
        counts, unreadable = read_counts(resp.json())
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if unreadable:
        return counts, f"{unreadable} arrangement(er) uten lesbart antall"
    return counts, None


# --- Beslutninger ---

@dataclass
class State:
    counts: dict = field(default_factory=dict)  # sist leste antall per arrangement
    broken_since: float | None = None           # når sjekkene begynte å feile
    alerted_down: float | None = None           # når vi sist sa fra om at det er nede
    last_log: float | None = None               # for å slippe én logglinje i sekundet
    last_status: str | None = None


def step(state, counts, problem, now, notify=None):
    """Én runde med beslutninger. Skilt fra nettverkskallet slik at den kan
    testes uten å gå på nett. `notify` har samme signatur som `send`."""
    notify = notify or send
    if counts is not None:
        _alert_on_new_tickets(state, counts, notify)
    _alert_on_health(state, problem, now, notify)
    _log_status(state, counts, problem, now)


def _alert_on_new_tickets(state, counts, notify):
    gains = [(key, state.counts.get(key, 0), count)
             for key, count in counts.items() if count > state.counts.get(key, 0)]
    if gains:
        detail = "; ".join(f"{key}: {before} -> {after}" for key, before, after in gains)
        new = sum(after - before for _key, before, after in gains)
        if not notify("NFF Resale - Ledige billetter!",
                      f"LEDIGE resale-billetter! {new} ny(e): {detail}."):
            # Gammel tilstand beholdes, så neste sjekk ser samme økning og
            # prøver igjen.
            return
        log(f"VARSEL sendt - {detail}")
    # update(), ikke tilordning: et arrangement som manglet i dette svaret
    # beholder sist kjente antall. Ved sekundintervall er et slikt hull så
    # kort at det ikke er verdt egen håndtering.
    state.counts.update(counts)


def _alert_on_health(state, problem, now, notify):
    """Si fra hvis vi ikke får lest katalogen, og når det virker igjen."""
    if problem:
        if state.broken_since is None:
            state.broken_since = now
        down_for = now - state.broken_since
        due = (state.alerted_down is None
               or now - state.alerted_down >= BLIND_REPEAT)
        if down_for >= BLIND_AFTER and due:
            if notify("NFF Resale - VARSLING NEDE",
                      f"Overvåkingen har feilet i {int(down_for)}s: {problem}",
                      priority="4", tags="warning"):
                state.alerted_down = now
        return

    state.broken_since = None
    # Friskmelding bare hvis vi faktisk sa fra. Feiler den, prøver vi igjen
    # neste runde - ellers tror mottakeren at det fortsatt er nede.
    if state.alerted_down is not None:
        if notify("NFF Resale - virker igjen",
                  "Overvåkingen leser katalogen som normalt igjen.",
                  priority="2", tags="white_check_mark"):
            state.alerted_down = None


def _describe(counts, problem):
    parts = []
    if problem:
        parts.append(f"FEIL - {problem}")
    if counts:
        parts.append(f"{len(counts)} arrangement(er), {sum(counts.values())} ledige: "
                     + "; ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    elif counts is not None and not problem:
        parts.append("0 arrangementer i katalogen")
    return " | ".join(parts)


def _log_status(state, counts, problem, now):
    # Én linje i sekundet ville gjort loggen ubrukelig, så vi logger bare
    # når noe endrer seg, pluss et livstegn med jevne mellomrom.
    status = _describe(counts, problem)
    if (status != state.last_status or state.last_log is None
            or now - state.last_log >= LOG_EVERY):
        log(status)
        state.last_status = status
        state.last_log = now


# --- Kjøring ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--interval", type=float, default=POLL_INTERVAL,
                        help="sekunder mellom hver sjekk")
    parser.add_argument("--once", action="store_true",
                        help="kjør én sjekk og avslutt (exit 1 hvis den feilet)")
    parser.add_argument("--test-notify", action="store_true",
                        help="send en testmelding til ntfy og avslutt")
    args = parser.parse_args()

    if args.test_notify:
        return 0 if send("NFF Resale - test", "Testmelding fra monitor.py.",
                         priority="3", tags="white_check_mark") else 1

    session = requests.Session()
    state = State()
    log(f"Starter overvåking av {API_URL} (intervall {args.interval}s)")

    while True:
        started = time.monotonic()
        counts, problem = check(session)
        step(state, counts, problem, started)

        if args.once:
            return 1 if problem else 0

        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
