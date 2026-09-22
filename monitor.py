"""Varsler via ntfy når NFF legger ut resale-billetter.

`availableQuantity: null` betyr at antallet ikke kunne leses, ikke 0.
Derfor varsler vi også når katalogen ikke kan leses: stillhet skal bety
«ingen billetter», ikke «overvåkingen er død».
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
# ntfy-topics er offentlige. Sett NTFY_URL til et navn som er vanskelig å gjette.
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/nff-resale-billetter")

# Alle i sekunder.
POLL_INTERVAL = 1        # tid mellom sjekker
REQUEST_TIMEOUT = 10
BLIND_AFTER = 60         # feil før «nede»-varsel
BLIND_REPEAT = 1800      # tid mellom gjentatte «nede»-varsler
LOG_EVERY = 300          # livstegn i loggen

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
    """Sender push via ntfy. Returnerer False ved feil, så neste sjekk kan
    prøve igjen. Tittelen er en HTTP-header og må være ASCII."""
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
        log(f"ntfy feilet: {type(e).__name__}: {e}")
        return False


# --- Lesing av katalogen ---

def _is_count(value):
    # bool er en underklasse av int.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def read_counts(data):
    """Returnerer ({arrangement: antall}, antall uleselige).

    Kaster ved uventet format, så et endret API gir feil i stedet for 0.
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
            # Summer duplikater, ellers kan en økning på det ene skjules.
            counts[key] = counts.get(key, 0) + quantity
    return counts, unreadable


def check(session):
    """Henter katalogen og returnerer (antall, feil):

    - alt lest:        (antall, None)
    - noe uleselig:    (antall for de lesbare, feil)
    - henting feilet:  (None, feil)
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
    counts: dict = field(default_factory=dict)  # sist kjente antall per arrangement
    broken_since: float | None = None           # første feil i gjeldende feilperiode
    alerted_down: float | None = None           # siste «nede»-varsel
    last_log: float | None = None
    last_status: str | None = None


def step(state, counts, problem, now, notify=None):
    """Én runde med beslutninger, uten nettverk. `notify` er som `send`."""
    notify = notify or send
    if counts is not None:
        _alert_on_new_tickets(state, counts, notify)
    _alert_on_health(state, problem, now, notify)
    _log_status(state, counts, problem, now)


def _alert_on_new_tickets(state, counts, notify):
    gains = [(key, state.counts.get(key, 0), count)
             for key, count in counts.items() if count > state.counts.get(key, 0)]
    if gains:
        detail = "; ".join(f"{key}: {before} → {after}" for key, before, after in gains)
        if not notify("NFF Resale - Ledige billetter!", detail):
            return  # behold tilstanden, så neste sjekk prøver igjen
        log(f"Varsel sendt: {detail}")
    # update(), ikke tilordning: arrangementer som mangler i svaret beholder
    # sist kjente antall.
    state.counts.update(counts)


def _alert_on_health(state, problem, now, notify):
    """Varsler når katalogen ikke kan leses, og når den kan leses igjen."""
    if problem:
        if state.broken_since is None:
            state.broken_since = now
        down_for = now - state.broken_since
        due = (state.alerted_down is None
               or now - state.alerted_down >= BLIND_REPEAT)
        if down_for >= BLIND_AFTER and due:
            if notify("NFF Resale - VARSLING NEDE",
                      f"Har ikke fått lest katalogen på {int(down_for)} s: {problem}",
                      priority="4", tags="warning"):
                state.alerted_down = now
        return

    state.broken_since = None
    # Friskmeld bare hvis vi varslet om nede. Feiler det, prøv igjen neste runde.
    if state.alerted_down is not None:
        if notify("NFF Resale - virker igjen",
                  "Katalogen kan leses igjen.",
                  priority="2", tags="white_check_mark"):
            state.alerted_down = None


def _describe(counts, problem):
    parts = []
    if problem:
        parts.append(f"FEIL: {problem}")
    if counts:
        parts.append(f"{sum(counts.values())} ledige i {len(counts)} arrangement(er): "
                     + "; ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    elif counts is not None and not problem:
        parts.append("katalogen er tom")
    return " | ".join(parts)


def _log_status(state, counts, problem, now):
    # Logg ved endring, og ellers hvert LOG_EVERY.
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
                        help="sekunder mellom sjekker")
    parser.add_argument("--once", action="store_true",
                        help="kjør én sjekk og avslutt (exit 1 ved feil)")
    parser.add_argument("--test-notify", action="store_true",
                        help="send testvarsel og avslutt")
    args = parser.parse_args()

    if args.test_notify:
        return 0 if send("NFF Resale - test", "Testvarsel fra monitor.py.",
                         priority="3", tags="white_check_mark") else 1

    session = requests.Session()
    state = State()
    log(f"Overvåker {API_URL} hvert {args.interval} s")

    while True:
        started = time.monotonic()
        counts, problem = check(session)
        step(state, counts, problem, started)

        if args.once:
            return 1 if problem else 0

        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
