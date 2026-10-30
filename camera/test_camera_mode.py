"""Tests for camera_mode.py: settings, the per-tap decision over several
frames, and the setup checks. A fake OpenCV and a scripted camera stand in
for the real ones, so no OpenCV, models or webcam are needed.

    cd camera && python3 -m unittest -v
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import camera_mode
import face_engine
from camera_common import CameraError, SetupError
from fake_cv2 import FakeCv2, FakeImage, FakeNumpy, photo_bytes

SPRING_ENV = {"SPRING_UID": "teacher", "SPRING_PASSWORD": "pw"}


class ConfigTest(unittest.TestCase):
    def load(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return camera_mode.load_camera_config()

    def assert_refused(self, **env):
        with self.assertRaises(SetupError) as raised:
            self.load(**env)
        return str(raised.exception)

    def test_defaults(self):
        config = self.load(**SPRING_ENV)
        self.assertEqual(config["camera_index"], 0)
        self.assertEqual(config["match_threshold"], 0.363)
        self.assertEqual(config["face_wait_seconds"], 3.0)
        self.assertEqual(config["face_source"], "spring")
        self.assertEqual(config["spring_url"], "https://spring.opencodingsociety.com")
        self.assertIsNone(config["face_uids"])
        self.assertEqual(config["detector_model"],
                         os.path.join(face_engine.CAMERA_DIR, "models", "face_detection_yunet_2023mar.onnx"))
        self.assertEqual(config["recognizer_model"],
                         os.path.join(face_engine.CAMERA_DIR, "models", "face_recognition_sface_2021dec.onnx"))

    def test_model_paths_count_from_the_camera_folder(self):
        config = self.load(FACE_DETECTOR_MODEL="my/yunet.onnx", FACE_RECOGNIZER_MODEL="/abs/sface.onnx", **SPRING_ENV)
        self.assertEqual(config["detector_model"], os.path.join(face_engine.CAMERA_DIR, "my", "yunet.onnx"))
        self.assertEqual(config["recognizer_model"], "/abs/sface.onnx")

    def test_face_uids_is_a_comma_separated_list(self):
        self.assertEqual(self.load(FACE_UIDS=" vibham, kush1434,,", **SPRING_ENV)["face_uids"],
                         {"vibham", "kush1434"})
        self.assertIsNone(self.load(FACE_UIDS=" , ", **SPRING_ENV)["face_uids"])

    def test_bad_numbers_are_refused(self):
        for env in ({"MATCH_THRESHOLD": "nan"}, {"MATCH_THRESHOLD": "0"}, {"MATCH_THRESHOLD": "1.5"},
                    {"MATCH_THRESHOLD": "abc"}, {"FACE_WAIT_SECONDS": "-1"}, {"FACE_WAIT_SECONDS": "30"},
                    {"FACE_WAIT_SECONDS": "nan"}, {"CAMERA_INDEX": "-1"}, {"CAMERA_INDEX": "one"}):
            with self.subTest(env=env):
                self.assert_refused(**env, **SPRING_ENV)
        self.assertEqual(self.load(MATCH_THRESHOLD="0.5", FACE_WAIT_SECONDS="0", **SPRING_ENV)["match_threshold"], 0.5)

    def test_face_source_must_be_spring_or_folder(self):
        self.assert_refused(FACE_SOURCE="camera-roll")
        self.assertEqual(self.load(FACE_SOURCE="Folder")["face_source"], "folder")  # no Spring login needed

    def test_spring_needs_a_login(self):
        message = self.assert_refused(SPRING_UID="teacher")
        self.assertIn("SPRING_PASSWORD", message)

    def test_spring_password_only_goes_over_https_unless_local(self):
        message = self.assert_refused(SPRING_URL="http://spring.opencodingsociety.com", **SPRING_ENV)
        self.assertIn("https://", message)
        self.assertNotIn("pw", message.replace("https", ""))
        self.assertEqual(self.load(SPRING_URL="http://localhost:8585/", **SPRING_ENV)["spring_url"],
                         "http://localhost:8585")


class ScriptedGrabber:
    """Stands in for FrameGrabber: hands out the given frames one per call,
    then None, like a camera that stopped."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.number = 0
        self.timeouts = []
        self.stopped = False

    def latest_number(self):
        return self.number

    def wait_for_frame_after(self, frame_number, timeout):
        assert frame_number == self.number, "always asks for a frame newer than the last one it saw"
        self.timeouts.append(timeout)
        if not self.frames:
            return None
        self.number += 1
        return self.number, self.frames.pop(0), f"2026-10-21T18:42:0{self.number}.000Z"

    def stop(self):
        self.stopped = True


def picture(*faces):
    return FakeImage(faces, (720, 1280, 3))


class CameraDeciderTest(unittest.TestCase):
    def make_decider(self, frames, face_wait_seconds=3.0, problems=(), face_uids=None):
        self.cv2 = FakeCv2()
        engine = face_engine.FaceEngine(self.cv2, FakeNumpy(), "d.onnx", "r.onnx")
        gallery = {"vibham": [engine.embed(None, (0, 0, 1, 1, "vibham"))],
                   "kush1434": [engine.embed(None, (0, 0, 1, 1, "kush1434"))]}
        self.grabber = ScriptedGrabber(frames)
        return camera_mode.CameraDecider(engine, gallery, self.grabber, 0.363, face_wait_seconds,
                                         problems, face_uids)

    def decide(self, decider, uid="vibham"):
        with redirect_stdout(io.StringIO()):
            return decider.decide({"uid": uid, "type": "enter", "occurred_at": "2026-10-21T18:42:00.000Z"})

    def decide_with_log(self, decider, uid):
        log = io.StringIO()
        with redirect_stdout(log):
            answer = decider.decide({"uid": uid, "type": "enter", "occurred_at": "2026-10-21T18:42:00.000Z"})
        return answer, log.getvalue()

    def test_keeps_looking_until_a_face_shows_up(self):
        answer = self.decide(self.make_decider([picture(), picture(), picture((300, "vibham"))]))
        self.assertEqual((answer.result, answer.uid, answer.confidence), ("match", "vibham", 0.9))
        self.assertEqual(answer.taken_at, "2026-10-21T18:42:03.000Z")  # when the deciding picture was taken

    def test_no_face_after_waiting(self):
        answer = self.decide(self.make_decider([picture(), picture(), picture()]))
        self.assertEqual((answer.result, answer.uid, answer.confidence), ("no_face", None, None))
        self.assertEqual(answer.taken_at, "2026-10-21T18:42:03.000Z")

    def test_a_match_decides_at_once(self):
        decider = self.make_decider([picture((300, "vibham")), picture()])
        self.assertEqual(self.decide(decider).result, "match")
        self.assertEqual(len(self.grabber.frames), 1)  # the second frame was never needed

    def test_a_poor_look_is_followed_by_a_better_one(self):
        # e.g. looking down at the pad, then up
        decider = self.make_decider([picture((300, "vibham-looking-down")), picture((300, "vibham"))])
        self.cv2.match_scores = {("vibham-looking-down", "vibham"): 0.25}
        self.assertEqual(self.decide(decider)[:2], ("match", "vibham"))

    def test_stranger_is_no_match_with_the_closest_score(self):
        decider = self.make_decider([picture((300, "stranger")), picture(), picture((300, "stranger2"))])
        self.cv2.match_scores = {("stranger", "vibham"): 0.3, ("stranger2", "kush1434"): 0.2}
        answer = self.decide(decider)
        self.assertEqual((answer.result, answer.uid, answer.confidence), ("no_match", None, 0.3))
        self.assertEqual(answer.taken_at, "2026-10-21T18:42:01.000Z")  # the picture that gave 0.3

    def test_someone_else_is_reported_by_uid(self):
        # A proxy tap: kush1434's face, the closest one, at vibham's tap.
        answer = self.decide(self.make_decider([picture((100, "vibham"), (300, "kush1434"))]))
        self.assertEqual((answer.result, answer.uid), ("match", "kush1434"))

    def test_someone_else_waits_for_the_deadline(self):
        # A match on someone else does not end the look early.
        decider = self.make_decider([picture((300, "kush1434")), picture(), picture()])
        self.assertEqual(self.decide(decider)[:2], ("match", "kush1434"))
        self.assertEqual(len(self.grabber.frames), 0)

    def test_the_tapper_seen_later_beats_a_bystander_seen_first(self):
        # vibham taps while looking down; only kush1434, behind her, is found
        # at first. Then she looks up and is the largest face.
        decider = self.make_decider([picture((100, "kush1434")), picture((300, "vibham"), (100, "kush1434"))])
        self.assertEqual(self.decide(decider)[:2], ("match", "vibham"))

    def test_the_tapper_seen_later_beats_someone_else_seen_first(self):
        # A look down at the pad can score just above the threshold against a teammate.
        decider = self.make_decider([picture((300, "vibham-down")), picture((300, "vibham"))])
        self.cv2.match_scores = {("vibham-down", "kush1434"): 0.40, ("vibham-down", "vibham"): 0.30}
        self.assertEqual(self.decide(decider)[:2], ("match", "vibham"))

    def test_a_match_on_someone_else_beats_a_stranger(self):
        decider = self.make_decider([picture((300, "kush1434")), picture((300, "stranger"))])
        self.cv2.match_scores = {("stranger", "vibham"): 0.3}
        self.assertEqual(self.decide(decider)[:2], ("match", "kush1434"))

    def test_not_enrolled_tapper_is_never_photographed(self):
        decider = self.make_decider([picture((300, "optout"))])
        answer = self.decide(decider, uid="optout")
        self.assertEqual((answer.result, answer.uid, answer.confidence), ("not_enrolled", None, None))
        self.assertEqual(self.grabber.timeouts, [])

    def test_uid_with_other_capitals_is_warned_about(self):
        answer, log = self.decide_with_log(self.make_decider([]), "Vibham")
        self.assertEqual(answer.result, "not_enrolled")
        self.assertIn("must match exactly", log)

    def test_not_enrolled_says_why(self):
        problems = [("arnav", "Spring photo: no face found in it (retake the photo)")]
        decider = self.make_decider([], problems=problems, face_uids={"vibham", "kush1434", "arnav"})
        cases = {"arnav": "arnav's reference face could not be used (Spring photo: no face found",
                 "nikhil": "nikhil is not in FACE_UIDS"}
        for uid, reason in cases.items():
            with self.subTest(uid=uid):
                answer, log = self.decide_with_log(decider, uid)
                self.assertEqual(answer.result, "not_enrolled")
                self.assertIn(reason, log)
        _, log = self.decide_with_log(self.make_decider([]), "optout")
        self.assertIn("no reference face for optout", log)
        self.assertEqual(self.grabber.timeouts, [])  # no picture looked at

    def test_zero_wait_looks_at_exactly_one_frame(self):
        decider = self.make_decider([picture(), picture((300, "vibham"))], face_wait_seconds=0)
        self.assertEqual(self.decide(decider).result, "no_face")
        self.assertEqual(len(self.grabber.frames), 1)
        self.assertEqual(self.grabber.timeouts, [camera_mode.NEW_FRAME_TIMEOUT_SECONDS])

    def test_first_wait_covers_the_whole_face_wait(self):
        decider = self.make_decider([picture((300, "vibham"))], face_wait_seconds=3.0)
        self.decide(decider)
        self.assertGreater(self.grabber.timeouts[0], 2.9)

    def test_stalled_camera_gives_no_result(self):
        with self.assertRaises(CameraError) as raised:
            self.decide(self.make_decider([]))
        self.assertIn("restart the camera client", str(raised.exception))

    def test_opencv_error_gives_no_result(self):
        decider = self.make_decider([picture((300, "vibham"))])
        self.cv2.fail_detect = True
        with self.assertRaises(CameraError):
            self.decide(decider)

    def test_close_stops_the_camera(self):
        decider = self.make_decider([])
        decider.close()
        self.assertTrue(self.grabber.stopped)


class ListCamerasTest(unittest.TestCase):
    def list_cameras(self, found):
        log = io.StringIO()
        with mock.patch.object(camera_mode, "load_opencv", return_value=(FakeCv2(), FakeNumpy())), \
                mock.patch.object(camera_mode, "probe_cameras", return_value=found), redirect_stdout(log):
            camera_mode.list_cameras()
        return log.getvalue()

    def test_no_camera_at_all_points_to_the_permission(self):
        output = self.list_cameras([(index, "no camera") for index in range(6)])
        self.assertIn("click Allow", output)
        self.assertIn("Privacy & Security > Camera", output)

    def test_working_camera_says_where_to_put_its_number(self):
        output = self.list_cameras([(0, "works, 1920x1080"), (1, "no camera")])
        self.assertIn("camera 0: works, 1920x1080", output)
        self.assertIn("CAMERA_INDEX", output)
        self.assertNotIn("click Allow", output)


class CheckFacesTest(unittest.TestCase):
    def test_lists_usable_faces_and_problems_without_ocs(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        photos = {"vibham.jpg": photo_bytes([[80, "vibham"]]), "vibham_2.jpg": photo_bytes([[90, "vibham"]]),
                  "blurry.jpg": photo_bytes([])}
        for name, data in photos.items():
            with open(os.path.join(folder.name, name), "wb") as f:
                f.write(data)
        engine = face_engine.FaceEngine(FakeCv2(), FakeNumpy(), "d.onnx", "r.onnx")
        env = {"FACE_SOURCE": "folder", "FACE_UIDS": "vibham,blurry,arnav"}

        log = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(camera_mode, "FACES_DIR", folder.name), \
                mock.patch.object(camera_mode, "start_face_engine", return_value=engine), redirect_stdout(log):
            camera_mode.check_faces()

        output = log.getvalue()
        self.assertIn("ok vibham (2 photos)", output)
        self.assertIn("! blurry: faces/blurry.jpg: no face found", output)
        self.assertIn("! arnav: listed in FACE_UIDS, but has no photo in faces/", output)
        self.assertIn("1 people can be recognized", output)

    def test_no_usable_faces_is_a_setup_error(self):
        engine = face_engine.FaceEngine(FakeCv2(), FakeNumpy(), "d.onnx", "r.onnx")
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.dict(os.environ, {"FACE_SOURCE": "folder"}, clear=True), \
                mock.patch.object(camera_mode, "FACES_DIR", empty), \
                mock.patch.object(camera_mode, "start_face_engine", return_value=engine), \
                redirect_stdout(io.StringIO()), self.assertRaises(SetupError):
            camera_mode.check_faces()


class PreviewCv2(FakeCv2):
    """FakeCv2 plus the drawing and window calls the preview uses."""

    FONT_HERSHEY_SIMPLEX = 0
    LINE_AA = 16

    def __init__(self, keys):
        super().__init__()
        self.keys = list(keys)
        self.boxes, self.texts, self.shown = [], [], []

    def rectangle(self, image, top_left, bottom_right, color, thickness):
        self.boxes.append((top_left, bottom_right, color, thickness))

    def putText(self, image, text, origin, font, scale, color, thickness, line_type):
        self.texts.append(text)

    def imshow(self, window, image):
        self.shown.append(image)

    def waitKey(self, delay):
        return self.keys.pop(0) if self.keys else -1


class PreviewTest(unittest.TestCase):
    def test_boxes_names_and_scores_then_q_quits(self):
        cv2 = PreviewCv2(keys=[-1, ord("q")])
        engine = face_engine.FaceEngine(cv2, FakeNumpy(), "d.onnx", "r.onnx")
        gallery = {"vibham": [engine.embed(None, (0, 0, 1, 1, "vibham"))]}
        cv2.match_scores = {("stranger", "vibham"): 0.2}
        first = FakeImage([(300, "vibham"), (100, "stranger")], (360, 640, 3))  # small: not shrunk
        grabber = ScriptedGrabber([first, picture(), picture()])

        camera_mode.show_preview(engine, gallery, grabber, 0.363)

        self.assertEqual(len(cv2.shown), 2)  # quit after the second frame
        self.assertEqual(len(grabber.frames), 1)
        texts = " | ".join(cv2.texts)
        self.assertIn("vibham 0.90", texts)
        self.assertIn("no match (closest: vibham 0.20)", texts)
        self.assertIn("threshold 0.363", texts)
        thick = [box for box in cv2.boxes if box[3] == 3]
        self.assertEqual(thick, [((0, 0), (300, 300), camera_mode.GREEN, 3)])  # the face a tap would use
        self.assertIsNot(cv2.shown[0], first)  # draws on a copy, never on the camera's frame

    def test_label_face(self):
        self.assertEqual(camera_mode.label_face({}, 0.363), ("face", camera_mode.WHITE))
        self.assertEqual(camera_mode.label_face({"a": 0.5, "b": 0.4}, 0.363), ("a 0.50", camera_mode.GREEN))
        self.assertEqual(camera_mode.label_face({"a": 0.3}, 0.363),
                         ("no match (closest: a 0.30)", camera_mode.RED))


if __name__ == "__main__":
    unittest.main()
