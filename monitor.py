import requests
import hashlib
import time
import argparse
import re
from datetime import datetime

URLS = [
    "https://resale.fotball.no/list/resaleProducts/?lang=no",
]
NTFY_URL = "https://ntfy.sh/nff-resale-billetter"

# Matcher "0 billetter", "1 billett", "12 billetter" osv.
TICKET_RE = re.compile(r"(\d+)\s*billett", re.IGNORECASE)

def debug_log(message, enabled):
    if enabled:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [DEBUG] {message}")

def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def ticket_count(html):
    """Summerer alle "<n> billett(er)"-forekomster i HTML-en.

    Returnerer (total, found). found=False betyr at mønsteret ikke fantes
    i det hele tatt - da er siden sannsynligvis JavaScript-rendret, og vi
    faller tilbake til hash-basert endringsdeteksjon.
    """
    matches = TICKET_RE.findall(html)
    if not matches:
        return 0, False
    return sum(int(m) for m in matches), True

def send_notification(url, message):
    headers = {
        "Title": "NFF RESALE - ULLEVÅL",
        "Priority": "5",
        "Tags": "soccer,rotating_light"
    }
    body = f"{message} Sjekk {url} ({datetime.now()})"
    requests.post(
        NTFY_URL,
        headers=headers,
        data=body.encode("utf-8"),
        timeout=30
    )

parser = argparse.ArgumentParser()
parser.add_argument("-d", "--debug", action="store_true")
args = parser.parse_args()
debug = args.debug

# Per-URL tilstand: sist observert billettantall, sist observert hash,
# og om vi allerede har advart om manglende billett-tekst.
state = {url: {"count": None, "hash": None, "warned": False} for url in URLS}

while True:
    for url in URLS:
        try:
            debug_log(f"Downloading page... {url}", debug)
            response = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0"
                },
                timeout=30
            )

            response.raise_for_status()
            html = response.text
            debug_log(f"Downloaded {len(html)} bytes from {url}", debug)

            if not html:
                print(f"{datetime.now()}: Empty response received from {url}, skipping cycle")
                continue

            st = state[url]
            count, found = ticket_count(html)

            if found:
                # Primærmodus: følg billettantallet direkte.
                debug_log(f"[{url}] Ledige billetter: {count}", debug)
                if st["count"] is None:
                    print(f"{datetime.now()}: Initial load ({url}) - {count} billett(er)")
                elif count != st["count"]:
                    prev = st["count"]
                    print(f"{datetime.now()}: ENDRING ({url}): {prev} -> {count} billett(er)")
                    if count > prev:
                        message = f"LEDIGE resale-billetter til Ullevål! Nå {count} billett(er) tilgjengelig (var {prev})."
                    elif count == 0:
                        message = "Resale-billetter til Ullevål er utsolgt igjen (0 billetter)."
                    else:
                        message = f"Antall resale-billetter til Ullevål endret seg: {prev} -> {count}."
                    try:
                        send_notification(url, message)
                        print(f"{datetime.now()}: ntfy notification sent")
                    except Exception as e:
                        print(f"{datetime.now()}: Notification failed - {e}")
                else:
                    print(f"{datetime.now()}: Ingen endring ({url}) - {count} billett(er)")
                st["count"] = count
            else:
                # Fallback: billett-teksten fantes ikke i rå-HTML, sannsynligvis
                # JavaScript-rendret. Advar én gang og fall tilbake til hash.
                if not st["warned"]:
                    print(f"{datetime.now()}: ADVARSEL ({url}): fant ingen "
                          f"'<n> billetter'-tekst i HTML-en. Siden er trolig "
                          f"JavaScript-rendret; faller tilbake til hash-basert "
                          f"endringsdeteksjon. Vurder Playwright for pålitelig deteksjon.")
                    st["warned"] = True
                current_hash = sha256(html)
                if st["hash"] is None:
                    print(f"{datetime.now()}: Initial load ({url}) - hash {current_hash[:12]}")
                elif current_hash != st["hash"]:
                    print(f"{datetime.now()}: CHANGE DETECTED (hash) ({url})")
                    try:
                        send_notification(url, "Resale-siden for Ullevål endret seg.")
                        print(f"{datetime.now()}: ntfy notification sent")
                    except Exception as e:
                        print(f"{datetime.now()}: Notification failed - {e}")
                else:
                    print(f"{datetime.now()}: No change detected ({url})")
                st["hash"] = current_hash
        except Exception as e:
            print(f"{datetime.now()}: Error ({url}) - {e}")
    time.sleep(10)
