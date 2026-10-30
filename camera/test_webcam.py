"""Tests for webcam.py with a fake camera. No webcam or OpenCV needed.

    cd camera && python3 -m unittest -v
"""

import io
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import webcam
from camera_common import SetupError
from fake_cv2 import FakeCapture, FakeCv2, FakeImage


class CountingCapture:
    """A fake camera that sends frames 1, 2, 3, ... about every millisecond,
    like a real one that never stops."""

    def __init__(self):
        self.count = 0
        self.released = False

    def read(self):
        time.sleep(0.001)
        self.count += 1
        return True, self.count

    def release(self):
        self.released = True


class FrameGrabberTest(unittest.TestCase):
    def start(self, capture):
        grabber = webcam.FrameGrabber(capture)
        grabber.start()
        self.addCleanup(grabber.stop)
        return grabber

    def test_hands_out_the_newest_frame_not_a_queued_one(self):
        capture = CountingCapture()
        grabber = self.start(capture)
        time.sleep(0.1)  # frames keep coming while nobody asks for one

        number, frame, taken_at = grabber.wait_for_frame_after(0, timeout=1)
        self.assertGreater(number, 5)
        self.assertEqual(frame, number)  # the newest frame read, not frame 1
        self.assertRegex(taken_at, r"Z$")

        newer_number, newer_frame, _ = grabber.wait_for_frame_after(number, timeout=1)
        self.assertGreater(newer_number, number)
        self.assertEqual(newer_frame, newer_number)

    def test_waits_for_a_frame_newer_than_the_one_given(self):
        release = threading.Event()

        class SlowCapture(CountingCapture):
            def read(self):
                release.wait()
                return super().read()

        grabber = self.start(SlowCapture())
        self.assertIsNone(grabber.wait_for_frame_after(0, timeout=0.1))  # nothing read yet
        release.set()
        self.assertIsNotNone(grabber.wait_for_frame_after(0, timeout=1))

    def test_stalled_camera_gives_none_after_the_timeout(self):
        grabber = self.start(FakeCapture(frames=[]))  # every read fails
        started = time.monotonic()
        self.assertIsNone(grabber.wait_for_frame_after(grabber.latest_number(), timeout=0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.19)

    def test_an_opencv_error_on_a_read_is_reported_once_and_reading_goes_on(self):
        class FlakyCapture(CountingCapture):
            def __init__(self):
                super().__init__()
                self.errors_left = 3

            def read(self):
                if self.errors_left:
                    self.errors_left -= 1
                    raise RuntimeError("simulated cv2.error in read()")
                return super().read()

        log = io.StringIO()
        with mock.patch.object(webcam, "READ_ERROR_PAUSE_SECONDS", 0), redirect_stdout(log):
            grabber = self.start(FlakyCapture())
            self.assertIsNotNone(grabber.wait_for_frame_after(0, timeout=1))
        self.assertEqual(log.getvalue().count("Camera read failed: simulated cv2.error"), 1)

    def test_stop_releases_the_camera(self):
        capture = CountingCapture()
        grabber = webcam.FrameGrabber(capture)
        grabber.start()
        grabber.stop()
        self.assertTrue(capture.released)
        self.assertFalse(grabber.thread.is_alive())


class OpenCameraTest(unittest.TestCase):
    def test_uses_avfoundation_on_macos_and_asks_for_720p(self):
        cv2 = FakeCv2()
        cv2.cameras[1] = FakeCapture(frames=[FakeImage(shape=(720, 1280, 3))])
        with mock.patch.object(webcam.sys, "platform", "darwin"):
            capture, frame = webcam.open_camera(cv2, 1)
        self.assertEqual(cv2.opened, [(1, cv2.CAP_AVFOUNDATION)])
        self.assertEqual(capture.settings, {cv2.CAP_PROP_FRAME_WIDTH: 1280, cv2.CAP_PROP_FRAME_HEIGHT: 720})
        self.assertEqual(frame.shape, (720, 1280, 3))

    def test_lets_opencv_pick_elsewhere(self):
        cv2 = FakeCv2()
        cv2.cameras[0] = FakeCapture(frames=[FakeImage()])
        with mock.patch.object(webcam.sys, "platform", "linux"):
            webcam.open_camera(cv2, 0)
        self.assertEqual(cv2.opened, [(0, cv2.CAP_ANY)])

    def test_first_frames_may_fail_while_the_camera_starts(self):
        cv2 = FakeCv2()
        cv2.cameras[0] = FakeCapture(frames=[FakeImage()], failed_reads_first=3)
        with mock.patch.object(webcam, "READ_RETRY_SECONDS", 0):
            _, frame = webcam.open_camera(cv2, 0)
        self.assertIsNotNone(frame)

    def test_camera_that_does_not_open_mentions_the_permission(self):
        with self.assertRaises(SetupError) as raised:
            webcam.open_camera(FakeCv2(), 4)
        self.assertIn("--list-cameras", str(raised.exception))
        self.assertIn("Privacy & Security > Camera", str(raised.exception))

    def test_camera_with_no_picture_is_released_and_reported(self):
        cv2 = FakeCv2()
        cv2.cameras[0] = capture = FakeCapture(frames=[])
        with mock.patch.object(webcam, "FIRST_FRAME_SECONDS", 0.05), self.assertRaises(SetupError) as raised:
            webcam.open_camera(cv2, 0)
        self.assertIn("sent no picture", str(raised.exception))
        self.assertTrue(capture.released)

    def test_probe_lists_each_camera_number(self):
        cv2 = FakeCv2()
        cv2.cameras[0] = FakeCapture(frames=[FakeImage(shape=(720, 1280, 3))])
        cv2.cameras[1] = FakeCapture(frames=[])  # opens, no picture
        with mock.patch.object(webcam, "PROBE_FIRST_FRAME_SECONDS", 0.05):
            found = dict(webcam.probe_cameras(cv2, range(3)))
        self.assertEqual(found[0], "works, 1280x720")
        self.assertIn("no picture", found[1])
        self.assertEqual(found[2], "no camera")
        self.assertTrue(cv2.cameras[0].released)


if __name__ == "__main__":
    unittest.main()
