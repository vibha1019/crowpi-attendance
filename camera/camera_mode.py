"""Camera mode: the Mac Mini camera software from Jarvis3000#17.

For each tap, CameraDecider looks at fresh webcam frames for up to
FACE_WAIT_SECONDS and compares the largest face with the reference faces
(Spring or camera/faces/). It answers match, no_match, no_face or
not_enrolled. Pictures stay in memory and are never saved or sent.

This file also has the setup checks for the Mac Mini: --list-cameras,
--check-faces and --preview. None of them talk to OCS.
"""

import os
import time
import urllib.parse

from camera_common import LOCAL_HOSTS, Answer, CameraError, SetupError, utc_iso_now
from face_engine import (CAMERA_DIR, DEFAULT_MATCH_THRESHOLD, DETECTOR_FILE, RECOGNIZER_FILE, FaceEngine,
                         best_match, build_gallery, decide_camera_result, largest_face, load_opencv,
                         require_model_files)
from reference_faces import describe_source, load_reference_photos
from webcam import PERMISSION_HINT, FrameGrabber, open_camera, probe_cameras

DEFAULT_SPRING_URL = "https://spring.opencodingsociety.com"
FACE_SOURCES = ("spring", "folder")
FACES_DIR = os.path.join(CAMERA_DIR, "faces")
DEFAULT_FACE_WAIT_SECONDS = 3.0
# A result should reach OCS within ~10 s of the tap (Jarvis3000#17), and a
# tap can already be up to 8 s old when it is picked up.
MAX_FACE_WAIT_SECONDS = 5.0
# Going this long without a new frame means the camera has stalled.
NEW_FRAME_TIMEOUT_SECONDS = 1.0
CAMERA_NUMBERS_TO_PROBE = range(6)
MAX_UIDS_TO_LIST = 20

PREVIEW_WINDOW = "Camera preview - press q to quit"
GREEN, RED, WHITE, BLACK = (0, 200, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0)  # OpenCV colors are BGR


# ---------------------------------------------------------------- settings

def model_path(env_name, file_name):
    """A model file path. Relative paths count from the camera/ folder."""
    return os.path.join(CAMERA_DIR, os.environ.get(env_name) or os.path.join("models", file_name))


def parse_uid_list(text):
    """"vibham, kush1434" -> {"vibham", "kush1434"}; empty -> None (no filter)."""
    uids = {uid.strip() for uid in (text or "").split(",") if uid.strip()}
    return uids or None


def load_camera_config():
    """Camera mode settings from the environment (see camera.env.example)."""
    try:
        match_threshold = float(os.environ.get("MATCH_THRESHOLD") or DEFAULT_MATCH_THRESHOLD)
        face_wait_seconds = float(os.environ.get("FACE_WAIT_SECONDS") or DEFAULT_FACE_WAIT_SECONDS)
        camera_index = int(os.environ.get("CAMERA_INDEX") or 0)
    except ValueError:
        raise SetupError("MATCH_THRESHOLD and FACE_WAIT_SECONDS must be numbers, and CAMERA_INDEX a whole number.")
    # Written as "not (inside the range)" so that nan is rejected too.
    if not 0 < match_threshold <= 1:
        raise SetupError(f"MATCH_THRESHOLD must be above 0 and at most 1, got {match_threshold} "
                         f"(the standard value for SFace is {DEFAULT_MATCH_THRESHOLD}).")
    if not 0 <= face_wait_seconds <= MAX_FACE_WAIT_SECONDS:
        raise SetupError(f"FACE_WAIT_SECONDS must be from 0 to {MAX_FACE_WAIT_SECONDS:g}, got {face_wait_seconds}.")
    if camera_index < 0:
        raise SetupError(f"CAMERA_INDEX must be 0 or more, got {camera_index}.")

    face_source = (os.environ.get("FACE_SOURCE") or "spring").strip().lower()
    if face_source not in FACE_SOURCES:
        raise SetupError(f"FACE_SOURCE must be spring or folder, got {face_source!r}.")
    spring_url = (os.environ.get("SPRING_URL") or DEFAULT_SPRING_URL).rstrip("/")
    if face_source == "spring":
        missing = [name for name in ("SPRING_UID", "SPRING_PASSWORD") if not os.environ.get(name)]
        if missing:
            raise SetupError(f"FACE_SOURCE=spring needs {' and '.join(missing)} (a Teacher or Admin login), "
                             "or set FACE_SOURCE=folder.")
        url = urllib.parse.urlparse(spring_url)
        if url.scheme != "https" and url.hostname not in LOCAL_HOSTS:
            raise SetupError(f"SPRING_URL must start with https:// (the password is sent there), got {spring_url!r}.")

    return {
        "camera_index": camera_index,
        "match_threshold": match_threshold,
        "face_wait_seconds": face_wait_seconds,
        "detector_model": model_path("FACE_DETECTOR_MODEL", DETECTOR_FILE),
        "recognizer_model": model_path("FACE_RECOGNIZER_MODEL", RECOGNIZER_FILE),
        "face_source": face_source,
        "face_uids": parse_uid_list(os.environ.get("FACE_UIDS")),
        "faces_dir": FACES_DIR,
        "spring_url": spring_url,
        "spring_uid": os.environ.get("SPRING_UID"),
        "spring_password": os.environ.get("SPRING_PASSWORD"),
    }


# ---------------------------------------------------------------- deciding a tap

def problems_by_uid(problems):
    """(uid, message) pairs -> {uid: "message; message"}."""
    found = {}
    for uid, message in problems:
        found[uid] = f"{found[uid]}; {message}" if uid in found else message
    return found


def explain_not_enrolled(tapper, gallery, problems, face_uids):
    """Why a tapper has no usable reference face. Printed with each
    not_enrolled, so a teammate whose photo failed is not silently sent as
    Tap only."""
    if tapper in problems:
        return f"{tapper}'s reference face could not be used ({problems[tapper]}). Run --check-faces."
    if face_uids and tapper not in face_uids:
        return f"{tapper} is not in FACE_UIDS."
    for uid in gallery:
        if tapper and uid.lower() == str(tapper).lower():
            return f"the reference face is under {uid!r}, not {tapper!r}. The uids must match exactly."
    return f"no reference face for {tapper} (opted out, or a different uid in Spring)."


# Which answer to keep when the tapper is never recognized: someone
# recognized beats a stranger, and a stranger beats no face.
RESULT_ORDER = {"no_face": 0, "no_match": 1, "match": 2}


def rank(answer):
    """Sort key for two pictures' answers: the better result wins, and
    among equal results the higher score wins."""
    return (RESULT_ORDER[answer.result], answer.confidence or 0.0)


class CameraDecider:
    """decide(tap) for camera mode.

    A student often looks down at the tap pad, so one picture is not
    enough. For up to face_wait_seconds it checks each new frame: the
    first frame whose face matches the tapper decides at once (match).
    A match on someone else is kept, but only sent if the tapper never
    shows up before the time is up (a proxy tap). Otherwise the answer is
    the closest no_match, or no_face if no frame had a face. A tapper with
    no reference face is not_enrolled, and no picture is looked at.

    problems are the (uid, message) pairs found while loading the
    reference faces; they explain a not_enrolled in the log."""

    def __init__(self, engine, gallery, grabber, threshold, face_wait_seconds, problems=(), face_uids=None):
        self.engine = engine
        self.gallery = gallery
        self.grabber = grabber
        self.threshold = threshold
        self.face_wait_seconds = face_wait_seconds
        self.problems = problems_by_uid(problems)
        self.face_uids = face_uids

    def check_frame(self, tapper, frame, taken_at):
        """The answer this one picture gives."""
        small, faces = self.engine.find_faces(frame)
        scores = self.engine.scores(self.engine.embed(small, largest_face(faces)), self.gallery) if faces else {}
        result, uid, confidence = decide_camera_result(tapper, self.gallery, len(faces), scores, self.threshold)
        return Answer(result, uid, taken_at, confidence)

    def decide(self, tap):
        tapper = tap.get("uid")
        print(f"\nTap by {tapper} ({tap.get('type')}) at {tap.get('occurred_at')}")
        if tapper not in self.gallery:
            reason = explain_not_enrolled(tapper, self.gallery, self.problems, self.face_uids)
            print(f"  not_enrolled (Tap only): {reason}")
            return Answer("not_enrolled", None, utc_iso_now())

        deadline = time.monotonic() + self.face_wait_seconds
        last_number = self.grabber.latest_number()  # only frames read after the tap was picked up
        best = None
        while True:
            wait = max(deadline - time.monotonic(), NEW_FRAME_TIMEOUT_SECONDS)
            got = self.grabber.wait_for_frame_after(last_number, wait)
            if got is None:
                if best is not None:
                    return best  # the camera stopped part way; use what it saw
                raise CameraError(f"no new picture from the camera for {wait:.0f} s (unplugged? "
                                  "plug it back in and restart the camera client)")
            last_number, frame, taken_at = got
            try:
                answer = self.check_frame(tapper, frame, taken_at)
            except self.engine.cv2.error as e:
                raise CameraError(f"OpenCV failed on a picture: {e}")
            if answer.result == "match" and answer.uid == tapper:
                return answer  # the tapper is seen: done
            # A match on someone else (a teammate behind the tapper, or a
            # look down at the pad that resembles a teammate) is kept, but
            # the tapper may still look up before the deadline.
            if best is None or rank(answer) >= rank(best):
                best = answer
            if time.monotonic() >= deadline:
                return best

    def close(self):
        self.grabber.stop()


# ---------------------------------------------------------------- setup

def start_face_engine(camera_config):
    require_model_files(camera_config["detector_model"], camera_config["recognizer_model"])
    cv2, numpy = load_opencv()
    return FaceEngine(cv2, numpy, camera_config["detector_model"], camera_config["recognizer_model"])


def load_gallery(engine, camera_config):
    """(gallery, problems) from the configured reference faces."""
    print(f"Loading reference faces from {describe_source(camera_config)}...")
    photos, problems = load_reference_photos(camera_config)
    gallery, photo_problems = build_gallery(engine, photos)
    if not camera_config["face_uids"]:
        print(f"FACE_UIDS is empty, so all {len(gallery)} people's faces are kept in memory. "
              "Set FACE_UIDS to keep only the demo team's.")
    return gallery, problems + photo_problems


def print_problems(problems):
    for uid, message in problems:
        print(f"  ! {uid}: {message}")


def start_grabber(engine, camera_index):
    capture, first_frame = open_camera(engine.cv2, camera_index)
    height, width = first_frame.shape[:2]
    print(f"Camera {camera_index} is on ({width}x{height}).")
    grabber = FrameGrabber(capture)
    grabber.start()
    return grabber


def build_camera_decider():
    """Sets up camera mode, checking in order so the first problem is
    reported clearly: settings, model files, OpenCV, reference faces, then
    the camera. Call .close() on the result when done."""
    camera_config = load_camera_config()
    engine = start_face_engine(camera_config)
    gallery, problems = load_gallery(engine, camera_config)
    print_problems(problems)
    if not gallery:
        raise SetupError("No usable reference faces, so every tap would be not_enrolled. Run --check-faces.")
    grabber = start_grabber(engine, camera_config["camera_index"])
    names = ", ".join(sorted(gallery)) if len(gallery) <= MAX_UIDS_TO_LIST else "too many to list"
    print(f"Camera mode: {len(gallery)} people can be recognized ({names}). "
          f"Match threshold {camera_config['match_threshold']}, "
          f"looking up to {camera_config['face_wait_seconds']:g} s per tap.")
    return CameraDecider(engine, gallery, grabber, camera_config["match_threshold"],
                         camera_config["face_wait_seconds"], problems, camera_config["face_uids"])


# ---------------------------------------------------------------- setup checks

def list_cameras():
    """--list-cameras: which camera numbers work, and their frame size."""
    cv2, _ = load_opencv()
    print(f"Checking camera numbers {CAMERA_NUMBERS_TO_PROBE[0]}-{CAMERA_NUMBERS_TO_PROBE[-1]}. "
          "OpenCV may print warnings for numbers with no camera; that is normal.")
    found = probe_cameras(cv2, CAMERA_NUMBERS_TO_PROBE)
    for index, description in found:
        print(f"  camera {index}: {description}")
    if all(description == "no camera" for _, description in found):
        # Before macOS has said yes to the camera, every number fails to open.
        print("No camera opened. If macOS just asked about camera access, click Allow, quit Terminal "
              "(Cmd+Q), reopen it and run this again. " + PERMISSION_HINT)
        return
    print("Put the Brio's number in camera.env as CAMERA_INDEX. To see which camera a number is:\n"
          "  CAMERA_INDEX=<number> python camera_client.py --preview")


def check_faces():
    """--check-faces: load the reference faces and say who is usable."""
    camera_config = load_camera_config()
    engine = start_face_engine(camera_config)
    gallery, problems = load_gallery(engine, camera_config)
    for uid in sorted(gallery):
        count = len(gallery[uid])
        print(f"  ok {uid} ({count} photo{'' if count == 1 else 's'})")
    print_problems(problems)
    print(f"{len(gallery)} people can be recognized. Anyone else who taps is sent as not_enrolled (Tap only).")
    if not gallery:
        raise SetupError("No usable reference faces.")


def label_face(scores, threshold):
    """Text and color for one face box in the preview."""
    uid, score = best_match(scores)
    if uid is None:
        return "face", WHITE
    if score >= threshold:
        return f"{uid} {score:.2f}", GREEN
    return f"no match (closest: {uid} {score:.2f})", RED


def draw_text(cv2, image, text, origin, color):
    """Text with a dark outline, readable on any background."""
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.5, BLACK, 3, cv2.LINE_AA)
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)


def show_preview(engine, gallery, grabber, threshold):
    """The preview loop. It must run on the main thread: macOS only allows
    windows there. The thick box is the largest face, the one a tap is
    checked against."""
    cv2 = engine.cv2
    last_number, frames, fps, fps_started = 0, 0, 0.0, time.monotonic()
    while True:
        got = grabber.wait_for_frame_after(last_number, NEW_FRAME_TIMEOUT_SECONDS)
        if got is None:
            print("No new picture from the camera. Is it unplugged?")
            if cv2.waitKey(100) & 0xFF in (ord("q"), 27):
                return
            continue
        last_number, frame, _ = got
        small, faces = engine.find_faces(frame)
        canvas = small.copy()  # draw on a copy, so the boxes never touch the picture faces are read from
        tap_face = largest_face(faces) if faces else None
        for face in faces:
            text, color = label_face(engine.scores(engine.embed(small, face), gallery), threshold)
            x, y, width, height = (int(value) for value in face[:4])
            cv2.rectangle(canvas, (x, y), (x + width, y + height), color, 3 if face is tap_face else 1)
            draw_text(cv2, canvas, text, (x, max(y - 8, 14)), color)

        frames += 1
        elapsed = time.monotonic() - fps_started
        if elapsed >= 1:
            fps, frames, fps_started = frames / elapsed, 0, time.monotonic()
        draw_text(cv2, canvas, f"threshold {threshold:.3f}   {fps:.0f} fps   q = quit",
                  (8, canvas.shape[0] - 10), WHITE)
        cv2.imshow(PREVIEW_WINDOW, canvas)
        # Only q / Esc (or Ctrl+C in Terminal) stop it. Closing the window is
        # not checked: not every OpenCV build can report that reliably.
        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
            return


def preview():
    """--preview: a live window to aim the camera and tune the threshold.
    Display only: nothing is saved or sent."""
    camera_config = load_camera_config()
    engine = start_face_engine(camera_config)
    gallery, problems = load_gallery(engine, camera_config)
    print_problems(problems)
    if not gallery:
        print("No usable reference faces: the preview will show face boxes but no names.")
    grabber = start_grabber(engine, camera_config["camera_index"])
    print("Preview is open. Nothing is saved or sent. Press q in the preview window to quit.")
    try:
        show_preview(engine, gallery, grabber, camera_config["match_threshold"])
    finally:
        grabber.stop()
        engine.cv2.destroyAllWindows()
