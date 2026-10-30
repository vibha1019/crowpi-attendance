"""Tests for face_engine.py: the decision rule, and the OpenCV wiring with a
fake OpenCV. No OpenCV, models or webcam needed.

    cd camera && python3 -m unittest -v
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import face_engine
from camera_common import SetupError
from fake_cv2 import FakeCv2, FakeImage, FakeNumpy, photo_bytes
from reference_faces import ReferencePhoto


class DecisionTest(unittest.TestCase):
    ENROLLED = {"vibham": [], "kush1434": []}

    def decide(self, tapper, face_count, scores, threshold=0.363):
        return face_engine.decide_camera_result(tapper, self.ENROLLED, face_count, scores, threshold)

    def test_tapper_without_a_reference_face_is_not_enrolled(self):
        self.assertEqual(self.decide("optout", 1, {"vibham": 0.9}), ("not_enrolled", None, None))

    def test_no_face_in_picture(self):
        self.assertEqual(self.decide("vibham", 0, {}), ("no_face", None, None))

    def test_match_with_the_tapper(self):
        self.assertEqual(self.decide("vibham", 1, {"vibham": 0.71, "kush1434": 0.12}), ("match", "vibham", 0.71))

    def test_match_reports_who_was_seen_even_if_not_the_tapper(self):
        # A proxy tap: kush1434 taps vibham's tag. OCS turns this into Mismatch.
        self.assertEqual(self.decide("vibham", 1, {"vibham": 0.2, "kush1434": 0.8}), ("match", "kush1434", 0.8))

    def test_score_exactly_at_threshold_is_a_match(self):
        self.assertEqual(self.decide("vibham", 1, {"vibham": 0.363})[0], "match")

    def test_below_threshold_is_no_match_with_the_closest_score(self):
        self.assertEqual(self.decide("vibham", 1, {"vibham": 0.3, "kush1434": 0.1}), ("no_match", None, 0.3))

    def test_confidence_is_clamped_to_0_1(self):
        self.assertEqual(self.decide("vibham", 1, {"vibham": -0.2, "kush1434": -0.4}), ("no_match", None, 0.0))
        self.assertEqual(self.decide("vibham", 1, {"vibham": 1.2}), ("match", "vibham", 1.0))

    def test_nan_scores_never_match(self):
        self.assertEqual(self.decide("vibham", 1, {"vibham": float("nan")}), ("no_match", None, None))
        self.assertEqual(self.decide("vibham", 1, {"vibham": float("nan"), "kush1434": 0.8}),
                         ("match", "kush1434", 0.8))

    def test_confidence_is_a_float(self):
        _, _, confidence = self.decide("vibham", 1, {"vibham": 1})
        self.assertIs(type(confidence), float)


def make_engine(cv2=None):
    return face_engine.FaceEngine(cv2 or FakeCv2(), FakeNumpy(), "d.onnx", "r.onnx")


class FaceEngineTest(unittest.TestCase):
    def test_model_load_error_is_a_setup_error(self):
        cv2 = FakeCv2()
        cv2.fail_model_load = True
        with self.assertRaises(SetupError) as raised:
            make_engine(cv2)
        self.assertIn("get_models.sh", str(raised.exception))

    def test_unreadable_or_empty_bytes_decode_to_none(self):
        engine = make_engine()
        self.assertIsNone(engine.decode(b"not an image"))
        self.assertIsNone(engine.decode(b""))
        self.assertEqual(engine.decode(photo_bytes([[80, "vibham"]])), [(80, "vibham")])

    def test_big_pictures_are_shrunk_before_detection(self):
        cv2 = FakeCv2()
        engine = make_engine(cv2)
        small, faces = engine.find_faces(FakeImage([(400, "vibham")], (1080, 1920, 3)))  # the Brio at 1080p
        self.assertEqual(cv2.resized_to, [(1280, 720)])
        self.assertEqual(cv2.input_sizes[-1], (1280, 720))  # the detector is told the real size
        self.assertEqual(small.shape[:2], (720, 1280))
        self.assertEqual(len(faces), 1)

    def test_720p_frames_are_used_as_they_are(self):
        # Shrinking a webcam frame would leave too few pixels per face for SFace.
        cv2 = FakeCv2()
        engine = make_engine(cv2)
        for shape in ((720, 1280, 3), (480, 640, 3)):
            small, _ = engine.find_faces(FakeImage([], shape))
            self.assertEqual(small.shape, shape)
        self.assertEqual(cv2.resized_to, [])
        self.assertEqual(cv2.input_sizes, [(1280, 720), (640, 480)])

    def test_largest_face_is_the_closest_person(self):
        faces = [(0, 0, 40, 40, "far"), (0, 0, 120, 120, "near"), (0, 0, 80, 80, "middle")]
        self.assertEqual(face_engine.largest_face(faces)[4], "near")

    def test_scores_use_the_best_photo_of_each_person(self):
        cv2 = FakeCv2()
        engine = make_engine(cv2)
        cv2.match_scores = {("vibham", "vibham-old"): 0.4}
        gallery = {"vibham": [engine.embed(None, (0, 0, 1, 1, "vibham-old")),
                              engine.embed(None, (0, 0, 1, 1, "vibham"))],
                   "kush1434": [engine.embed(None, (0, 0, 1, 1, "kush1434"))]}
        seen = engine.embed(None, (0, 0, 1, 1, "vibham"))
        self.assertEqual(engine.scores(seen, gallery), {"vibham": 0.9, "kush1434": 0.1})


class GalleryTest(unittest.TestCase):
    def test_photos_are_grouped_by_uid_and_problems_reported(self):
        photos = [
            ReferencePhoto("vibham", "faces/vibham.jpg", photo_bytes([[80, "vibham"]])),
            ReferencePhoto("vibham", "faces/vibham_2.jpg", photo_bytes([[90, "vibham"]])),
            ReferencePhoto("kush1434", "Spring photo", photo_bytes([[70, "kush1434"], [20, "someone"]])),
            ReferencePhoto("blurry", "Spring photo", photo_bytes([])),
            ReferencePhoto("broken", "Spring photo", b"\xff\xd8 half a jpeg"),
        ]
        gallery, problems = face_engine.build_gallery(make_engine(), photos)

        self.assertEqual({uid: len(refs) for uid, refs in gallery.items()}, {"vibham": 2, "kush1434": 1})
        self.assertEqual(gallery["kush1434"][0].person, "kush1434")  # the largest face, not the one behind
        messages = dict(problems)
        self.assertIn("no face found", messages["blurry"])
        self.assertIn("not a readable image", messages["broken"])
        self.assertIn("2 faces", messages["kush1434"])

    def test_opencv_error_on_one_photo_is_reported_not_fatal(self):
        cv2 = FakeCv2()
        engine = make_engine(cv2)
        cv2.fail_detect = True
        gallery, problems = face_engine.build_gallery(
            engine, [ReferencePhoto("vibham", "Spring photo", photo_bytes([[80, "vibham"]]))])
        self.assertEqual(gallery, {})
        self.assertIn("OpenCV failed", problems[0][1])


class SetupTest(unittest.TestCase):
    def test_missing_models_message_has_the_zoo_urls_and_a_command(self):
        with self.assertRaises(SetupError) as raised:
            face_engine.require_model_files("/nonexistent/yunet.onnx", "/nonexistent/sface.onnx")
        message = str(raised.exception)
        self.assertIn("/nonexistent/yunet.onnx", message)
        self.assertIn("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
                      "face_detection_yunet_2023mar.onnx", message)
        self.assertIn("https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
                      "face_recognition_sface_2021dec.onnx", message)
        self.assertIn(os.path.join(face_engine.CAMERA_DIR, "get_models.sh"), message)

    def test_tiny_model_file_counts_as_missing(self):
        # e.g. a Git LFS pointer file saved instead of the real model
        with tempfile.TemporaryDirectory() as folder:
            pointer = os.path.join(folder, "yunet.onnx")
            real = os.path.join(folder, "sface.onnx")
            with open(pointer, "w") as f:
                f.write("version https://git-lfs.github.com/spec/v1\n")
            with open(real, "wb") as f:
                f.write(b"\0" * face_engine.MIN_MODEL_BYTES)
            with self.assertRaises(SetupError) as raised:
                face_engine.require_model_files(pointer, real)
            self.assertIn(pointer, str(raised.exception))
            self.assertNotIn(real, str(raised.exception))

    def test_missing_opencv_is_a_clear_setup_error(self):
        with mock.patch.dict(sys.modules, {"cv2": None}), self.assertRaises(SetupError) as raised:
            face_engine.load_opencv()
        self.assertIn("pip install -r requirements.txt", str(raised.exception))

    def test_modules_import_without_opencv_or_numpy(self):
        # sys.modules[...] = None makes that import fail, even if it is installed.
        code = ("import sys; sys.modules['cv2'] = None; sys.modules['numpy'] = None; "
                "import camera_client, camera_mode, face_engine, reference_faces, webcam; print('ok')")
        here = os.path.dirname(os.path.abspath(__file__))
        done = subprocess.run([sys.executable, "-c", code], cwd=here, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
