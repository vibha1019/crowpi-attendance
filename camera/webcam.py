"""The webcam: opening it, and reading it nonstop in the background so a tap
is always checked against a fresh picture.

Frames live in memory only. Nothing here saves or sends a picture.
"""

import sys
import threading
import time

from camera_common import SetupError, utc_iso_now

# We ask for 1280x720, but on macOS OpenCV keeps the camera's current
# format, so frames may be another size (e.g. 640x480 or 1920x1080). Any
# size works: face_engine shrinks big frames.
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
# Right after opening, macOS cameras can take a moment to send the first frame.
FIRST_FRAME_SECONDS = 3.0
PROBE_FIRST_FRAME_SECONDS = 1.5  # shorter for --list-cameras, which tries 6 numbers
READ_RETRY_SECONDS = 0.05
READ_ERROR_PAUSE_SECONDS = 1.0  # after OpenCV raises an error on a read

PERMISSION_HINT = ("On macOS, allow the app you run this from (Terminal, iTerm or VS Code) to use the camera: "
                   "System Settings > Privacy & Security > Camera. Then quit that app fully and run this again. "
                   "Also check that no other program (such as Jarvis's own capture loop) is using the camera; "
                   "on Linux only one program can use it at a time.")


def capture_backend(cv2):
    """AVFoundation is macOS's camera system. Elsewhere, let OpenCV pick."""
    return cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY


def read_first_frame(capture, seconds):
    """The first frame from a just-opened camera, or None if none comes."""
    deadline = time.monotonic() + seconds
    while True:
        ok, frame = capture.read()
        if ok and frame is not None:
            return frame
        if time.monotonic() >= deadline:
            return None
        time.sleep(READ_RETRY_SECONDS)


def open_camera(cv2, index):
    """Opens camera number `index` (asking for 1280x720). Returns
    (capture, first_frame)."""
    capture = cv2.VideoCapture(index, capture_backend(cv2))
    if not capture.isOpened():
        capture.release()
        raise SetupError(f"Could not open camera {index}. Run --list-cameras to see which numbers work. "
                         + PERMISSION_HINT)
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    frame = read_first_frame(capture, FIRST_FRAME_SECONDS)
    if frame is None:
        capture.release()
        raise SetupError(f"Camera {index} opened but sent no picture. " + PERMISSION_HINT)
    return capture, frame


def probe_cameras(cv2, indexes):
    """(index, description) for each camera number: whether it opens and
    its frame size. Used by --list-cameras."""
    found = []
    for index in indexes:
        capture = cv2.VideoCapture(index, capture_backend(cv2))
        if not capture.isOpened():
            found.append((index, "no camera"))
            capture.release()
            continue
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        frame = read_first_frame(capture, PROBE_FIRST_FRAME_SECONDS)
        capture.release()
        if frame is None:
            found.append((index, "opens, but sends no picture (camera permission?)"))
        else:
            height, width = frame.shape[:2]
            found.append((index, f"works, {width}x{height}"))
    return found


class FrameGrabber:
    """Reads the camera nonstop in a background thread and keeps only the
    newest frame. Cameras queue up a few frames; without this, a tap could
    be checked against a picture from a second ago."""

    def __init__(self, capture):
        self.capture = capture
        self.new_frame = threading.Condition()
        self.frame = None
        self.frame_number = 0  # how many frames have been read; 0 = none yet
        self.taken_at = None   # when the newest frame was read, UTC ISO string
        self.running = False
        self.thread = None
        self.read_error_reported = False

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.read_forever, daemon=True)
        self.thread.start()

    def read_forever(self):
        while self.running:
            try:
                ok, frame = self.capture.read()
            except Exception as e:  # cv2.error: keep the thread alive and say why once
                if not self.read_error_reported:
                    print(f"Camera read failed: {e}")
                    self.read_error_reported = True
                time.sleep(READ_ERROR_PAUSE_SECONDS)
                continue
            if not ok or frame is None:
                time.sleep(READ_RETRY_SECONDS)  # a hiccup or an unplugged camera; keep trying
                continue
            taken_at = utc_iso_now()
            with self.new_frame:
                self.frame, self.taken_at = frame, taken_at
                self.frame_number += 1
                self.new_frame.notify_all()

    def latest_number(self):
        with self.new_frame:
            return self.frame_number

    def wait_for_frame_after(self, frame_number, timeout):
        """The newest frame, once one newer than frame_number has been read,
        as (number, frame, taken_at). None if none arrives within timeout."""
        with self.new_frame:
            if not self.new_frame.wait_for(lambda: self.frame_number > frame_number, timeout):
                return None
            return self.frame_number, self.frame, self.taken_at

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self.capture.release()
