"""CrowPi RFID reader service.

Runs on the CrowPi and posts each tag scan to the backend. Two modes:

  --register   scanning a tag prompts for a student name and registers it
               (use this once per tag to map the ~20 physical tags to students)
  (default)    scanning a tag logs an attendance event (toggles enter/exit)

Pass --simulate to run without the CrowPi's RFID hardware attached (types a
fake tag UID instead of tapping a real card) so the pipeline can be tested
from a laptop before the CrowPi is set up.
"""

import argparse
import time

import requests

DEBOUNCE_SECONDS = 2


def post(base_url, path, payload):
    try:
        resp = requests.post(f"{base_url}{path}", json=payload, timeout=3)
        return resp.json()
    except requests.RequestException as exc:
        print(f"Could not reach backend at {base_url}: {exc}")
        return None


def handle_scan(base_url, tag_uid, register):
    if register:
        name = input(f"Tag {tag_uid} scanned - enter student name: ").strip()
        if not name:
            print("Skipped (no name entered)")
            return
        result = post(base_url, "/api/admin/register", {"tag_uid": tag_uid, "name": name})
        print(f"Registered: {result}")
    else:
        result = post(base_url, "/api/events", {"tag_uid": tag_uid})
        print(f"Scan result: {result}")


def run_hardware(base_url, register):
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
                handle_scan(base_url, uid, register)
                last_seen[uid] = now
    except KeyboardInterrupt:
        print("\nStopped.")


def run_simulate(base_url, register):
    print("Simulate mode - no CrowPi hardware needed.")
    print("Type a fake tag UID and press enter to fake a scan (Ctrl+C to quit).")
    try:
        while True:
            uid = input("tag_uid> ").strip()
            if uid:
                handle_scan(base_url, uid, register)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CrowPi RFID reader service")
    parser.add_argument("--simulate", action="store_true", help="Run without CrowPi hardware")
    parser.add_argument(
        "--register",
        action="store_true",
        help="Registration mode: scanning a tag prompts for a student name",
    )
    parser.add_argument("--backend", default="http://localhost:5050", help="Backend base URL")
    args = parser.parse_args()

    if args.simulate:
        run_simulate(args.backend, args.register)
    else:
        try:
            run_hardware(args.backend, args.register)
        except ImportError:
            print("mfrc522 library not available - falling back to simulate mode.")
            run_simulate(args.backend, args.register)
