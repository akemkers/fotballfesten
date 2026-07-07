import requests
import hashlib
import time
import argparse
from datetime import datetime

URL = "https://www.fotballfesten.no/frognerstadion"
NTFY_URL = "https://ntfy.sh/fotballfesten-kemkers"

def debug_log(message, enabled):
    if enabled:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} [DEBUG] {message}")

def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def send_notification():
    headers = {
        "Title": "FOTBALLFESTEN",
        "Priority": "5",
        "Tags": "rotating_light,ticket"
    }
    body = f"Siden endret seg {datetime.now()}"
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
last_html = ""

while True:
    try:
        debug_log("Downloading page...", debug)
        response = requests.get(
            URL,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            timeout=30
        )

        response.raise_for_status()
        html = response.text
        debug_log(f"Downloaded {len(html)} bytes", debug)

        if not html:
            print(f"{datetime.now()}: Empty response received, skipping cycle")
            time.sleep(10)
            continue

        if not last_html:
            print(f"{datetime.now()}: Initial page loaded")
            initial_hash = sha256(html)
            debug_log(f"Initial hash: {initial_hash}", debug)
        else:
            current_hash = sha256(html)
            previous_hash = sha256(last_html)
            debug_log(f"Current length : {len(html)}", debug)
            debug_log(f"Previous length: {len(last_html)}", debug)
            debug_log(f"Current hash   : {current_hash}", debug)
            debug_log(f"Previous hash  : {previous_hash}", debug)
            if current_hash != previous_hash:
                debug_log("Content differs", debug)
                if debug:
                    max_len = min(len(html), len(last_html))
                    for i in range(max_len):
                        if html[i] != last_html[i]:
                            start = max(0, i - 500)
                            length = min(1000, len(html) - start)
                            print("\n========== OLD HTML ==========")
                            print(last_html[start:start + length])
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
                        f.write(last_html)
                    with open(curr_file, "w", encoding="utf-8") as f:
                        f.write(html)
                    print("Saved diff files:")
                    print(f"  {prev_file}")
                    print(f"  {curr_file}")

                print(f"{datetime.now()}: CHANGE DETECTED!")

                try:
                    send_notification()
                    print(f"{datetime.now()}: ntfy notification sent")
                except Exception as e:
                    print(f"{datetime.now()}: Notification failed - {e}")
            else:
                debug_log("Content identical", debug)
                print(f"{datetime.now()}: No change detected")

        last_html = html
    except Exception as e:
        print(f"{datetime.now()}: Error - {e}")
    time.sleep(10)
