"""CrowPi RFID reader service.

Runs on the CrowPi and posts every tag scan to the backend's single /api/scan
endpoint. There is no CLI mode flag: the backend decides what a scan means.
A brand new tag gets parked as "pending" for an admin to name in the web
panel at <backend>/admin. A tag that is already registered just logs an
attendance event (toggles enter/exit). This is meant to run unattended, for
example as a systemd service that starts on boot, so nobody has to SSH in
and type a command before attendance can be taken.

A scan that fails to reach the backend (wifi blip, backend restarting) is
retried a few times immediately, and if it still fails, gets queued and
retried in the background every few seconds until it goes through, instead
of just being silently dropped.

Pass --simulate to run without the CrowPi's RFID hardware attached (types a
fake tag UID instead of tapping a real card) so the pipeline can be tested
from a laptop before the CrowPi is set up.
"""

import argparse
import time

import requests

DEBOUNCE_SECONDS = 2
IMMEDIATE_RETRIES = 3
IMMEDIATE_RETRY_BACKOFF = [0.5, 1, 2]  # seconds, one per retry
QUEUE_RETRY_SECONDS = 5


def try_post(base_url, tag_uid):
    """One attempt to post a scan. Returns the parsed response, or None if
    the backend could not be reached (network error, not an HTTP error)."""
    try:
        resp = requests.post(f"{base_url}/api/scan", json={"tag_uid": tag_uid}, timeout=3)
        return resp.json()
    except requests.RequestException:
        return None


def post_scan_with_retry(base_url, tag_uid):
    """Try immediately, with a couple of quick retries, before giving up
    and letting the caller queue it for background retry."""
    result = try_post(base_url, tag_uid)
    if result is not None:
        return result

    for attempt, backoff in enumerate(IMMEDIATE_RETRY_BACKOFF, start=1):
        print(f"  backend unreachable, retrying ({attempt}/{IMMEDIATE_RETRIES})...")
        time.sleep(backoff)
        result = try_post(base_url, tag_uid)
        if result is not None:
            print("  reconnected.")
            return result

    return None


def describe(result):
    if result.get("status") == "pending":
        return f"New tag {result['tag_uid']} - go name it at /admin"
    if result.get("status") == "logged":
        return f"{result['student']}: {result['type']}"
    return f"Scan result: {result}"


def handle_scan(base_url, tag_uid, retry_queue):
    result = post_scan_with_retry(base_url, tag_uid)
    if result is None:
        print(f"Could not reach backend at {base_url} - queued {tag_uid} for retry")
        retry_queue.append(tag_uid)
        return
    print(describe(result))


def drain_retry_queue(base_url, retry_queue):
    """Called periodically from the main loop. Tries every queued scan
    once; anything that still fails stays queued for next time."""
    if not retry_queue:
        return
    still_pending = []
    for tag_uid in retry_queue:
        result = try_post(base_url, tag_uid)
        if result is None:
            still_pending.append(tag_uid)
        else:
            print(f"[recovered] {describe(result)}")
    retry_queue[:] = still_pending


def run_hardware(base_url):
    from mfrc522 import SimpleMFRC522

    reader = SimpleMFRC522()
    last_seen = {}
    retry_queue = []
    last_queue_check = time.time()
    print("CrowPi reader running. Tap a tag on the scan pad. Ctrl+C to quit.")
    try:
        while True:
            uid, _ = reader.read()
            uid = str(uid)
            now = time.time()
            if now - last_seen.get(uid, 0) > DEBOUNCE_SECONDS:
                handle_scan(base_url, uid, retry_queue)
                last_seen[uid] = now

            if time.time() - last_queue_check > QUEUE_RETRY_SECONDS:
                drain_retry_queue(base_url, retry_queue)
                last_queue_check = time.time()
    except KeyboardInterrupt:
        print("\nStopped.")


def run_simulate(base_url):
    print("Simulate mode - no CrowPi hardware needed.")
    print("Type a fake tag UID and press enter to fake a scan (Ctrl+C to quit).")
    retry_queue = []
    try:
        while True:
            uid = input("tag_uid> ").strip()
            if uid:
                handle_scan(base_url, uid, retry_queue)
                drain_retry_queue(base_url, retry_queue)
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
