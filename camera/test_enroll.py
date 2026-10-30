"""Tests for enroll.py: which uids can be saved, where the photo goes, and
the enroll window (a fake OpenCV and a scripted camera, no webcam).

    cd camera && python3 -m unittest -v
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import camera_client
import enroll
import face_engine
from camera_common import SetupError
from fake_cv2 import FakeImage, FakeNumpy
from test_camera_mode import PreviewCv2, ScriptedGrabber


class EnrollCv2(PreviewCv2):
    """PreviewCv2 that also records saved pictures."""

    def __init__(self, keys):
        super().__init__(keys)
        self.saved = []

    def imwrite(self, path, image):
        self.saved.append((path, image))
        with open(path, "wb") as photo_file:
            photo_file.write(b"jpeg")
        return True


def frame(*faces):
    return FakeImage(faces, (720, 1280, 3))


class UidTest(unittest.TestCase):
    def test_plain_uids_are_accepted(self):
        for uid in ("kush", "kush1434", "vibha.m", "a-b_c"):
            self.assertEqual(enroll.check_uid(uid), uid)

    def test_uids_that_are_not_safe_file_names_are_refused(self):
        for uid in ("", "../kush", "a b", "kush/x", ".hidden", "x" * 65):
            with self.subTest(uid=uid), self.assertRaises(SetupError):
                enroll.check_uid(uid)

    def test_uid_that_would_read_back_as_another_uid_is_refused(self):
        with self.assertRaises(SetupError):
            enroll.check_uid("kush_2")  # kush_2.jpg is a second photo of kush


class PhotoPathTest(unittest.TestCase):
    def test_first_photo_then_numbered_extra_photos(self):
        with tempfile.TemporaryDirectory() as faces_dir:
            self.assertEqual(enroll.next_photo_path(faces_dir, "kush"), os.path.join(faces_dir, "kush.jpg"))
            open(os.path.join(faces_dir, "kush.jpg"), "wb").close()
            self.assertEqual(enroll.next_photo_path(faces_dir, "kush"), os.path.join(faces_dir, "kush_2.jpg"))
            open(os.path.join(faces_dir, "kush_2.jpg"), "wb").close()
            self.assertEqual(enroll.next_photo_path(faces_dir, "kush"), os.path.join(faces_dir, "kush_3.jpg"))

    def test_folder_that_does_not_exist_yet(self):
        with tempfile.TemporaryDirectory() as parent:
            faces_dir = os.path.join(parent, "faces")
            self.assertEqual(enroll.next_photo_path(faces_dir, "kush"), os.path.join(faces_dir, "kush.jpg"))


class EnrollWindowTest(unittest.TestCase):
    def run_window(self, frames, keys):
        self.cv2 = EnrollCv2(keys)
        engine = face_engine.FaceEngine(self.cv2, FakeNumpy(), "d.onnx", "r.onnx")
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.faces_dir = os.path.join(parent.name, "faces")
        log = io.StringIO()
        with redirect_stdout(log):
            path = enroll.run_enroll_window(engine, ScriptedGrabber(frames), self.faces_dir, "kush")
        return path, log.getvalue()

    def test_space_with_one_face_saves_the_picture_without_boxes(self):
        first = frame((300, "kush"))
        path, _ = self.run_window([first], keys=[enroll.SPACE])
        self.assertEqual(path, os.path.join(self.faces_dir, "kush.jpg"))
        self.assertEqual(len(self.cv2.saved), 1)
        saved_path, saved_image = self.cv2.saved[0]
        self.assertEqual(saved_path, path)
        self.assertIs(saved_image, first)  # the camera's picture, not the copy the box was drawn on
        self.assertIsNot(self.cv2.shown[0], first)

    def test_space_with_no_face_or_two_faces_saves_nothing(self):
        frames = [frame(), frame((300, "kush"), (200, "vibham")), frame()]
        path, output = self.run_window(frames, keys=[enroll.SPACE, enroll.SPACE, ord("q")])
        self.assertIsNone(path)
        self.assertEqual(self.cv2.saved, [])
        self.assertIn("Not saved (no face found: face the camera)", output)
        self.assertIn("Not saved (2 faces: only kush should be in view)", output)
        self.assertFalse(os.path.exists(self.faces_dir))

    def test_q_quits_without_saving(self):
        path, _ = self.run_window([frame((300, "kush"))], keys=[ord("q")])
        self.assertIsNone(path)
        self.assertEqual(self.cv2.saved, [])

    def test_other_keys_keep_the_window_open(self):
        path, _ = self.run_window([frame((300, "kush")), frame((300, "kush"))], keys=[-1, enroll.SPACE])
        self.assertEqual(len(self.cv2.shown), 2)
        self.assertEqual(path, os.path.join(self.faces_dir, "kush.jpg"))


class EnrollCliTest(unittest.TestCase):
    def test_enroll_needs_no_ocs_or_spring_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(enroll, "enroll") as take_photo:
            camera_client.main(["--enroll", "kush"])
        take_photo.assert_called_once_with("kush")

    def test_bad_uid_exits_before_the_camera_opens(self):
        with mock.patch.object(enroll, "start_face_engine") as start, self.assertRaises(SystemExit) as raised:
            camera_client.main(["--enroll", ""])
        start.assert_not_called()
        self.assertIn("Can't save a photo", raised.exception.code)

    def test_bad_camera_index_is_a_setup_error(self):
        with mock.patch.dict(os.environ, {"CAMERA_INDEX": "brio"}, clear=True), self.assertRaises(SetupError):
            enroll.camera_index_from_env()


if __name__ == "__main__":
    unittest.main()
