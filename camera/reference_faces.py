"""Reference faces: the registered photo of each student, which camera mode
compares the face at the tap station against.

Two sources (FACE_SOURCE in camera.env):

  spring  (default) the faces students registered on their OCS profile
          page, from Spring's GET /api/face/faces. That endpoint needs a
          Teacher (or Admin) login (SPRING_UID / SPRING_PASSWORD).
  folder  photos in camera/faces/, named by OCS uid: vibham.jpg, and more
          photos of the same person as vibham_2.jpg, vibham_3.jpg, ...

This file only fetches the photos as bytes. face_engine.py turns them into
face embeddings. Nothing is ever written to disk. Spring cannot send just
some faces: it always sends every registered face (photo and name).
FACE_UIDS (optional) keeps only the listed uids; the rest are dropped
right away, never decoded and never saved.

Each problem found (bad photo data, a FACE_UIDS entry with no photo) is
returned as a (uid, message) pair so --check-faces can list it.
"""

import base64
import binascii
import os
import re
from collections import namedtuple

import requests

from camera_common import SetupError

SPRING_COOKIE = "jwt_java_spring"  # Spring's login is cookie-only (CookieFactory.JWT_COOKIE)
SPRING_TIMEOUT_SECONDS = 30  # the face list can be several MB of base64
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png")

# One photo of one person. label says where it came from, for messages.
ReferencePhoto = namedtuple("ReferencePhoto", ["uid", "label", "image_bytes"])


def decode_face_data(face_data):
    """Spring stores each face as base64 JPEG. The profile page removes the
    "data:image/jpeg;base64," prefix before saving, but other clients might
    not, so it is removed here too. Raises ValueError if unusable."""
    text = str(face_data or "").strip()
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    if not text:
        raise ValueError("no photo data")
    try:
        return base64.b64decode(text)
    except (binascii.Error, ValueError):
        raise ValueError("photo data is not valid base64")


def login_to_spring(spring_url, uid, password):
    """A requests.Session holding Spring's login cookie. The password is
    only sent in the login request and never printed."""
    session = requests.Session()
    try:
        resp = session.post(f"{spring_url}/authenticate", json={"uid": uid, "password": password},
                            timeout=SPRING_TIMEOUT_SECONDS, allow_redirects=False)
    except requests.RequestException as e:
        raise SetupError(f"Could not reach Spring at {spring_url} ({type(e).__name__}). "
                         "Check SPRING_URL and this machine's internet connection.")
    if resp.status_code == 401:
        raise SetupError(f"Spring rejected the login for {uid!r} (401). Check SPRING_UID and SPRING_PASSWORD.")
    if resp.status_code != 200:
        raise SetupError(f"Spring login at {spring_url}/authenticate failed ({resp.status_code}). "
                         "Check SPRING_URL.")
    if not any(cookie.name == SPRING_COOKIE for cookie in session.cookies):
        if SPRING_COOKIE in resp.headers.get("Set-Cookie", ""):
            # Spring's default settings mark the cookie Secure, for *.opencodingsociety.com only.
            raise SetupError(f"Spring sent its login cookie only for https://*.opencodingsociety.com, so it "
                             f"can't be used from {spring_url}. Use https://spring.opencodingsociety.com, "
                             "or start the local Spring with jwt.cookie.secure=false.")
        raise SetupError(f"Spring accepted the login but sent no {SPRING_COOKIE} cookie. Check SPRING_URL.")
    return session


def fetch_spring_faces(session, spring_url, uid):
    """Spring's list of registered faces: [{uid, name, faceData}, ...]."""
    try:
        resp = session.get(f"{spring_url}/api/face/faces", timeout=SPRING_TIMEOUT_SECONDS, allow_redirects=False)
    except requests.RequestException as e:
        raise SetupError(f"Could not get the face list from Spring ({type(e).__name__}).")
    if resp.status_code == 401:
        raise SetupError("Spring did not accept the login cookie for /api/face/faces (401). "
                         "SPRING_URL must start with https:// unless Spring runs on this machine.")
    if resp.status_code == 403:
        raise SetupError(f"{uid!r} is not allowed to read the face list (403). "
                         "SPRING_UID must be a Teacher or Admin account, "
                         "or use FACE_SOURCE=folder with photos in camera/faces/.")
    if resp.status_code != 200:
        raise SetupError(f"Spring refused the face list request ({resp.status_code}).")
    try:
        faces = resp.json()
    except ValueError:
        raise SetupError("Spring's face list was not JSON.")
    if not isinstance(faces, list):
        raise SetupError("Spring's face list was not a list.")
    return faces


def photos_from_spring(faces, face_uids):
    """ReferencePhotos from Spring's face list, keeping only face_uids if
    it is set. Returns (photos, problems)."""
    photos, problems = [], []
    for entry in faces:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("uid") or "").strip()
        if not uid or (face_uids and uid not in face_uids):
            continue
        try:
            photos.append(ReferencePhoto(uid, "Spring photo", decode_face_data(entry.get("faceData"))))
        except ValueError as e:
            problems.append((uid, f"Spring photo: {e}"))
    return photos, problems


def uid_from_photo_name(filename):
    """faces/<uid>.jpg, <uid>_2.jpg, ... -> uid. None if not a photo."""
    stem, extension = os.path.splitext(filename)
    if extension.lower() not in PHOTO_EXTENSIONS or not stem or stem.startswith("."):
        return None
    numbered = re.fullmatch(r"(.+)_\d+", stem)
    return numbered.group(1) if numbered else stem


def photos_from_folder(faces_dir, face_uids):
    """ReferencePhotos from faces/<uid>.jpg files. Returns (photos, problems)."""
    if not os.path.isdir(faces_dir):
        raise SetupError(f"There is no {faces_dir} folder. Create it and add <uid>.jpg photos, "
                         "or set FACE_SOURCE=spring.")
    photos = []
    for name in sorted(os.listdir(faces_dir)):
        uid = uid_from_photo_name(name)
        if uid is None or (face_uids and uid not in face_uids):
            continue
        with open(os.path.join(faces_dir, name), "rb") as photo_file:
            photos.append(ReferencePhoto(uid, f"faces/{name}", photo_file.read()))
    return photos, []


def load_reference_photos(camera_config):
    """All reference photos from the configured source, plus the problems
    found. FACE_UIDS entries with no photo at all are problems too."""
    face_uids = camera_config["face_uids"]
    if camera_config["face_source"] == "spring":
        spring_url = camera_config["spring_url"]
        with login_to_spring(spring_url, camera_config["spring_uid"], camera_config["spring_password"]) as session:
            faces = fetch_spring_faces(session, spring_url, camera_config["spring_uid"])
        photos, problems = photos_from_spring(faces, face_uids)
        no_photo = "listed in FACE_UIDS, but has no face registered in Spring"
    else:
        photos, problems = photos_from_folder(camera_config["faces_dir"], face_uids)
        no_photo = "listed in FACE_UIDS, but has no photo in faces/"

    if face_uids:
        found = {photo.uid for photo in photos} | {uid for uid, _ in problems}
        problems += [(uid, no_photo) for uid in sorted(face_uids - found)]
    return photos, problems


def describe_source(camera_config):
    """One line saying where reference faces come from, for log messages."""
    if camera_config["face_source"] == "spring":
        source = f"Spring ({camera_config['spring_url']}, logged in as {camera_config['spring_uid']})"
    else:
        source = f"the folder {camera_config['faces_dir']}"
    if camera_config["face_uids"]:
        source += f", only FACE_UIDS: {', '.join(sorted(camera_config['face_uids']))}"
    return source
