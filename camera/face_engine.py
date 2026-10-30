"""Face detection and recognition with OpenCV, run on this machine only.

YuNet finds faces in a picture. SFace turns one face into a list of
numbers (an embedding). Two embeddings of the same person point in nearly
the same direction, so their cosine similarity (the "score") is high.
OpenCV's standard cutoff for SFace is 0.363: at or above it, two faces
count as the same person.

OpenCV is imported only when a camera feature is used, so the test modes
and the unit tests run without it. The cv2 and numpy modules are passed
in, so the tests can hand in fakes.
"""

import math
import os

from camera_common import SetupError

CAMERA_DIR = os.path.dirname(os.path.abspath(__file__))
DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
MODEL_URLS = {
    DETECTOR_FILE: "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + DETECTOR_FILE,
    RECOGNIZER_FILE: "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/" + RECOGNIZER_FILE,
}
# A real model is hundreds of KB (YuNet) or tens of MB (SFace). Anything this
# small is a Git LFS pointer or an error page saved by mistake.
MIN_MODEL_BYTES = 50_000
# OpenCV's own cosine-similarity cutoff for SFace (from its face recognition sample).
DEFAULT_MATCH_THRESHOLD = 0.363
# How sure YuNet must be that a box is a face. OpenCV's default is 0.9, but
# with the real model that missed brighter and smaller copies of a clear test
# face (they scored 0.89), so a student under a bright light or a step back
# would come back as no_face.
DETECT_SCORE_THRESHOLD = 0.7
# Faces are embedded from the same picture they are found in. 720p webcam
# frames are used as they are, so a face a meter away keeps enough detail
# for SFace. Bigger frames and phone photos are shrunk to 1280 px, to keep
# detection fast.
MAX_DETECT_SIDE = 1280


def load_opencv():
    """(cv2, numpy), or a SetupError saying what to install."""
    try:
        import cv2
        import numpy
    except ImportError as e:
        raise SetupError(f"Camera features need OpenCV and numpy ({e}). Run: pip install -r requirements.txt")
    if not hasattr(cv2, "FaceDetectorYN") or not hasattr(cv2, "FaceRecognizerSF"):
        raise SetupError(f"OpenCV {cv2.__version__} has no face detector. Run: pip install -r requirements.txt")
    return cv2, numpy


def require_model_files(detector_model, recognizer_model):
    """Stops with download instructions if a model file is missing or is
    not a real model."""
    bad = [path for path in (detector_model, recognizer_model)
           if not os.path.isfile(path) or os.path.getsize(path) < MIN_MODEL_BYTES]
    if bad:
        raise SetupError(
            "Face model file(s) missing or incomplete:\n  " + "\n  ".join(bad) + "\n"
            "Download both (about 40 MB) once with:\n"
            f"  {os.path.join(CAMERA_DIR, 'get_models.sh')}\n"
            "or download them yourself from the OpenCV Zoo:\n  " + "\n  ".join(MODEL_URLS.values()) + "\n"
            "into camera/models/ (or point FACE_DETECTOR_MODEL / FACE_RECOGNIZER_MODEL at them)."
        )


def largest_face(faces):
    """YuNet rows are [x, y, w, h, ...]. The biggest box is the person
    closest to the tap station."""
    return max(faces, key=lambda face: face[2] * face[3])


def best_match(scores):
    """(uid, score) with the highest finite score, or (None, None)."""
    finite = {uid: float(score) for uid, score in scores.items() if math.isfinite(score)}
    if not finite:
        return None, None
    uid = max(finite, key=finite.get)
    return uid, finite[uid]


def decide_camera_result(tapper_uid, enrolled_uids, face_count, scores, threshold):
    """The camera-mode rule for one picture, kept free of OpenCV so it can
    be tested. Returns (result, uid, confidence).

    scores maps each enrolled uid to its best cosine score against the
    largest face in the picture. confidence is that raw score clamped to
    0..1 (a similarity score, not a probability)."""
    if tapper_uid not in enrolled_uids:
        return "not_enrolled", None, None
    if face_count == 0:
        return "no_face", None, None
    uid, score = best_match(scores)  # a broken embedding can score NaN; it never wins
    if uid is None:
        return "no_match", None, None
    confidence = min(max(score, 0.0), 1.0)
    if score >= threshold:
        return "match", uid, confidence  # whoever it is, even if not the tapper
    return "no_match", None, confidence


class FaceEngine:
    """YuNet + SFace, loaded once."""

    def __init__(self, cv2, numpy, detector_model, recognizer_model):
        self.cv2 = cv2
        self.numpy = numpy
        try:
            self.detector = cv2.FaceDetectorYN.create(detector_model, "", (320, 320), DETECT_SCORE_THRESHOLD)
            self.recognizer = cv2.FaceRecognizerSF.create(recognizer_model, "")
        except cv2.error as e:
            raise SetupError(f"OpenCV {cv2.__version__} could not load the face models ({str(e).strip()}). "
                             f"Re-download them with {os.path.join(CAMERA_DIR, 'get_models.sh')}.")

    def decode(self, image_bytes):
        """JPEG/PNG bytes -> image, or None if they are not a readable image."""
        buffer = self.numpy.frombuffer(image_bytes, dtype=self.numpy.uint8)
        if buffer.size == 0:
            return None
        try:
            return self.cv2.imdecode(buffer, self.cv2.IMREAD_COLOR)
        except self.cv2.error:
            return None

    def shrink(self, image):
        height, width = image.shape[:2]
        scale = MAX_DETECT_SIDE / max(height, width)
        if scale >= 1:
            return image
        size = (round(width * scale), round(height * scale))
        return self.cv2.resize(image, size, interpolation=self.cv2.INTER_AREA)

    def find_faces(self, image):
        """(small_image, faces): the image shrunk for detection, and the
        faces found in it. Use small_image with embed(), since the face
        boxes are in its coordinates."""
        small = self.shrink(image)
        height, width = small.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(small)
        return small, ([] if faces is None else list(faces))

    def embed(self, image, face):
        aligned = self.recognizer.alignCrop(image, face)
        # OpenCV's own sample copies this too, in case the output buffer is reused.
        return self.recognizer.feature(aligned).copy()

    def scores(self, embedding, gallery):
        """uid -> best cosine score of this face against that uid's photos."""
        cosine = self.cv2.FaceRecognizerSF_FR_COSINE
        return {
            uid: max(self.recognizer.match(embedding, reference, cosine) for reference in references)
            for uid, references in gallery.items()
        }


def build_gallery(engine, photos):
    """uid -> embeddings of that person's reference photos, computed once
    and kept in memory only. Returns (gallery, problems), where problems
    are (uid, message) pairs for photos that could not be used."""
    gallery, problems = {}, []
    for photo in photos:
        image = engine.decode(photo.image_bytes)
        if image is None:
            problems.append((photo.uid, f"{photo.label}: not a readable image"))
            continue
        try:
            small, faces = engine.find_faces(image)
            if not faces:
                problems.append((photo.uid, f"{photo.label}: no face found in it (retake the photo)"))
                continue
            gallery.setdefault(photo.uid, []).append(engine.embed(small, largest_face(faces)))
        except engine.cv2.error as e:
            problems.append((photo.uid, f"{photo.label}: OpenCV failed on it ({e})"))
            continue
        if len(faces) > 1:
            problems.append((photo.uid, f"{photo.label}: {len(faces)} faces in it, used the largest"))
    return gallery, problems
