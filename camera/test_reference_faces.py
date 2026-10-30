"""Tests for reference_faces.py. A tiny fake Spring runs on localhost in a
thread, with the same cookie-only login as the real one. No network needed.

    cd camera && python3 -m unittest -v
"""

import base64
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from requests.cookies import MockRequest, MockResponse, RequestsCookieJar

import reference_faces
from camera_common import SetupError

PASSWORD = "s3cret-teacher-pw"
VIBHAM_PHOTO = b"\xff\xd8 vibham's jpeg"
KUSH_PHOTO = b"\xff\xd8 kush1434's jpeg"
ARNAV_PHOTO = b"\xff\xd8 arnav's jpeg"


def b64(data):
    return base64.b64encode(data).decode()


class FakeSpring:
    """Just enough of Spring: POST /authenticate sets the jwt_java_spring
    cookie (path /api), and GET /api/face/faces needs that cookie from a
    Teacher (401 without it, 403 for a student)."""

    TEACHER_TOKEN = "teacher-jwt"
    STUDENT_TOKEN = "student-jwt"

    LOCAL_COOKIE = "Path=/api; HttpOnly; SameSite=Lax"  # jwt.cookie.secure=false
    # What CookieFactory sends with the production settings (jwt.cookie.secure=true).
    PROD_COOKIE = "Path=/api; Domain=.opencodingsociety.com; Max-Age=604800; Secure; HttpOnly; SameSite=None"

    def __init__(self, faces):
        self.faces = faces
        self.cookie_attributes = self.LOCAL_COOKIE
        self.logins = []          # JSON bodies of every login
        self.face_list_cookies = []  # Cookie header of every face list request
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.make_handler())
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def make_handler(self):
        spring = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body, cookie=None):
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                if cookie:
                    self.send_header("Set-Cookie", f"jwt_java_spring={cookie}; {spring.cookie_attributes}")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                spring.logins.append(data)
                if self.path != "/authenticate":
                    return self.reply(404, "not found")
                if data == {"uid": "teacher", "password": PASSWORD}:
                    return self.reply(200, "teacher was authenticated successfully", spring.TEACHER_TOKEN)
                if data == {"uid": "student", "password": PASSWORD}:
                    return self.reply(200, "student was authenticated successfully", spring.STUDENT_TOKEN)
                return self.reply(401, "Authentication failed: INVALID_CREDENTIALS")

            def do_GET(self):
                if self.path != "/api/face/faces":
                    return self.reply(404, "not found")
                cookie = self.headers.get("Cookie") or ""
                spring.face_list_cookies.append(cookie)
                if f"jwt_java_spring={spring.TEACHER_TOKEN}" in cookie:
                    return self.reply(200, spring.faces)
                if f"jwt_java_spring={spring.STUDENT_TOKEN}" in cookie:
                    return self.reply(403, {"error": "Forbidden"})
                return self.reply(401, {"error": "Unauthorized"})

            def log_message(self, *args):
                pass  # keep test output clean

        return Handler


def spring_config(url, uid="teacher", password=PASSWORD, face_uids=None):
    return {"face_source": "spring", "spring_url": url, "spring_uid": uid, "spring_password": password,
            "face_uids": face_uids, "faces_dir": None}


class SpringTest(unittest.TestCase):
    def setUp(self):
        self.spring = FakeSpring([
            {"uid": "vibham", "name": "Vibha", "faceData": b64(VIBHAM_PHOTO)},
            {"uid": "kush1434", "name": "Kush", "faceData": "data:image/jpeg;base64," + b64(KUSH_PHOTO)},
            {"uid": "arnav", "name": "Arnav", "faceData": b64(ARNAV_PHOTO)},
            {"uid": "broken", "name": "Broken", "faceData": "not base64!!"},
        ])
        self.addCleanup(self.spring.stop)

    def load(self, **overrides):
        log = io.StringIO()
        with redirect_stdout(log):
            photos, problems = reference_faces.load_reference_photos(spring_config(self.spring.url, **overrides))
        self.assertNotIn(PASSWORD, log.getvalue())
        return photos, problems

    def test_logs_in_with_a_cookie_and_reads_every_face(self):
        photos, problems = self.load()

        self.assertEqual(self.spring.logins, [{"uid": "teacher", "password": PASSWORD}])
        self.assertIn(f"jwt_java_spring={FakeSpring.TEACHER_TOKEN}", self.spring.face_list_cookies[0])
        self.assertEqual({photo.uid: photo.image_bytes for photo in photos},
                         {"vibham": VIBHAM_PHOTO, "kush1434": KUSH_PHOTO, "arnav": ARNAV_PHOTO})
        self.assertEqual(problems, [("broken", "Spring photo: photo data is not valid base64")])

    def test_data_url_prefix_is_removed(self):
        photos, _ = self.load()
        kush = [photo for photo in photos if photo.uid == "kush1434"]
        self.assertEqual(kush[0].image_bytes, KUSH_PHOTO)

    def test_face_uids_keeps_only_those_people_and_reports_missing_ones(self):
        photos, problems = self.load(face_uids={"vibham", "kush1434", "nikhil"})
        self.assertEqual(sorted(photo.uid for photo in photos), ["kush1434", "vibham"])
        self.assertEqual(problems, [("nikhil", "listed in FACE_UIDS, but has no face registered in Spring")])

    def test_wrong_password_is_a_setup_error_without_the_password(self):
        with self.assertRaises(SetupError) as raised:
            self.load(password="wrong-pw")
        self.assertIn("SPRING_PASSWORD", str(raised.exception))
        self.assertNotIn("wrong-pw", str(raised.exception))
        self.assertEqual(self.spring.face_list_cookies, [])

    def test_student_account_is_told_it_needs_teacher_or_admin(self):
        with self.assertRaises(SetupError) as raised:
            self.load(uid="student")
        self.assertIn("Teacher or Admin", str(raised.exception))
        self.assertIn("FACE_SOURCE=folder", str(raised.exception))

    def test_local_spring_with_the_production_cookie_is_explained(self):
        # A local Spring on its default settings sends the cookie Secure and
        # for *.opencodingsociety.com only, so it can't be used from here.
        self.spring.cookie_attributes = FakeSpring.PROD_COOKIE
        with self.assertRaises(SetupError) as raised:
            self.load()
        self.assertIn("jwt.cookie.secure=false", str(raised.exception))
        self.assertNotIn(PASSWORD, str(raised.exception))

    def test_unreachable_spring_is_a_setup_error(self):
        with self.assertRaises(SetupError) as raised:
            reference_faces.load_reference_photos(spring_config("http://127.0.0.1:9"))
        self.assertIn("Could not reach Spring", str(raised.exception))
        self.assertNotIn(PASSWORD, str(raised.exception))

    def test_decode_face_data(self):
        self.assertEqual(reference_faces.decode_face_data(b64(b"abc")), b"abc")
        self.assertEqual(reference_faces.decode_face_data("data:image/png;base64," + b64(b"abc")), b"abc")
        self.assertEqual(reference_faces.decode_face_data(" " + b64(b"abc") + "\n"), b"abc")
        for bad in (None, "", "data:image/jpeg;base64,", "abc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reference_faces.decode_face_data(bad)


class ProductionCookieTest(unittest.TestCase):
    """The production Spring's cookie (Secure, Domain=.opencodingsociety.com,
    set by /authenticate for Path=/api) must come back on /api/face/faces,
    over https only. Checked with requests' own cookie jar, no network."""

    def jar_after_login(self, login_url):
        jar = RequestsCookieJar()
        raw = f"Set-Cookie: jwt_java_spring=t; {FakeSpring.PROD_COOKIE}\r\n\r\n".encode()
        headers = http.client.parse_headers(io.BytesIO(raw))
        jar.extract_cookies(MockResponse(headers), MockRequest(requests.Request("POST", login_url).prepare()))
        return jar

    def cookie_sent_to(self, jar, url):
        request = requests.Request("GET", url).prepare()
        request.prepare_cookies(jar)
        return request.headers.get("Cookie")

    def test_cookie_goes_back_to_the_face_list_over_https_only(self):
        jar = self.jar_after_login("https://spring.opencodingsociety.com/authenticate")
        self.assertIn("jwt_java_spring=t",
                      self.cookie_sent_to(jar, "https://spring.opencodingsociety.com/api/face/faces"))
        self.assertIsNone(self.cookie_sent_to(jar, "http://spring.opencodingsociety.com/api/face/faces"))


class FolderTest(unittest.TestCase):
    def make_folder(self, files):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, data in files.items():
            with open(os.path.join(folder.name, name), "wb") as f:
                f.write(data)
        return folder.name

    def config(self, faces_dir, face_uids=None):
        return {"face_source": "folder", "faces_dir": faces_dir, "face_uids": face_uids}

    def test_photos_are_named_by_uid(self):
        folder = self.make_folder({"vibham.jpg": VIBHAM_PHOTO, "vibham_2.PNG": b"second", "kush1434.jpeg": KUSH_PHOTO,
                                   "notes.txt": b"x", ".DS_Store": b"x"})
        photos, problems = reference_faces.load_reference_photos(self.config(folder))
        self.assertEqual([(photo.uid, photo.label) for photo in photos],
                         [("kush1434", "faces/kush1434.jpeg"), ("vibham", "faces/vibham.jpg"),
                          ("vibham", "faces/vibham_2.PNG")])
        self.assertEqual(photos[1].image_bytes, VIBHAM_PHOTO)
        self.assertEqual(problems, [])

    def test_face_uids_filter_applies_to_the_folder_too(self):
        folder = self.make_folder({"vibham.jpg": VIBHAM_PHOTO, "kush1434.jpg": KUSH_PHOTO})
        photos, problems = reference_faces.load_reference_photos(self.config(folder, {"vibham", "arnav"}))
        self.assertEqual([photo.uid for photo in photos], ["vibham"])
        self.assertEqual(problems, [("arnav", "listed in FACE_UIDS, but has no photo in faces/")])

    def test_missing_folder_is_a_setup_error(self):
        with self.assertRaises(SetupError):
            reference_faces.load_reference_photos(self.config("/nonexistent/faces"))

    def test_photo_file_names(self):
        cases = {"vibham.jpg": "vibham", "vibham_2.JPG": "vibham", "vibham_10.png": "vibham",
                 "kush1434.jpeg": "kush1434", "mr_smith.jpg": "mr_smith", "notes.txt": None, ".DS_Store": None}
        for name, uid in cases.items():
            with self.subTest(name=name):
                self.assertEqual(reference_faces.uid_from_photo_name(name), uid)


if __name__ == "__main__":
    unittest.main()
