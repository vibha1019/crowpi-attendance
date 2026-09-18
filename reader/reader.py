"""CrowPi RFID reader service.

Runs on the CrowPi and posts every tag scan to a backend. Two backends are
supported:

  Standalone (default) - posts to this project's own Flask + SQLite
  backend at /api/scan. A brand new tag gets parked as "pending" for an
  admin to name in the web panel at <backend>/admin.

  OCS (--api-key given) - posts to the shared OCS system's
  /api/rfid/scan instead, authenticated with a shared API key (the device
  has no OCS login of its own). OCS is the source of truth here: it
  resolves the tag to a student and records the attendance event itself,
  this script is just the reading point. Tag registration for OCS mode
  happens separately, through OCS's own admin-authenticated
  /api/rfid/register endpoint, not through this script.

Either way, a scan that fails to reach the backend (wifi blip, backend
restarting) is retried a few times immediately, and if it still fails, gets
queued and retried in the background every few seconds until it goes
through, instead of just being silently dropped.

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


def try_post(target, tag_uid):
    """One attempt to post a scan. Returns the parsed response, or None if
    the backend could not be reached (network error, not an HTTP error)."""
    try:
        if target.get("api_key"):
            resp = requests.post(
                f"{target['base_url']}/api/rfid/scan",
                json={"tag_uid": tag_uid, "classroom_id": target["classroom_id"]},
                headers={"X-API-Key": target["api_key"]},
                timeout=3,
            )
        else:
            resp = requests.post(
                f"{target['base_url']}/api/scan", json={"tag_uid": tag_uid}, timeout=3
            )
        return resp.json()
    except requests.RequestException:
        return None


def post_scan_with_retry(target, tag_uid):
    """Try immediately, with a couple of quick retries, before giving up
    and letting the caller queue it for background retry."""
    result = try_post(target, tag_uid)
    if result is not None:
        return result

    for attempt, backoff in enumerate(IMMEDIATE_RETRY_BACKOFF, start=1):
        print(f"  backend unreachable, retrying ({attempt}/{IMMEDIATE_RETRIES})...")
        time.sleep(backoff)
        result = try_post(target, tag_uid)
        if result is not None:
            print("  reconnected.")
            return result

    return None


def describe(result):
    # OCS response shape
    if result.get("status") == "accepted":
        event = result["event"]
        return f"{event['user_name']}: {event['type']}"
    if result.get("status") == "unregistered":
        return f"{result['message']} - register it in OCS first"
    # Standalone backend response shape
    if result.get("status") == "pending":
        return f"New tag {result['tag_uid']} - go name it at /admin"
    if result.get("status") == "logged":
        return f"{result['student']}: {result['type']}"
    return f"Scan result: {result}"


def handle_scan(target, tag_uid, retry_queue):
    result = post_scan_with_retry(target, tag_uid)
    if result is None:
        print(f"Could not reach backend at {target['base_url']} - queued {tag_uid} for retry")
        retry_queue.append(tag_uid)
        return
    print(describe(result))


def drain_retry_queue(target, retry_queue):
    """Called periodically from the main loop. Tries every queued scan
    once; anything that still fails stays queued for next time."""
    if not retry_queue:
        return
    still_pending = []
    for tag_uid in retry_queue:
        result = try_post(target, tag_uid)
        if result is None:
            still_pending.append(tag_uid)
        else:
            print(f"[recovered] {describe(result)}")
    retry_queue[:] = still_pending


def run_hardware(target):
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
                handle_scan(target, uid, retry_queue)
                last_seen[uid] = now

            if time.time() - last_queue_check > QUEUE_RETRY_SECONDS:
                drain_retry_queue(target, retry_queue)
                last_queue_check = time.time()
    except KeyboardInterrupt:
        print("\nStopped.")


def run_simulate(target):
    print("Simulate mode - no CrowPi hardware needed.")
    print("Type a fake tag UID and press enter to fake a scan (Ctrl+C to quit).")
    retry_queue = []
    try:
        while True:
            uid = input("tag_uid> ").strip()
            if uid:
                handle_scan(target, uid, retry_queue)
                drain_retry_queue(target, retry_queue)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CrowPi RFID reader service")
    parser.add_argument("--simulate", action="store_true", help="Run without CrowPi hardware")
    parser.add_argument("--backend", default="http://localhost:5050", help="Backend base URL")
    parser.add_argument(
        "--api-key",
        default=None,
        help="OCS RFID_API_KEY. If given, posts to OCS's /api/rfid/scan instead of the standalone backend.",
    )
    parser.add_argument(
        "--classroom-id",
        type=int,
        default=None,
        help="OCS classroom id to log attendance against. Required with --api-key.",
    )
    args = parser.parse_args()

    if args.api_key and not args.classroom_id:
        parser.error("--classroom-id is required when --api-key is given")

    target = {
        "base_url": args.backend,
        "api_key": args.api_key,
        "classroom_id": args.classroom_id,
    }

    if args.simulate:
        run_simulate(target)
    else:
        try:
            run_hardware(target)
        except ImportError:
            print("mfrc522 library not available - falling back to simulate mode.")
            run_simulate(target)
