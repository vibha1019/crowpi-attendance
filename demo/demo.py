"""Camera check demo on one laptop, with no Raspberry Pi and no Mac Mini.

It starts three things and shows them on one web page:

  1. Vibha's presence backend (a flask_csh checkout) on 127.0.0.1, on a
     throwaway SQLite file, with a room, the PEOPLE below and their RFID
     tags, and a 60-minute demo period.
  2. The camera client in camera mode (camera/camera_client.py) on this
     laptop's webcam, with reference photos from camera/faces/.
  3. A page at http://127.0.0.1:8600 with a tap button per person in place
     of the RFID reader, and the live roster with each camera check.

Run it with the camera folder's Python, from a Terminal that may use the
camera (see the README's "Feature demo on a laptop"):

  camera/venv/bin/python demo/demo.py --flask-dir ../flask_csh_demo \\
      --flask-python ../flask_csh/venv/bin/python

The API key and teacher password are random for each run and never
printed. The database and the backend's log live in a temp folder that is
deleted on exit. Ctrl+C stops everything.
"""

import argparse
import collections
import http.server
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone

import requests

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
CAMERA_DIR = os.path.join(os.path.dirname(DEMO_DIR), "camera")
FACES_DIR = os.path.join(CAMERA_DIR, "faces")
PAGE_FILE = os.path.join(DEMO_DIR, "demo_page.html")
sys.path.insert(0, CAMERA_DIR)
from reference_faces import uid_from_photo_name  # noqa: E402

# (uid, name, RFID tag). Reference photos are camera/faces/<uid>.jpg.
PEOPLE = [
    ("kush", "Kush Shah", "DEMO0001"),
    ("vibha", "Vibha Mandayam", "DEMO0002"),
    ("ruta", "Ruta Sirdeshmukh", "DEMO0003"),
]
TEACHER_UID = "demo_teacher"
ROOM_NAME = "Camera Demo Room"
PERIOD_SECONDS = 3600
DEVICE_ID = "laptop-demo"
CAMERA_LINES_KEPT = 40
BACKEND_START_SECONDS = 60
REQUEST_SECONDS = 10


class DemoError(Exception):
    """A step of the demo setup failed; the message says which and why."""


def utc_iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def uids_with_photos(faces_dir):
    """The uids that have at least one reference photo in faces_dir."""
    if not os.path.isdir(faces_dir):
        return set()
    return {uid_from_photo_name(name) for name in os.listdir(faces_dir)} - {None}


# ---------------------------------------------------------------- the backend

class Backend:
    """flask_csh as a child process, on a free port and a temp database."""

    def __init__(self, flask_dir, flask_python, work_dir, env):
        self.flask_dir, self.flask_python, self.env = flask_dir, flask_python, env
        self.db_path = os.path.join(work_dir, "presence_demo.db")
        self.log_path = os.path.join(work_dir, "backend.log")
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc = None

    def start(self):
        print(f"Starting Vibha's presence backend from {self.flask_dir} ...", flush=True)
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [self.flask_python, os.path.join(DEMO_DIR, "flask_server.py"), self.flask_dir, self.db_path,
             str(self.port)], env=self.env, cwd=self.flask_dir, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + BACKEND_START_SECONDS
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise DemoError(f"The backend stopped while starting. Its last lines:\n{self.last_lines()}")
            try:
                requests.get(f"{self.base_url}/api/presence/rooms", timeout=1)
                return
            except requests.RequestException:
                time.sleep(0.5)
        raise DemoError(f"The backend did not start within {BACKEND_START_SECONDS} s. Its last lines:\n"
                        f"{self.last_lines()}")

    def last_lines(self, count=15):
        """The end of the backend's log, minus lines with login tokens."""
        self.log.flush()
        with open(self.log_path) as log:
            lines = [line.rstrip() for line in log if "token" not in line.lower() and "jwt" not in line.lower()]
        return "\n".join("  " + line for line in lines[-count:])

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc:
            self.log.close()


class Ocs:
    """The demo's calls to the backend: as the teacher (login cookie) for
    setup and the roster, and as the RFID reader (API key) for taps."""

    def __init__(self, base_url, api_key):
        self.base_url, self.api_key = base_url, api_key
        self.teacher = requests.Session()
        self.reader = requests.Session()  # no login cookie: taps authenticate with the API key only
        self.lock = threading.Lock()  # the page's requests come in on several threads

    def call(self, method, path, ok_codes, session=None, **kwargs):
        try:
            with self.lock:
                resp = (session or self.teacher).request(method, self.base_url + path, timeout=REQUEST_SECONDS,
                                                         **kwargs)
        except requests.RequestException as e:
            raise DemoError(f"{method} {path}: the backend did not answer ({type(e).__name__}).")
        if resp.status_code not in ok_codes:
            raise DemoError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def log_in(self, password):
        self.call("POST", "/api/authenticate", (200,), json={"uid": TEACHER_UID, "password": password})

    def set_up_room(self):
        """A room with every demo person as a member, and their tags. Returns the room id."""
        room = self.call("POST", "/api/presence/rooms", (201,), json={
            "name": ROOM_NAME, "description": "Laptop camera demo", "member_uids": [uid for uid, _, _ in PEOPLE]})
        for uid, _, tag in PEOPLE:
            self.call("POST", "/api/rfid/tags", (200, 201), json={"tag_uid": tag, "uid": uid})
        return room["id"]

    def start_period(self, room_id):
        # Grace as long as the period, so a first tap is always Present, never Tardy.
        return self.call("POST", f"/api/presence/rooms/{room_id}/periods", (201,), json={
            "name": "Camera demo", "duration_seconds": PERIOD_SECONDS, "grace_seconds": PERIOD_SECONDS})

    def status(self, room_id):
        return self.call("GET", f"/api/presence/rooms/{room_id}/status", (200,))

    def tap(self, room_id, tag):
        """What the Pi's reader sends for one tap (reader/reader.py)."""
        body = self.call("POST", "/api/rfid/scan", (200, 201), session=self.reader, headers={"X-API-Key": self.api_key},
                         json={"tag_uid": tag, "room_id": room_id, "event_id": str(uuid.uuid4()),
                               "occurred_at": utc_iso_now(), "device_id": DEVICE_ID})
        return body["event"]


# ---------------------------------------------------------------- the camera

class CameraClient:
    """camera_client.py --mode camera as a child process. Its output is
    echoed here with a [camera] prefix, and the last lines are kept for
    the page."""

    def __init__(self, env):
        self.env = env
        self.lines = collections.deque(maxlen=CAMERA_LINES_KEPT)
        self.lock = threading.Lock()
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-u", "camera_client.py", "--mode", "camera"], cwd=CAMERA_DIR,
                                     env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self.read_output, daemon=True).start()

    def read_output(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            print(f"[camera] {line}", flush=True)
            with self.lock:
                self.lines.append(line)

    def snapshot(self):
        with self.lock:
            lines = list(self.lines)
        exit_code = self.proc.poll()
        return {"running": exit_code is None, "exit_code": exit_code, "lines": lines}

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # lets it release the camera
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------- the page

class Demo:
    """What the page can ask for: the current state, a tap, a new period."""

    def __init__(self, ocs, room_id, camera):
        self.ocs, self.room_id, self.camera = ocs, room_id, camera
        self.tags = {uid: tag for uid, _, tag in PEOPLE}

    def state(self):
        on_file = uids_with_photos(FACES_DIR)
        return {
            "status": self.ocs.status(self.room_id),
            "people": [{"uid": uid, "name": name, "tag": tag, "face_on_file": uid in on_file}
                       for uid, name, tag in PEOPLE],
            "camera": self.camera.snapshot(),
        }

    def tap(self, uid):
        if uid not in self.tags:
            raise DemoError(f"{uid!r} is not in the demo")
        return self.ocs.tap(self.room_id, self.tags[uid])

    def new_period(self):
        return self.ocs.start_period(self.room_id)


class DemoPageHandler(http.server.BaseHTTPRequestHandler):
    demo = None  # set in main()

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_body(status, json.dumps(data).encode(), "application/json")

    def answer(self, action):
        try:
            self.send_json(200, action())
        except DemoError as e:
            self.send_json(502, {"error": str(e)})

    def do_GET(self):
        if self.path == "/":
            with open(PAGE_FILE, "rb") as page:
                self.send_body(200, page.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self.answer(self.demo.state)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json(400, {"error": "body must be JSON"})
        if self.path == "/api/tap":
            self.answer(lambda: self.demo.tap(str(data.get("uid"))))
        elif self.path == "/api/period":
            self.answer(self.demo.new_period)
        else:
            self.send_json(404, {"error": "not found"})

    def log_message(self, *args):
        pass  # the page asks for the state every second


# ---------------------------------------------------------------- setup

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Camera check demo on one laptop (see the README).")
    parser.add_argument("--flask-dir", required=True, help="A flask_csh checkout with Vibha's presence system")
    parser.add_argument("--flask-python", help="flask_csh's Python (default: <flask-dir>/venv/bin/python)")
    parser.add_argument("--camera-index", type=int, default=0, help="Which camera (see --list-cameras)")
    parser.add_argument("--port", type=int, default=8600, help="Port for the demo page")
    parser.add_argument("--no-browser", action="store_true", help="Don't open the page in a browser")
    args = parser.parse_args(argv)
    args.flask_dir = os.path.abspath(args.flask_dir)
    args.flask_python = os.path.abspath(args.flask_python or os.path.join(args.flask_dir, "venv", "bin", "python"))
    return args


def check_setup(args):
    """Stops with what to do if something the demo needs is missing."""
    if not os.path.isfile(os.path.join(args.flask_dir, "api", "presence_api.py")):
        sys.exit(f"{args.flask_dir} is not a flask_csh checkout with the presence system (no api/presence_api.py).")
    if not os.path.isfile(args.flask_python):
        sys.exit(f"No Python at {args.flask_python}. Pass --flask-python with flask_csh's venv Python.")
    if not uids_with_photos(FACES_DIR) & {uid for uid, _, _ in PEOPLE}:
        sys.exit(f"camera/faces/ has no reference photo for any of: {', '.join(uid for uid, _, _ in PEOPLE)}. "
                 "Take one first:\n"
                 "  cd camera && venv/bin/python camera_client.py --enroll kush")


def main(argv=None):
    args = parse_args(argv)
    check_setup(args)
    api_key, teacher_password = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    work_dir = tempfile.mkdtemp(prefix="presence-demo-")
    backend = Backend(args.flask_dir, args.flask_python, work_dir, dict(
        os.environ, RFID_API_KEY=api_key, DEMO_TEACHER_UID=TEACHER_UID, DEMO_TEACHER_PASSWORD=teacher_password,
        DEMO_PEOPLE=json.dumps([[uid, name] for uid, name, _ in PEOPLE]), PYTHONDONTWRITEBYTECODE="1"))
    try:
        page_server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), DemoPageHandler)
    except OSError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        sys.exit(f"Can't open the demo page on port {args.port} ({e}). Try --port 8601.")
    camera = None
    try:
        backend.start()
        ocs = Ocs(backend.base_url, api_key)
        ocs.log_in(teacher_password)
        room_id = ocs.set_up_room()
        ocs.start_period(room_id)
        print(f"Backend is up: room {room_id} with {', '.join(name for _, name, _ in PEOPLE)}, "
              f"and a {PERIOD_SECONDS // 60}-minute demo period.", flush=True)

        camera = CameraClient(dict(
            os.environ, BACKEND_URL=backend.base_url, RFID_API_KEY=api_key, ROOM_ID=str(room_id),
            FACE_SOURCE="folder", FACE_UIDS=",".join(uid for uid, _, _ in PEOPLE),
            CAMERA_INDEX=str(args.camera_index), DEVICE_ID=f"{DEVICE_ID}-cam", PYTHONDONTWRITEBYTECODE="1"))
        camera.start()

        DemoPageHandler.demo = Demo(ocs, room_id, camera)
        url = f"http://127.0.0.1:{args.port}"
        print(f"Demo page: {url}   (Ctrl+C stops everything)", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        page_server.serve_forever()
    except DemoError as e:
        print(f"Demo stopped: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        page_server.server_close()
        if camera:
            camera.stop()
        backend.stop()
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
