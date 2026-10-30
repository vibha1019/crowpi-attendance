"""Camera client for the Classroom Presence System (Jarvis3000#17).

Runs on the Mac Mini at the tap station, next to the Raspberry Pi:

  1. About once a second, GET /api/presence/rooms/<ROOM_ID>/taps/pending
     for taps that have no camera result yet.
  2. Pick one result per tap: match, no_match, no_face or not_enrolled.
  3. POST it to /api/presence/event as source "camera".

How the result is picked depends on --mode:

  camera     the real thing: face recognition with OpenCV on this
             machine's webcam (see camera_mode.py). Pictures stay in
             memory and are never saved or sent; only the result goes to
             OCS.
  no-face    answers every tap with no_face, without a camera. Checks OCS
             and the API key from this machine.
  rehearsal  a person types each result, to practice the demo scenarios in
             crowpi-attendance#13 against a local, throwaway OCS. It refuses
             any other server. This is NOT face recognition; never present
             these results as recognition.

Setup checks for camera mode (no OCS needed): --list-cameras,
--check-faces and --preview. --enroll UID takes a reference photo with
the webcam into camera/faces/, for FACE_SOURCE=folder (see enroll.py).

Configuration comes from the environment (see camera.env.example):

  BACKEND_URL    OCS base URL, e.g. http://192.168.1.242:8587
  RFID_API_KEY   shared device key, must match flask_csh's RFID_API_KEY
  ROOM_ID        OCS presence room whose taps to answer (CLASSROOM_ID is
                 still read as a fallback, like the reader)
  DEVICE_ID      optional label; default mac-mini-cam in camera mode and
                 camera-test-tool in the test modes

Each result gets its occurred_at (when the picture was taken, UTC) and a
fresh event_id before it is first sent. Retries reuse that event_id, so
the server never stores one result twice. Delivery works like
reader/reader.py: a few quick retries, then an in-memory outbox that a
background thread retries every few seconds, in order.
"""

import argparse
import os
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, timezone

import requests

import camera_mode
import enroll
from camera_common import LOCAL_HOSTS, RESULTS, Answer, CameraError, SetupError, utc_iso_now

POLL_SECONDS = 1.0
# Jarvis3000#17 asks for a result within ~10 s of the tap; after that the
# student has likely walked off and the next one may be at the table, so a
# picture could flag the wrong person. Older taps are skipped.
STALE_TAP_SECONDS = 8
IMMEDIATE_RETRY_BACKOFF = [0.5, 1, 2]  # seconds, one per retry
QUEUE_RETRY_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 3

MODES = ("camera", "no-face", "rehearsal")
# Results from the test modes must be easy to tell apart from real camera
# results in the database.
DEFAULT_DEVICE_IDS = {"camera": "mac-mini-cam", "no-face": "camera-test-tool", "rehearsal": "camera-test-tool"}
MAX_DEVICE_ID_LENGTH = 64

REHEARSAL_BANNER = """
====================================================================
 REHEARSAL MODE: a person types each result at this keyboard, to
 practice the demo on a local, throwaway OCS only.
 This is NOT face recognition. Never present these results as
 face recognition, in a demo or anywhere else.
====================================================================
"""


class BackendUnavailable(Exception):
    """OCS could not be reached or had a server error. Worth retrying."""


def tap_age_seconds(tap):
    """Seconds since the tap happened. Raises ValueError if its occurred_at
    is missing or not ISO 8601 with a timezone."""
    occurred_at = datetime.fromisoformat(str(tap.get("occurred_at") or "").replace("Z", "+00:00"))
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at has no timezone")
    return (datetime.now(timezone.utc) - occurred_at).total_seconds()


def make_result(config, tap_event_id, answer):
    """The POST body for one answer. The event_id is created here, once,
    and reused by every retry of this result."""
    if answer.result not in RESULTS:
        raise ValueError(f"result must be one of {RESULTS}, got {answer.result!r}")
    if answer.result == "match" and not answer.uid:
        raise ValueError("a match needs the uid of the person seen")
    if answer.confidence is not None and not 0 <= answer.confidence <= 1:
        raise ValueError(f"confidence must be from 0 to 1, got {answer.confidence!r}")

    payload = {
        "event_id": str(uuid.uuid4()),
        "source": "camera",
        "room_id": config["room_id"],
        "tap_event_id": tap_event_id,
        "occurred_at": answer.taken_at,
        "result": answer.result,
        "device_id": config["device_id"],
    }
    if answer.result == "match":
        payload["uid"] = answer.uid
    if answer.confidence is not None:
        payload["confidence"] = float(answer.confidence)
    return payload


def response_body(resp):
    """The JSON object a response carried, or its text as a message."""
    try:
        body = resp.json()
    except ValueError:
        return {"message": resp.text[:200]}
    return body if isinstance(body, dict) else {"message": str(body)[:200]}


def fetch_pending_taps(config):
    """Taps from the last ~15 s with no camera result yet. Raises
    BackendUnavailable if OCS can't be reached right now, and SetupError for
    answers retrying won't fix (bad key, unknown room)."""
    url = f"{config['base_url']}/api/presence/rooms/{config['room_id']}/taps/pending"
    try:
        # Redirects are not followed: requests would pass X-API-Key on to wherever they point.
        resp = requests.get(url, headers={"X-API-Key": config["api_key"]}, timeout=REQUEST_TIMEOUT_SECONDS,
                            allow_redirects=False)
    except requests.RequestException as e:
        raise BackendUnavailable(type(e).__name__)
    if resp.status_code >= 500:
        raise BackendUnavailable(f"{resp.status_code}: {response_body(resp).get('message')}")
    if 300 <= resp.status_code < 400:
        raise SetupError(f"OCS redirected the pending-taps request ({resp.status_code}) to "
                         f"{resp.headers.get('Location')}. Set BACKEND_URL to the address it points to.")
    if resp.status_code != 200:
        raise SetupError(f"OCS refused the pending-taps request ({resp.status_code}): "
                         f"{response_body(resp).get('message')}")
    try:
        taps = resp.json()
    except ValueError:
        raise BackendUnavailable("pending taps response was not JSON")
    if not isinstance(taps, list):
        raise BackendUnavailable("pending taps response was not a list")
    return taps


def try_post(config, payload):
    """One attempt to post a result. Returns (status_code, body), or None if
    it should be retried (network error or a 5xx from the server). A 4xx is
    a final answer (409 already_checked, unknown tap, bad key) and is
    returned as-is."""
    try:
        resp = requests.post(
            f"{config['base_url']}/api/presence/event",
            json=payload,
            headers={"X-API-Key": config["api_key"]},
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        return None
    if resp.status_code >= 500:
        return None
    return resp.status_code, response_body(resp)


def post_result_with_retry(config, payload):
    """Try immediately, with a couple of quick retries, before giving up
    and letting the caller queue it for background retry."""
    outcome = try_post(config, payload)
    if outcome is not None:
        return outcome

    for attempt, backoff in enumerate(IMMEDIATE_RETRY_BACKOFF, start=1):
        print(f"  backend unreachable, retrying ({attempt}/{len(IMMEDIATE_RETRY_BACKOFF)})...")
        time.sleep(backoff)
        outcome = try_post(config, payload)
        if outcome is not None:
            print("  reconnected.")
            return outcome

    return None


def describe(payload, outcome):
    """One log line for a delivered result. 200 (new or duplicate) and 409
    already_checked both mean the tap is done; any other 4xx is a final
    rejection that retrying won't fix."""
    status, body = outcome
    label = f"tap {payload['tap_event_id'][:8]}: {payload['result']}"
    details = [payload["uid"]] if payload.get("uid") else []
    if payload.get("confidence") is not None:
        details.append(f"score {payload['confidence']:.2f}")
    if details:
        label += f" ({', '.join(details)})"
    if status == 200:
        suffix = " (already recorded)" if body.get("duplicate") else ""
        verification = (body.get("check") or {}).get("verification", "accepted")
        return f"{label} -> {verification}{suffix}"
    if status == 409:
        return f"{label} not stored: that tap already had a camera result (is another camera client running?)"
    return f"{label} REJECTED ({status}): {body.get('message')}"


class Outbox:
    """Results that could not be delivered yet, drained by a background
    thread so recovery does not wait for the next tap."""

    def __init__(self, config):
        self.config = config
        self.items = []
        self.lock = threading.Lock()

    def has_items(self):
        with self.lock:
            return bool(self.items)

    def add(self, payload):
        with self.lock:
            self.items.append(payload)

    def drain(self):
        """Delivers queued results in order, stopping at the first one that
        still fails so later results never overtake earlier ones."""
        with self.lock:
            while self.items:
                outcome = try_post(self.config, self.items[0])
                if outcome is None:
                    return
                payload = self.items.pop(0)
                print(f"[recovered] {describe(payload, outcome)}")

    def run_forever(self):
        while True:
            time.sleep(QUEUE_RETRY_SECONDS)
            self.drain()

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()


class CameraClient:
    """Polls OCS for pending taps and answers each tap exactly once, using
    decide(tap) -> Answer to pick the result.

    ask_again_on_unknown_uid: after OCS says a matched uid does not exist,
    ask about that tap again on the next poll. Only rehearsal mode wants
    this (to fix a typo); the camera would just see the same face again."""

    def __init__(self, config, decide, outbox, ask_again_on_unknown_uid=False):
        self.config = config
        self.decide = decide
        self.outbox = outbox
        self.ask_again_on_unknown_uid = ask_again_on_unknown_uid
        # Every tap picked up so far, answered or skipped. A tap stays on the
        # pending list until its result is stored, so without this a queued
        # result would be answered again under a new event_id.
        self.seen_taps = set()
        self.backend_up = None  # unknown until the first poll

    def poll_once(self):
        try:
            taps = fetch_pending_taps(self.config)
        except BackendUnavailable as e:
            if self.backend_up is not False:  # log once per outage, not every poll
                print(f"Cannot get pending taps from {self.config['base_url']} ({e}) - will keep trying")
            self.backend_up = False
            return
        if self.backend_up is not True:
            print(f"Connected to {self.config['base_url']}, watching room {self.config['room_id']}.")
            self.backend_up = True
        for tap in taps:
            self.handle_tap(tap)

    def handle_tap(self, tap):
        tap_event_id = str(tap.get("tap_event_id") or "")
        if not tap_event_id or tap_event_id in self.seen_taps:
            return
        self.seen_taps.add(tap_event_id)
        short_id = tap_event_id[:8]

        try:
            age = tap_age_seconds(tap)
        except ValueError:
            print(f"Skipping tap {short_id}: unreadable occurred_at {tap.get('occurred_at')!r}")
            return
        if age > STALE_TAP_SECONDS:
            print(f"Skipping tap {short_id} by {tap.get('uid')}: {age:.0f}s old, too late to answer. "
                  "If every tap is skipped, check that this machine's and the Pi's clocks are NTP-synced.")
            return

        try:
            answer = self.decide(tap)
        except CameraError as e:
            # No result is better than a wrong one: the dashboard shows Unverified.
            print(f"No result for tap {short_id}: {e}")
            return
        except EOFError:
            raise  # rehearsal mode: the keyboard input ended, so stop
        except Exception as e:
            # A surprise from OpenCV, numpy or a bug must not stop the demo:
            # this tap stays Unverified and the next tap is still answered.
            print(f"No result for tap {short_id}: unexpected {type(e).__name__}: {e}")
            traceback.print_exc()
            return
        self.deliver(make_result(self.config, tap_event_id, answer))

    def deliver(self, payload):
        short_id = payload["tap_event_id"][:8]
        if self.outbox.has_items():
            print(f"Backend still unreachable - queued result for tap {short_id} behind earlier results")
            self.outbox.add(payload)
            return
        outcome = post_result_with_retry(self.config, payload)
        if outcome is None:
            print(f"Could not reach backend at {self.config['base_url']} - queued result for tap {short_id}")
            self.outbox.add(payload)
            return
        print(describe(payload, outcome))
        status, body = outcome
        if status == 404 and body.get("status") == "unregistered":
            # Unknown uid: a typo in rehearsal, or a Spring uid that OCS does
            # not have. Nothing was stored.
            if self.ask_again_on_unknown_uid:
                print(f"  OCS has no user {payload.get('uid')!r} - will ask about tap {short_id} again")
                self.seen_taps.discard(payload["tap_event_id"])
            else:
                print(f"  {payload.get('uid')!r} has a face in Spring but no user in OCS at "
                      f"{self.config['base_url']}; tap {short_id} stays Unverified")

    def run(self, poll_seconds):
        while True:
            self.poll_once()
            time.sleep(poll_seconds)


def decide_no_face(tap):
    """no-face mode: checks OCS and the API key, no camera involved."""
    return Answer("no_face", None, utc_iso_now())


class RehearsalDecider:
    """rehearsal mode: a person at the keyboard types each result, to
    practice the crowpi-attendance#13 demo scenarios. No confidence is
    ever sent, since nothing was recognized."""

    PROMPT = "[m] match tapper  [o] match other uid  [n] no_match  [f] no_face  [e] not_enrolled > "
    SIMPLE_CHOICES = {"n": "no_match", "f": "no_face", "e": "not_enrolled"}

    def __init__(self, ask=None):
        self.ask = ask or input

    def decide(self, tap):
        tapper = tap.get("uid")
        print(f"\nTap by {tapper} ({tap.get('type')}) at {tap.get('occurred_at')}")
        while True:
            choice = self.ask(self.PROMPT).strip().lower()
            if choice == "m" and tapper:
                return Answer("match", tapper, utc_iso_now())
            if choice == "o":
                other = self.ask("uid of the person you see > ").strip()
                if other:
                    return Answer("match", other, utc_iso_now())
            elif choice in self.SIMPLE_CHOICES:
                return Answer(self.SIMPLE_CHOICES[choice], None, utc_iso_now())
            print("Type m, o, n, f or e.")


def clean_api_key(raw_key):
    """The key without stray spaces or line endings around it. A key with
    hidden characters inside would make the HTTP library fail, and its
    error message would print the key, so it is refused here, before any
    request, with a message that does not show it."""
    api_key = raw_key.strip()
    if not api_key or not api_key.isprintable() or any(ch.isspace() for ch in api_key):
        raise SetupError("RFID_API_KEY is empty, or has spaces or hidden characters inside it. "
                         "Retype it in camera.env.")
    return api_key


def load_config(mode):
    """OCS settings from the environment. DEVICE_ID's default depends on
    the mode, so test results can be told apart from real ones."""
    room_id = os.environ.get("ROOM_ID") or os.environ.get("CLASSROOM_ID")
    missing = [name for name in ("BACKEND_URL", "RFID_API_KEY") if not os.environ.get(name)]
    if not room_id:
        missing.append("ROOM_ID")
    if missing:
        raise SetupError(f"Missing required environment variables: {', '.join(missing)} (see camera.env.example)")
    try:
        room_id = int(room_id)
    except ValueError:
        raise SetupError(f"ROOM_ID must be a whole number, got {room_id!r}")
    device_id = os.environ.get("DEVICE_ID") or DEFAULT_DEVICE_IDS[mode]
    return {
        "base_url": os.environ["BACKEND_URL"].rstrip("/"),
        "api_key": clean_api_key(os.environ["RFID_API_KEY"]),
        "room_id": room_id,
        "device_id": device_id[:MAX_DEVICE_ID_LENGTH],
    }


def require_local_backend(base_url):
    """Rehearsal results are typed by a person, but the dashboard shows them
    like real camera results. Keeping them on a local OCS means they can
    never reach the classroom server."""
    host = urllib.parse.urlparse(base_url).hostname
    if host not in LOCAL_HOSTS:
        raise SetupError(f"Rehearsal mode only runs against a local OCS (e.g. http://localhost:8587), "
                         f"not {host!r}. Typed results must never reach the classroom server.")


def build_decider(mode):
    """(decide, close) for a --mode. close releases the camera, if any."""
    if mode == "camera":
        decider = camera_mode.build_camera_decider()
        return decider.decide, decider.close
    if mode == "no-face":
        print("No-face mode: answering every tap with no_face "
              "(checks OCS and the API key from this machine; no camera is used).")
        return decide_no_face, lambda: None
    if mode == "rehearsal":
        print(REHEARSAL_BANNER)
        return RehearsalDecider().decide, lambda: None
    raise ValueError(f"unknown mode {mode!r}")


def run_setup_check(args):
    """--list-cameras, --check-faces, --preview or --enroll. None of them use OCS."""
    try:
        if args.list_cameras:
            camera_mode.list_cameras()
        elif args.check_faces:
            camera_mode.check_faces()
        elif args.enroll is not None:  # "" too, so check_uid rejects it
            enroll.enroll(args.enroll)
        else:
            camera_mode.preview()
    except SetupError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        print("\nStopped.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Camera client for the Classroom Presence System (Jarvis3000#17). "
                    "Pick one --mode, or one of the setup checks.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--mode", choices=MODES,
                        help="camera: face recognition on this machine's webcam (the real thing). "
                             "no-face: answer every tap with no_face, to check OCS and the API key. "
                             "rehearsal: a person types each result, local OCS only (NOT face recognition).")
    action.add_argument("--list-cameras", action="store_true", help="Show which camera numbers work, then exit")
    action.add_argument("--check-faces", action="store_true",
                        help="Load the reference faces and say whose are usable, then exit")
    action.add_argument("--preview", action="store_true",
                        help="Live window with face boxes and scores, to aim the camera and tune the threshold")
    action.add_argument("--enroll", metavar="UID",
                        help="Take a reference photo of UID with the webcam into camera/faces/ (FACE_SOURCE=folder)")
    parser.add_argument("--once", action="store_true", help="Answer the taps pending right now, then exit")
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS, help="How often to ask for new taps")
    args = parser.parse_args(argv)

    if not args.mode:
        run_setup_check(args)
        return

    try:
        config = load_config(args.mode)
        if args.mode == "rehearsal":
            require_local_backend(config["base_url"])
            # Stored results must show a person typed them; nothing was recognized.
            config["device_id"] = config["device_id"][:MAX_DEVICE_ID_LENGTH - len("-rehearsal")] + "-rehearsal"
        decide, close = build_decider(args.mode)
    except SetupError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:  # e.g. while camera mode is still loading faces
        sys.exit("\nStopped.")

    outbox = Outbox(config)
    client = CameraClient(config, decide, outbox, ask_again_on_unknown_uid=args.mode == "rehearsal")
    try:
        if args.once:
            client.poll_once()
        else:
            outbox.start()
            client.run(args.poll_seconds)
    except SetupError as e:
        sys.exit(str(e))
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
    finally:
        close()

    if outbox.has_items():
        sys.exit(f"{len(outbox.items)} result(s) were never delivered.")
    if args.once and client.backend_up is False:
        sys.exit("Could not reach OCS.")


if __name__ == "__main__":
    main()
