"""--enroll UID: take a reference photo of one person with the webcam and
save it as camera/faces/<uid>.jpg, for FACE_SOURCE=folder.

This is the only part of the camera software that writes a picture to
disk, and only when the operator presses space. The photo stays on this
machine (camera/faces/ is gitignored). Delete the file to remove the
person. Nothing here talks to OCS or Spring.
"""

import os
import re

from camera_common import SetupError
from camera_mode import (FACES_DIR, GREEN, NEW_FRAME_TIMEOUT_SECONDS, RED, WHITE, draw_text, model_path,
                         start_face_engine, start_grabber)
from face_engine import DETECTOR_FILE, RECOGNIZER_FILE
from reference_faces import uid_from_photo_name

ENROLL_WINDOW = "Enroll - space = save photo, q = quit"
# The uid becomes a file name, so only plain characters are allowed.
SAFE_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SPACE, ESC = ord(" "), 27


def check_uid(uid):
    """The uid, or a SetupError. It must also read back as the same uid
    from its file name: vibha_2.jpg is read as a second photo of vibha."""
    if not SAFE_UID.fullmatch(uid or "") or uid_from_photo_name(uid + ".jpg") != uid:
        raise SetupError(f"Can't save a photo for {uid!r}: use the OCS uid, with only letters, numbers, "
                         "'.', '-' and '_', and not ending in _<number>.")
    return uid


def camera_index_from_env():
    """CAMERA_INDEX on its own: --enroll needs no face source settings."""
    try:
        index = int(os.environ.get("CAMERA_INDEX") or 0)
    except ValueError:
        raise SetupError("CAMERA_INDEX must be a whole number.")
    if index < 0:
        raise SetupError(f"CAMERA_INDEX must be 0 or more, got {index}.")
    return index


def next_photo_path(faces_dir, uid):
    """faces/<uid>.jpg, or <uid>_2.jpg, <uid>_3.jpg, ... if that is taken.
    A second photo of the same person (other light, glasses) helps."""
    number = 1
    while True:
        name = f"{uid}.jpg" if number == 1 else f"{uid}_{number}.jpg"
        path = os.path.join(faces_dir, name)
        if not os.path.exists(path):
            return path
        number += 1


def enroll_hint(uid, face_count):
    if face_count == 1:
        return f"{uid}: press space to save"
    if face_count == 0:
        return "no face found: face the camera"
    return f"{face_count} faces: only {uid} should be in view"


def run_enroll_window(engine, grabber, faces_dir, uid):
    """Shows the camera with face boxes until space saves a picture with
    exactly one face in it, or q quits. Returns the saved path, or None.
    Must run on the main thread: macOS only allows windows there."""
    cv2 = engine.cv2
    last_number = 0
    while True:
        got = grabber.wait_for_frame_after(last_number, NEW_FRAME_TIMEOUT_SECONDS)
        if got is None:
            print("No new picture from the camera. Is it unplugged?")
            if cv2.waitKey(100) & 0xFF in (ord("q"), ESC):
                return None
            continue
        last_number, frame, _ = got
        small, faces = engine.find_faces(frame)
        ready = len(faces) == 1
        hint = enroll_hint(uid, len(faces))
        canvas = small.copy()  # boxes go on a copy; the saved photo has none
        for face in faces:
            x, y, width, height = (int(value) for value in face[:4])
            cv2.rectangle(canvas, (x, y), (x + width, y + height), GREEN if ready else RED, 2)
        draw_text(cv2, canvas, f"{hint}   q = quit", (8, canvas.shape[0] - 10), GREEN if ready else WHITE)
        cv2.imshow(ENROLL_WINDOW, canvas)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), ESC):
            return None
        if key != SPACE:
            continue
        if not ready:
            print(f"Not saved ({hint}).")
            continue
        path = next_photo_path(faces_dir, uid)
        os.makedirs(faces_dir, exist_ok=True)
        if not cv2.imwrite(path, small):
            raise SetupError(f"Could not save {path}.")
        return path


def enroll(uid):
    check_uid(uid)
    engine = start_face_engine({"detector_model": model_path("FACE_DETECTOR_MODEL", DETECTOR_FILE),
                                "recognizer_model": model_path("FACE_RECOGNIZER_MODEL", RECOGNIZER_FILE)})
    grabber = start_grabber(engine, camera_index_from_env())
    print(f"Taking a reference photo for {uid}. Face the camera; when the box is green, "
          "press space in the camera window. q quits without saving.")
    try:
        path = run_enroll_window(engine, grabber, FACES_DIR, uid)
    finally:
        grabber.stop()
        engine.cv2.destroyAllWindows()
    if path is None:
        print("Quit. Nothing was saved.")
        return
    print(f"Saved {path} (it stays on this machine).\n"
          f"Run --enroll {uid} again to add another photo of {uid}; delete the file to remove it.\n"
          "Check it with: FACE_SOURCE=folder python camera_client.py --check-faces")
