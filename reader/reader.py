"""CrowPi RFID reader service.

Runs on the CrowPi and posts every tag scan to OCS (flask_csh), which
resolves the tag to a student and records the attendance event itself.
This script is just the reading point. Tag registration happens
separately, through OCS's admin-authenticated /api/rfid/register endpoint.

Configuration comes from the environment (see reader.env.example; the
systemd unit loads reader.env via EnvironmentFile):

  BACKEND_URL    OCS base URL, e.g. http://192.168.1.242:8587
  RFID_API_KEY   shared device key, must match flask_csh's RFID_API_KEY
  CLASSROOM_ID   OCS classroom id to log attendance against
  DEVICE_ID      optional label for this reader, e.g. crowpi-room1

Every scan is stamped with the time it was tapped (occurred_at, UTC) and a
unique event_id on the CrowPi itself, so a scan that only reaches the
server later is still recorded at the right time, and a retry of a scan
the server already saw is not logged twice. The CrowPi's clock must be
NTP-synced for occurred_at to be trustworthy.

A scan that fails to reach the backend (wifi blip, backend restarting) is
retried a few times immediately, and if it still fails, gets queued. A
background thread retries the queue every few seconds, independent of new
taps. While anything is queued, new taps join the back of the queue so
scans always reach the server in the order they happened.

Pass --simulate to run without the CrowPi's RFID hardware attached (types a
fake tag UID instead of tapping a real card) so the pipeline can be tested
from a laptop before the CrowPi is set up.
"""

import argparse
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

DEBOUNCE_SECONDS = 2
IMMEDIATE_RETRY_BACKOFF = [0.5, 1, 2]  # seconds, one per retry
QUEUE_RETRY_SECONDS = 5


def utc_iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_scan(tag_uid):
    """Everything about a tap that has to be captured at tap time, not at
    send time."""
    return {"tag_uid": tag_uid, "occurred_at": utc_iso_now(), "event_id": str(uuid.uuid4())}


def try_post(config, scan):
    """One attempt to post a scan. Returns the parsed response, or None if
    it should be retried (network error or a 5xx from the server). A 4xx is
    a final answer (unregistered tag, bad key) and is returned as-is."""
    try:
        resp = requests.post(
            f"{config['base_url']}/api/rfid/scan",
            json={
                **scan,
                "classroom_id": config["classroom_id"],
                "device_id": config["device_id"],
            },
            headers={"X-API-Key": config["api_key"]},
            timeout=3,
        )
        if resp.status_code >= 500:
            return None
        return resp.json()
    except requests.RequestException:
        return None


def post_scan_with_retry(config, scan):
    """Try immediately, with a couple of quick retries, before giving up
    and letting the caller queue it for background retry."""
    result = try_post(config, scan)
    if result is not None:
        return result

    for attempt, backoff in enumerate(IMMEDIATE_RETRY_BACKOFF, start=1):
        print(f"  backend unreachable, retrying ({attempt}/{len(IMMEDIATE_RETRY_BACKOFF)})...")
        time.sleep(backoff)
        result = try_post(config, scan)
        if result is not None:
            print("  reconnected.")
            return result

    return None


def describe(result):
    if result.get("status") == "accepted":
        event = result["event"]
        suffix = " (already recorded)" if result.get("duplicate") else ""
        return f"{event['user_name']}: {event['type']}{suffix}"
    if result.get("status") == "unregistered":
        return f"{result['message']} - register it in OCS first"
    return f"Scan result: {result}"


class RetryQueue:
    """Scans that could not be delivered yet, drained by a background
    thread so recovery does not wait for the next tap."""

    def __init__(self, config):
        self.config = config
        self.items = []
        self.lock = threading.Lock()

    def has_items(self):
        with self.lock:
            return bool(self.items)

    def add(self, scan):
        with self.lock:
            self.items.append(scan)

    def drain(self):
        """Delivers queued scans in order, stopping at the first one that
        still fails so later scans never overtake earlier ones."""
        with self.lock:
            while self.items:
                result = try_post(self.config, self.items[0])
                if result is None:
                    return
                self.items.pop(0)
                print(f"[recovered] {describe(result)}")

    def run_forever(self):
        while True:
            time.sleep(QUEUE_RETRY_SECONDS)
            self.drain()

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()


def handle_scan(config, tag_uid, retry_queue):
    scan = make_scan(tag_uid)
    if retry_queue.has_items():
        print(f"Backend still unreachable - queued {tag_uid} behind earlier scans")
        retry_queue.add(scan)
        return
    result = post_scan_with_retry(config, scan)
    if result is None:
        print(f"Could not reach backend at {config['base_url']} - queued {tag_uid} for retry")
        retry_queue.add(scan)
        return
    print(describe(result))


def run_hardware(config, retry_queue):
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
                handle_scan(config, uid, retry_queue)
                last_seen[uid] = now
    except KeyboardInterrupt:
        print("\nStopped.")


def run_simulate(config, retry_queue):
    print("Simulate mode - no CrowPi hardware needed.")
    print("Type a fake tag UID and press enter to fake a scan (Ctrl+C to quit).")
    try:
        while True:
            uid = input("tag_uid> ").strip()
            if uid:
                handle_scan(config, uid, retry_queue)
    except KeyboardInterrupt:
        print("\nStopped.")


def load_config():
    missing = [name for name in ("BACKEND_URL", "RFID_API_KEY", "CLASSROOM_ID") if not os.environ.get(name)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)} (see reader.env.example)")
    return {
        "base_url": os.environ["BACKEND_URL"].rstrip("/"),
        "api_key": os.environ["RFID_API_KEY"],
        "classroom_id": int(os.environ["CLASSROOM_ID"]),
        "device_id": os.environ.get("DEVICE_ID") or None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CrowPi RFID reader service")
    parser.add_argument("--simulate", action="store_true", help="Run without CrowPi hardware")
    args = parser.parse_args()

    config = load_config()
    retry_queue = RetryQueue(config)
    retry_queue.start()

    if args.simulate:
        run_simulate(config, retry_queue)
    else:
        try:
            run_hardware(config, retry_queue)
        except ImportError:
            print("mfrc522 library not available - falling back to simulate mode.")
            run_simulate(config, retry_queue)
