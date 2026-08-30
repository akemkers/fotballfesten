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
import sys
import time
import argparse
from datetime import datetime

import requests

API_URL = "https://resale.fotball.no/list/resale/resaleProductCatalog.json"
NTFY_URL = "https://ntfy.sh/nff-resale-billetter"
PAGE_URL = "https://resale.fotball.no/list/resaleProducts/?lang=no"

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
    UTF-8 og tåler æ/ø/å.
    """
    try:
        resp = requests.post(
            NTFY_URL,
            headers={"Title": title, "Priority": priority, "Tags": tags},
            data=f"{message} {PAGE_URL}".encode("utf-8"),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log(f"ntfy feilet - {type(e).__name__}: {e}")
        return False


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
            if not name or isinstance(quantity, bool) or not isinstance(quantity, int) \
                    or quantity < 0:
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
    Nøyaktig én av de to er None."""
    try:
        resp = session.get(API_URL, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
        counts, unreadable = read_counts(resp.json())
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if unreadable:
        return None, f"{unreadable} arrangement(er) uten lesbart antall"
    return counts, None


def new_state():
    return {
        "counts": {},            # sist leste antall per arrangement
        "broken_since": None,    # når sjekkene begynte å feile
        "alerted_down": None,    # når vi sist sa fra om at det er nede
        "last_log": None,        # for å slippe én logglinje i sekundet
        "last_status": None,
    }


def step(state, counts, problem, now):
    """Én runde med beslutninger. Skilt fra nettverkskallet slik at den kan
    testes uten å gå på nett."""
    if counts is not None:
        gains = [(k, state["counts"].get(k, 0), v)
                 for k, v in counts.items() if v > state["counts"].get(k, 0)]
        if gains:
            detalj = "; ".join(f"{k}: {før} -> {nå}" for k, før, nå in gains)
            nye = sum(nå - før for _k, før, nå in gains)
            if send("NFF Resale - Ledige billetter!",
                    f"LEDIGE resale-billetter! {nye} ny(e): {detalj}."):
                log(f"VARSEL sendt - {detalj}")
                state["counts"].update(counts)
            # Ved feilet varsel beholdes gammel tilstand, så neste sjekk
            # ser samme økning og prøver igjen.
        else:
            # update(), ikke tilordning: et arrangement som manglet i dette
            # svaret beholder sist kjente antall. Ved sekundintervall er et
            # slikt hull så kort at det ikke er verdt egen håndtering.
            state["counts"].update(counts)

    # --- Si fra hvis vi ikke får lest katalogen ---
    if problem:
        if state["broken_since"] is None:
            state["broken_since"] = now
        nede = now - state["broken_since"]
        moden = nede >= BLIND_AFTER
        forfalt = (state["alerted_down"] is None
                   or now - state["alerted_down"] >= BLIND_REPEAT)
        if moden and forfalt:
            if send("NFF Resale - VARSLING NEDE",
                    f"Overvakingen har feilet i {int(nede)}s: {problem}",
                    priority="4", tags="warning"):
                state["alerted_down"] = now
    else:
        if state["alerted_down"] is not None:
            send("NFF Resale - virker igjen",
                 "Overvakingen leser katalogen som normalt igjen.",
                 priority="2", tags="white_check_mark")
        state["broken_since"] = None
        state["alerted_down"] = None

    # --- Logg ---
    # Én linje i sekundet ville gjort loggen ubrukelig, så vi logger bare
    # når noe endrer seg, pluss et livstegn med jevne mellomrom.
    if counts is None:
        status = f"FEIL - {problem}"
    elif not counts:
        status = "0 arrangementer i katalogen"
    else:
        status = (f"{len(counts)} arrangement(er), {sum(counts.values())} ledige: "
                  + "; ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if status != state["last_status"] or state["last_log"] is None \
            or now - state["last_log"] >= LOG_EVERY:
        log(status)
        state["last_status"] = status
        state["last_log"] = now


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
    state = new_state()
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
