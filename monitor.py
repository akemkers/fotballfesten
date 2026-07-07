import requests
import hashlib
import time
import argparse
from datetime import datetime

URLS = [
    "https://www.fotballfesten.no/frognerstadion",
    "https://fotballfesten.no",
]
NTFY_URL = "https://ntfy.sh/fotballfesten-kemkers"

def debug_log(message, enabled):
    if enabled:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [DEBUG] {message}")

def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def send_notification(url):
    headers = {
        "Title": "FOTBALLFESTEN",
        "Priority": "5",
        "Tags": "rotating_light"
    }
    body = f"Siden endret seg: {url} ({datetime.now()})"
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
last_html = {url: "" for url in URLS}

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

            if not last_html[url]:
                print(f"{datetime.now()}: Initial page loaded ({url})")
                initial_hash = sha256(html)
                debug_log(f"Initial hash: {initial_hash}", debug)
            else:
                current_hash = sha256(html)
                previous_hash = sha256(last_html[url])
                debug_log(f"[{url}] Current length : {len(html)}", debug)
                debug_log(f"[{url}] Previous length: {len(last_html[url])}", debug)
                debug_log(f"[{url}] Current hash   : {current_hash}", debug)
                debug_log(f"[{url}] Previous hash  : {previous_hash}", debug)
                if current_hash != previous_hash:
                    debug_log(f"Content differs ({url})", debug)
                    if debug:
                        prev = last_html[url]
                        max_len = min(len(html), len(prev))
                        for i in range(max_len):
                            if html[i] != prev[i]:
                                start = max(0, i - 500)
                                length = min(1000, len(html) - start)
                                print("\n========== OLD HTML ==========")
                                print(prev[start:start + length])
                                print("\n========== NEW HTML ==========")
                                print(html[start:start + length])
                                print(f"\nDifference position: {i}")
                                print("==============================\n")
                                break
                        else:
                            print(f"\nDifference position: {max_len} (length change only)")

                        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                        prev_file = f"previous-{timestamp}.html"
                        curr_file = f"current-{timestamp}.html"
                        with open(prev_file, "w", encoding="utf-8") as f:
                            f.write(prev)
                        with open(curr_file, "w", encoding="utf-8") as f:
                            f.write(html)
                        print("Saved diff files:")
                        print(f"  {prev_file}")
                        print(f"  {curr_file}")

                    print(f"{datetime.now()}: CHANGE DETECTED! ({url})")

                    try:
                        send_notification(url)
                        print(f"{datetime.now()}: ntfy notification sent")
                    except Exception as e:
                        print(f"{datetime.now()}: Notification failed - {e}")
                else:
                    debug_log(f"Content identical ({url})", debug)
                    print(f"{datetime.now()}: No change detected ({url})")

            last_html[url] = html
        except Exception as e:
            print(f"{datetime.now()}: Error ({url}) - {e}")
    time.sleep(10)
