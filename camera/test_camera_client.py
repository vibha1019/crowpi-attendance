"""Tests for the OCS side of the camera client: the Jarvis3000#17
contract, delivery, polling and the modes. No network, camera or OpenCV
needed: a tiny fake OCS runs on localhost in a thread.

    cd camera && python3 -m unittest -v
"""

import io
import json
import os
import re
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import camera_client
from camera_client import Answer

API_KEY = "test-key"
ROOM_ID = 1
PENDING_PATH = f"/api/presence/rooms/{ROOM_ID}/taps/pending"
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
NO_BACKOFF = [0, 0, 0]


def iso_z_seconds_ago(seconds):
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_tap(uid="vibham", seconds_ago=1):
    return {"tap_event_id": str(uuid.uuid4()), "uid": uid,
            "occurred_at": iso_z_seconds_ago(seconds_ago), "type": "enter"}


class FakeBackend:
    """Just enough of flask_csh's presence API: the pending-taps list, and
    POST /api/presence/event with record_camera_result()'s rules, in its
    order: same event_id -> 200 duplicate, unknown room -> 404, unknown tap
    -> 404, second result for a tap -> 409, bad result -> 400, match on an
    unknown uid -> 404 unregistered, confidence outside 0..1 -> 400, bad
    occurred_at -> 400. (Every fake tap is in ROOM_ID.)"""

    USERS = {"vibham", "kush1434"}

    def __init__(self):
        self.taps = []
        self.redirect_gets = False  # answer every GET with a redirect
        self.checks = {}         # tap_event_id -> event_id of its stored result
        self.stored_events = set()
        self.posts = []          # (headers, raw body text) of every POST
        self.fail_posts = 0      # answer this many POSTs with 500 first
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.make_handler())
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def posted_bodies(self):
        return [json.loads(body) for _, body in self.posts]

    def handle_get(self, path, headers):
        if headers.get("X-API-Key") != API_KEY:
            return 401, {"message": "Invalid or missing API key"}
        if path != PENDING_PATH:
            return 404, {"message": "no room"}
        with self.lock:
            return 200, [tap for tap in self.taps if tap["tap_event_id"] not in self.checks]

    def handle_post(self, path, headers, raw):
        with self.lock:
            self.posts.append((dict(headers), raw))
            if headers.get("X-API-Key") != API_KEY:
                return 401, {"message": "Invalid or missing API key"}
            if path != "/api/presence/event":
                return 404, {"message": "not found"}
            if self.fail_posts > 0:
                self.fail_posts -= 1
                return 500, {"message": "simulated server error"}
            data = json.loads(raw)
            if data["event_id"] in self.stored_events:
                return 200, {"status": "accepted", "duplicate": True, "check": {"verification": "NO_FACE"}}
            if data.get("room_id") != ROOM_ID or type(data.get("room_id")) is not int:
                return 404, {"message": f"no room {data.get('room_id')}"}
            if data.get("tap_event_id") not in {tap["tap_event_id"] for tap in self.taps}:
                return 404, {"message": "no tap with that event_id"}
            if data["tap_event_id"] in self.checks:
                return 409, {"message": "tap already has a camera result", "status": "already_checked"}
            if data.get("result") not in ("match", "no_match", "no_face", "not_enrolled"):
                return 400, {"message": "result must be one of match, no_match, no_face, not_enrolled"}
            if data["result"] == "match" and data.get("uid") not in self.USERS:
                return 404, {"message": f"no user with uid {data.get('uid')}", "status": "unregistered"}
            confidence = data.get("confidence")
            if confidence is not None and not (type(confidence) in (int, float) and 0 <= confidence <= 1):
                return 400, {"message": "confidence must be a number from 0 to 1"}
            try:
                when = datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError, KeyError):
                return 400, {"message": "occurred_at must be ISO 8601"}
            if when.tzinfo is None:
                return 400, {"message": "occurred_at needs a timezone"}
            self.stored_events.add(data["event_id"])
            self.checks[data["tap_event_id"]] = data["event_id"]
            return 200, {"status": "accepted", "duplicate": False, "check": {"verification": "NO_FACE"}}

    def make_handler(self):
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def send_json(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if backend.redirect_gets:
                    self.send_response(302)
                    self.send_header("Location", self.path)  # back here, so following it would loop
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_json(*backend.handle_get(self.path, self.headers))

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                self.send_json(*backend.handle_post(self.path, self.headers, raw))

            def log_message(self, *args):
                pass  # keep test output clean

        return Handler


class BackendTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.addCleanup(self.backend.stop)
        self.config = {"base_url": self.backend.base_url, "api_key": API_KEY,
                       "room_id": ROOM_ID, "device_id": "camera-test-tool"}
        self.outbox = camera_client.Outbox(self.config)
        patcher = mock.patch.object(camera_client, "IMMEDIATE_RETRY_BACKOFF", NO_BACKOFF)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_client(self, decide=camera_client.decide_no_face, ask_again_on_unknown_uid=False):
        return camera_client.CameraClient(self.config, decide, self.outbox, ask_again_on_unknown_uid)

    def poll(self, client):
        """One poll, with the log captured and returned."""
        log = io.StringIO()
        with redirect_stdout(log):
            client.poll_once()
        return log.getvalue()


class PayloadShapeTest(BackendTestCase):
    def test_no_face_result_on_the_wire_matches_the_contract(self):
        tap = make_tap()
        self.backend.taps.append(tap)
        self.poll(self.make_client())

        self.assertEqual(len(self.backend.posts), 1)
        headers, raw = self.backend.posts[0]
        self.assertEqual(headers["X-API-Key"], API_KEY)
        self.assertEqual(headers["Content-Type"], "application/json")
        body = json.loads(raw)
        self.assertEqual(set(body), {"event_id", "source", "room_id", "tap_event_id",
                                     "occurred_at", "result", "device_id"})
        self.assertEqual(body["source"], "camera")
        self.assertEqual(body["result"], "no_face")
        self.assertEqual(body["tap_event_id"], tap["tap_event_id"])
        self.assertEqual(body["device_id"], "camera-test-tool")
        self.assertIs(type(body["room_id"]), int)
        self.assertEqual(body["room_id"], ROOM_ID)
        self.assertEqual(uuid.UUID(body["event_id"]).version, 4)
        self.assertRegex(body["occurred_at"], ISO_Z)

    def test_camera_match_on_the_wire_carries_uid_and_float_confidence(self):
        tap = make_tap("vibham")
        self.backend.taps.append(tap)
        answer = Answer("match", "kush1434", camera_client.utc_iso_now(), 0.71)  # someone else's face
        log = self.poll(self.make_client(lambda tap: answer))

        body = self.backend.posted_bodies()[0]
        self.assertEqual(set(body), {"event_id", "source", "room_id", "tap_event_id", "occurred_at",
                                     "result", "uid", "confidence", "device_id"})
        self.assertEqual((body["result"], body["uid"]), ("match", "kush1434"))
        self.assertIs(type(body["confidence"]), float)
        self.assertEqual(body["confidence"], 0.71)
        self.assertEqual(body["occurred_at"], answer.taken_at)  # when the picture was taken
        self.assertIn("match (kush1434, score 0.71)", log)
        self.assertEqual(self.backend.checks[tap["tap_event_id"]], body["event_id"])  # stored

    def test_no_match_carries_a_confidence_but_no_uid(self):
        answer = Answer("no_match", "closest", camera_client.utc_iso_now(), 0.2)
        body = camera_client.make_result(self.config, "tap-1", answer)
        self.assertNotIn("uid", body)
        self.assertEqual(body["confidence"], 0.2)

    def test_match_carries_uid_and_never_a_confidence(self):
        answer = Answer("match", "vibham", camera_client.utc_iso_now())
        body = camera_client.make_result(self.config, "tap-1", answer)

        self.assertEqual(set(body), {"event_id", "source", "room_id", "tap_event_id", "occurred_at",
                                     "result", "uid", "device_id"})
        self.assertEqual(body["uid"], "vibham")
        self.assertRegex(body["occurred_at"], ISO_Z)

    def test_uid_is_only_sent_for_a_match(self):
        answer = Answer("no_match", "someone", camera_client.utc_iso_now())
        body = camera_client.make_result(self.config, "tap-1", answer)
        self.assertNotIn("uid", body)

    def test_each_result_gets_its_own_event_id(self):
        answer = camera_client.decide_no_face({})
        first = camera_client.make_result(self.config, "tap-1", answer)
        second = camera_client.make_result(self.config, "tap-2", answer)
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_bad_answers_are_refused_before_sending(self):
        now = camera_client.utc_iso_now()
        for answer in (Answer("maybe", None, now), Answer("match", None, now),
                       Answer("match", "vibham", now, 1.5), Answer("no_match", None, now, -0.1)):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                camera_client.make_result(self.config, "tap-1", answer)


class DeliveryTest(BackendTestCase):
    def test_event_id_is_reused_when_a_failed_post_is_retried(self):
        self.backend.taps.append(make_tap())
        self.backend.fail_posts = 1
        self.poll(self.make_client())

        event_ids = [body["event_id"] for body in self.backend.posted_bodies()]
        self.assertEqual(len(event_ids), 2)
        self.assertEqual(event_ids[0], event_ids[1])
        self.assertFalse(self.outbox.has_items())

    def test_queued_result_keeps_its_event_id_and_tap_is_answered_once(self):
        tap = make_tap()
        self.backend.taps.append(tap)
        self.backend.fail_posts = 1 + len(NO_BACKOFF)  # the first try and every quick retry fail
        decide = mock.Mock(side_effect=camera_client.decide_no_face)
        client = self.make_client(decide)

        log = self.poll(client)
        self.assertIn("queued result", log)
        self.assertTrue(self.outbox.has_items())

        # The tap is still pending on the server, but must not be answered again.
        self.poll(client)
        self.poll(client)
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(len(self.backend.posts), 1 + len(NO_BACKOFF))

        with redirect_stdout(io.StringIO()):
            self.outbox.drain()
        self.assertFalse(self.outbox.has_items())
        event_ids = {body["event_id"] for body in self.backend.posted_bodies()}
        self.assertEqual(len(event_ids), 1)
        self.assertEqual(self.backend.checks[tap["tap_event_id"]], event_ids.pop())

    def test_409_already_checked_counts_as_done(self):
        tap = make_tap()
        self.backend.taps.append(tap)
        self.backend.checks[tap["tap_event_id"]] = "someone-elses-result"
        payload = camera_client.make_result(self.config, tap["tap_event_id"], camera_client.decide_no_face(tap))

        log = io.StringIO()
        with redirect_stdout(log):
            self.make_client().deliver(payload)

        self.assertIn("already had a camera result", log.getvalue())
        self.assertFalse(self.outbox.has_items())
        self.assertEqual(len(self.backend.posts), 1)

    def test_duplicate_counts_as_done(self):
        tap = make_tap()
        self.backend.taps.append(tap)
        payload = camera_client.make_result(self.config, tap["tap_event_id"], camera_client.decide_no_face(tap))
        client = self.make_client()

        log = io.StringIO()
        with redirect_stdout(log):
            client.deliver(payload)
            client.deliver(payload)  # e.g. the first reply was lost on the way back

        self.assertIn("(already recorded)", log.getvalue())
        self.assertFalse(self.outbox.has_items())
        self.assertEqual(len(self.backend.posts), 2)

    def test_unknown_uid_asks_about_the_tap_again_in_rehearsal(self):
        tap = make_tap("vibham")
        self.backend.taps.append(tap)
        replies = iter(["o", "kushh1434", "o", "kush1434"])  # a typo, then the right uid
        decide = mock.Mock(side_effect=camera_client.RehearsalDecider(lambda prompt: next(replies)).decide)
        client = self.make_client(decide, ask_again_on_unknown_uid=True)

        log = self.poll(client)
        self.assertIn("REJECTED (404)", log)
        self.assertIn("will ask about tap", log)
        self.poll(client)

        self.assertEqual(decide.call_count, 2)
        stored = [body for body in self.backend.posted_bodies()
                  if body["event_id"] == self.backend.checks[tap["tap_event_id"]]]
        self.assertEqual(stored[0]["uid"], "kush1434")

    def test_unknown_uid_is_not_asked_again_in_camera_mode(self):
        # The camera would see the same face again and get the same 404.
        self.backend.taps.append(make_tap("vibham"))
        answer = Answer("match", "spring-only-uid", camera_client.utc_iso_now(), 0.7)
        decide = mock.Mock(return_value=answer)
        client = self.make_client(decide)

        log = self.poll(client) + self.poll(client)
        self.assertEqual(decide.call_count, 1)
        self.assertIn("has a face in Spring but no user in OCS", log)
        self.assertIn("stays Unverified", log)

    def test_fake_backend_checks_the_fields_like_the_real_one(self):
        # So a broken payload would fail these tests, not the demo.
        tap = make_tap()
        self.backend.taps.append(tap)
        good = camera_client.make_result(self.config, tap["tap_event_id"], camera_client.decide_no_face(tap))
        for change in ({"result": "maybe"}, {"occurred_at": "yesterday"}, {"occurred_at": "2026-10-21T18:42:00"}):
            with self.subTest(change=change):
                payload = dict(good, event_id=str(uuid.uuid4()), **change)
                self.assertEqual(camera_client.try_post(self.config, payload)[0], 400)
        self.assertEqual(camera_client.try_post(self.config, good)[0], 200)

    def test_other_rejections_are_final_and_logged(self):
        payload = camera_client.make_result(self.config, "no-such-tap", camera_client.decide_no_face({}))
        log = io.StringIO()
        with redirect_stdout(log):
            self.make_client().deliver(payload)
        self.assertIn("REJECTED (404)", log.getvalue())
        self.assertFalse(self.outbox.has_items())


class PollingTest(BackendTestCase):
    def test_tap_is_answered_only_once_across_polls(self):
        self.backend.taps.append(make_tap())
        decide = mock.Mock(side_effect=camera_client.decide_no_face)
        client = self.make_client(decide)
        for _ in range(3):
            self.poll(client)
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(len(self.backend.posts), 1)

    def test_stale_taps_are_dropped_and_logged(self):
        stale, fresh = make_tap("old", seconds_ago=30), make_tap("new", seconds_ago=1)
        self.backend.taps.extend([stale, fresh])
        decide = mock.Mock(side_effect=camera_client.decide_no_face)
        client = self.make_client(decide)

        log = self.poll(client)
        self.poll(client)  # logged once, not every poll

        self.assertEqual(log.count("Skipping tap"), 1)
        self.assertIn("too late", log)
        decide.assert_called_once_with(fresh)
        self.assertEqual([body["tap_event_id"] for body in self.backend.posted_bodies()],
                         [fresh["tap_event_id"]])

    def test_no_face_mode_answers_every_tap_with_no_face(self):
        taps = [make_tap("vibham"), make_tap("kush1434")]
        self.backend.taps.extend(taps)
        self.poll(self.make_client())

        bodies = self.backend.posted_bodies()
        self.assertEqual([body["tap_event_id"] for body in bodies], [tap["tap_event_id"] for tap in taps])
        self.assertTrue(all(body["result"] == "no_face" for body in bodies))
        self.assertTrue(all("uid" not in body and "confidence" not in body for body in bodies))

    def test_unreachable_backend_is_logged_once_then_reconnect(self):
        client = self.make_client()
        reachable_url = self.config["base_url"]
        self.config["base_url"] = "http://127.0.0.1:9"  # nothing listens on the discard port
        log = self.poll(client) + self.poll(client)
        self.assertEqual(log.count("Cannot get pending taps"), 1)

        self.config["base_url"] = reachable_url
        self.assertIn("Connected to", self.poll(client))

    def test_unexpected_error_skips_that_tap_and_answers_the_next(self):
        broken, fine = make_tap("vibham"), make_tap("kush1434")
        self.backend.taps.extend([broken, fine])

        def decide(tap):
            if tap["tap_event_id"] == broken["tap_event_id"]:
                raise RuntimeError("simulated numpy surprise")
            return camera_client.decide_no_face(tap)

        with redirect_stderr(io.StringIO()) as traceback_log:
            log = self.poll(self.make_client(decide))
        self.assertIn(f"No result for tap {broken['tap_event_id'][:8]}: unexpected RuntimeError", log)
        self.assertIn("simulated numpy surprise", traceback_log.getvalue())
        self.assertEqual([body["tap_event_id"] for body in self.backend.posted_bodies()],
                         [fine["tap_event_id"]])

    def test_end_of_keyboard_input_still_stops_rehearsal(self):
        self.backend.taps.append(make_tap())
        with self.assertRaises(EOFError):
            self.poll(self.make_client(mock.Mock(side_effect=EOFError)))

    def test_camera_error_skips_that_tap_and_answers_the_next(self):
        broken, fine = make_tap("vibham"), make_tap("kush1434")
        self.backend.taps.extend([broken, fine])

        def decide(tap):
            if tap["tap_event_id"] == broken["tap_event_id"]:
                raise camera_client.CameraError("no new picture from the camera")
            return camera_client.decide_no_face(tap)

        log = self.poll(self.make_client(decide))
        self.assertIn(f"No result for tap {broken['tap_event_id'][:8]}", log)
        self.assertEqual([body["tap_event_id"] for body in self.backend.posted_bodies()],
                         [fine["tap_event_id"]])

    def test_unknown_room_is_a_setup_error(self):
        self.config["room_id"] = 99
        with self.assertRaises(camera_client.SetupError) as raised:
            camera_client.fetch_pending_taps(self.config)
        self.assertIn("404", str(raised.exception))

    def test_redirect_is_not_followed(self):
        self.backend.redirect_gets = True
        with self.assertRaises(camera_client.SetupError) as raised:
            camera_client.fetch_pending_taps(self.config)
        self.assertIn("redirected", str(raised.exception))

    def test_bad_api_key_is_a_setup_error(self):
        self.config["api_key"] = "wrong"
        with self.assertRaises(camera_client.SetupError):
            camera_client.fetch_pending_taps(self.config)

    def test_wrong_api_key_is_never_printed(self):
        secret = "wrong-but-secret-key"
        env = {"BACKEND_URL": self.backend.base_url, "RFID_API_KEY": secret, "ROOM_ID": str(ROOM_ID)}
        log = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(log), \
                self.assertRaises(SystemExit) as raised:
            camera_client.main(["--mode", "no-face", "--once"])
        self.assertIn("401", str(raised.exception.code))
        self.assertNotIn(secret, str(raised.exception.code) + log.getvalue())


class RehearsalTest(BackendTestCase):
    def scripted(self, *replies):
        replies = iter(replies)
        return lambda prompt: next(replies)

    def test_each_key_gives_its_result(self):
        tap = make_tap("vibham")
        cases = [
            (["m"], "match", "vibham"),
            (["o", "kush1434"], "match", "kush1434"),
            (["n"], "no_match", None),
            (["f"], "no_face", None),
            (["e"], "not_enrolled", None),
            (["x", "", "o", "", "F"], "no_face", None),  # bad input re-prompts
        ]
        for replies, result, uid in cases:
            with self.subTest(replies=replies), redirect_stdout(io.StringIO()):
                answer = camera_client.RehearsalDecider(self.scripted(*replies)).decide(tap)
            self.assertEqual((answer.result, answer.uid), (result, uid))
            self.assertRegex(answer.taken_at, ISO_Z)

    def test_cli_rehearsal_once_shows_banner_and_posts_typed_result(self):
        tap = make_tap("vibham")
        self.backend.taps.append(tap)
        env = {"BACKEND_URL": self.backend.base_url, "RFID_API_KEY": API_KEY,
               "ROOM_ID": str(ROOM_ID), "DEVICE_ID": ""}
        log = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("builtins.input", self.scripted("m")), \
                redirect_stdout(log):
            camera_client.main(["--mode", "rehearsal", "--once"])

        self.assertIn("NOT face recognition", log.getvalue())
        self.assertIn("Tap by vibham", log.getvalue())
        body = self.backend.posted_bodies()[0]
        self.assertEqual((body["result"], body["uid"]), ("match", "vibham"))
        self.assertEqual(body["device_id"], "camera-test-tool-rehearsal")  # typed, not recognized
        self.assertIs(type(body["room_id"]), int)
        self.assertNotIn("confidence", body)

    def test_rehearsal_refuses_a_non_local_backend(self):
        # Typed results must never reach the classroom server, where the
        # dashboard would show them like real camera results.
        env = {"BACKEND_URL": "http://192.168.1.242:8587", "RFID_API_KEY": API_KEY,
               "ROOM_ID": str(ROOM_ID)}
        fetch = mock.Mock(return_value=[])
        log = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(camera_client, "fetch_pending_taps", fetch), \
                redirect_stdout(log), self.assertRaises(SystemExit) as raised:
            camera_client.main(["--mode", "rehearsal", "--once"])

        self.assertIn("local OCS", str(raised.exception.code))
        self.assertNotIn("REHEARSAL MODE", log.getvalue())
        fetch.assert_not_called()

    def test_only_local_hosts_count_as_local(self):
        for url in ("http://localhost:8587", "http://127.0.0.1:8587", "http://[::1]:8587"):
            with self.subTest(url=url):
                camera_client.require_local_backend(url)  # no error
        for url in ("http://192.168.1.242:8587", "https://flask.opencodingsociety.com", "http://localhost.evil.com"):
            with self.subTest(url=url), self.assertRaises(camera_client.SetupError):
                camera_client.require_local_backend(url)


OCS_ENV = {"BACKEND_URL": "http://ocs", "RFID_API_KEY": API_KEY, "ROOM_ID": str(ROOM_ID)}


class SetupTest(unittest.TestCase):
    def test_room_id_must_be_a_whole_number(self):
        env = dict(OCS_ENV, ROOM_ID="abc")
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(camera_client.SetupError):
            camera_client.load_config("camera")

    def test_classroom_id_is_still_read_as_a_fallback(self):
        # Like reader.py, so an old camera.env keeps working.
        env = {"BACKEND_URL": "http://ocs", "RFID_API_KEY": API_KEY, "CLASSROOM_ID": "3"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(camera_client.load_config("camera")["room_id"], 3)

    def test_api_key_is_trimmed_and_hidden_characters_are_refused(self):
        with mock.patch.dict(os.environ, dict(OCS_ENV, RFID_API_KEY=" test-key\r\n"), clear=True):
            self.assertEqual(camera_client.load_config("camera")["api_key"], "test-key")
        for bad in ("sec ret-key", "sec\nret-key", "sec\x1bret-key", "   "):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, dict(OCS_ENV, RFID_API_KEY=bad), clear=True), \
                    self.assertRaises(camera_client.SetupError) as raised:
                camera_client.load_config("camera")
            self.assertIn("RFID_API_KEY", str(raised.exception))
            self.assertNotIn("ret-key", str(raised.exception))

    def test_missing_room_id_is_named(self):
        env = {"BACKEND_URL": "http://ocs", "RFID_API_KEY": API_KEY}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(camera_client.SetupError) as raised:
            camera_client.load_config("camera")
        self.assertIn("ROOM_ID", str(raised.exception))

    def test_device_id_default_depends_on_the_mode(self):
        with mock.patch.dict(os.environ, OCS_ENV, clear=True):
            self.assertEqual(camera_client.load_config("camera")["device_id"], "mac-mini-cam")
            self.assertEqual(camera_client.load_config("no-face")["device_id"], "camera-test-tool")
            self.assertEqual(camera_client.load_config("rehearsal")["device_id"], "camera-test-tool")
        with mock.patch.dict(os.environ, dict(OCS_ENV, DEVICE_ID="x" * 80), clear=True):
            self.assertEqual(len(camera_client.load_config("camera")["device_id"]), 64)  # the server's limit

    def test_unknown_mode_fails_fast(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            camera_client.main(["--mode", "webcam"])
        self.assertEqual(raised.exception.code, 2)
        with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            camera_client.build_decider("webcam")  # never falls through to another mode

    def test_exactly_one_mode_or_setup_check(self):
        for argv in ([], ["--mode", "no-face", "--preview"], ["--check-faces", "--list-cameras"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                camera_client.main(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_setup_checks_need_no_ocs_settings(self):
        for flag, function in (("--list-cameras", "list_cameras"), ("--check-faces", "check_faces"),
                               ("--preview", "preview")):
            with self.subTest(flag=flag), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(camera_client.camera_mode, function) as check:
                camera_client.main([flag])
            check.assert_called_once_with()

    def test_setup_check_problem_exits_with_its_message(self):
        problem = camera_client.SetupError("Could not open camera 3.")
        with mock.patch.object(camera_client.camera_mode, "list_cameras", side_effect=problem), \
                self.assertRaises(SystemExit) as raised:
            camera_client.main(["--list-cameras"])
        self.assertEqual(raised.exception.code, "Could not open camera 3.")


class CameraModeCliTest(BackendTestCase):
    """--mode camera wiring, with the camera part replaced by a fake."""

    def run_camera_mode(self, decider):
        env = {"BACKEND_URL": self.backend.base_url, "RFID_API_KEY": API_KEY, "ROOM_ID": str(ROOM_ID)}
        log = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(log), \
                mock.patch.object(camera_client.camera_mode, "build_camera_decider", return_value=decider):
            camera_client.main(["--mode", "camera", "--once"])
        return log.getvalue()

    def test_camera_mode_posts_the_recognized_result_and_releases_the_camera(self):
        tap = make_tap("vibham")
        self.backend.taps.append(tap)
        decider = mock.Mock()
        decider.decide.return_value = Answer("match", "vibham", camera_client.utc_iso_now(), 0.82)

        log = self.run_camera_mode(decider)

        decider.decide.assert_called_once_with(tap)
        decider.close.assert_called_once_with()
        body = self.backend.posted_bodies()[0]
        self.assertEqual((body["result"], body["uid"], body["confidence"]), ("match", "vibham", 0.82))
        self.assertEqual(body["device_id"], "mac-mini-cam")
        self.assertIs(type(body["room_id"]), int)
        self.assertIn("watching room 1", log)

    def test_camera_setup_problem_stops_before_polling(self):
        fetch = mock.Mock(return_value=[])
        env = {"BACKEND_URL": self.backend.base_url, "RFID_API_KEY": API_KEY, "ROOM_ID": str(ROOM_ID)}
        problem = camera_client.SetupError("Face model file(s) missing")
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(camera_client, "fetch_pending_taps", fetch), \
                mock.patch.object(camera_client.camera_mode, "build_camera_decider", side_effect=problem), \
                self.assertRaises(SystemExit) as raised:
            camera_client.main(["--mode", "camera", "--once"])
        self.assertIn("missing", str(raised.exception.code))
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
