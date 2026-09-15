"""CrowPi RFID reader service.

Runs on the CrowPi and posts every tag scan to the backend's single /api/scan
endpoint. There is no CLI mode flag: the backend decides what a scan means.
A brand new tag gets parked as "pending" for an admin to name in the web
panel at <backend>/admin. A tag that is already registered just logs an
attendance event (toggles enter/exit). This is meant to run unattended, for
example as a systemd service that starts on boot, so nobody has to SSH in
and type a command before attendance can be taken.

Pass --simulate to run without the CrowPi's RFID hardware attached (types a
fake tag UID instead of tapping a real card) so the pipeline can be tested
from a laptop before the CrowPi is set up.
"""

import argparse
import time

import requests

DEBOUNCE_SECONDS = 2


def post_scan(base_url, tag_uid):
    try:
        resp = requests.post(f"{base_url}/api/scan", json={"tag_uid": tag_uid}, timeout=3)
        return resp.json()
    except requests.RequestException as exc:
        print(f"Could not reach backend at {base_url}: {exc}")
        return None


def handle_scan(base_url, tag_uid):
    result = post_scan(base_url, tag_uid)
    if result is None:
        return
    if result.get("status") == "pending":
        print(f"New tag {tag_uid} - go name it at {base_url}/admin")
    elif result.get("status") == "logged":
        print(f"{result['student']}: {result['type']}")
    else:
        print(f"Scan result: {result}")


def run_hardware(base_url):
    from mfrc522 import SimpleMFRC522

    reader = SimpleMFRC522()
    last_seen = {}
    print("CrowPi reader running. Tap a tag on the scan pad. Ctrl+C to quit.")
    try:
        while True:
            uid, _ = reader.read()
            uid = str(uid)
            now = time.time()
            if now - last_seen.get(uid, 0) > DEBOUNCE_SECONDS:
                handle_scan(base_url, uid)
                last_seen[uid] = now
    except KeyboardInterrupt:
        print("\nStopped.")


def run_simulate(base_url):
    print("Simulate mode - no CrowPi hardware needed.")
    print("Type a fake tag UID and press enter to fake a scan (Ctrl+C to quit).")
    try:
        while True:
            uid = input("tag_uid> ").strip()
            if uid:
                handle_scan(base_url, uid)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CrowPi RFID reader service")
    parser.add_argument("--simulate", action="store_true", help="Run without CrowPi hardware")
    parser.add_argument("--backend", default="http://localhost:5050", help="Backend base URL")
    args = parser.parse_args()

    if args.simulate:
        run_simulate(args.backend)
    else:
        try:
            run_hardware(args.backend)
        except ImportError:
            print("mfrc522 library not available - falling back to simulate mode.")
            run_simulate(args.backend)
