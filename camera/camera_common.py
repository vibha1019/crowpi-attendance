"""Small pieces shared by every part of the camera client: the answer for
one tap, the error types, and the UTC timestamp format OCS expects."""

from collections import namedtuple
from datetime import datetime, timezone

RESULTS = ("match", "no_match", "no_face", "not_enrolled")
# Servers on this machine. Rehearsal mode only talks to these, and a
# Spring URL needs https unless it is one of these.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# One decision about one tap. taken_at is when the picture was taken (in
# the test modes: when the answer was picked), as a UTC ISO string.
# confidence is the face score from 0 to 1, or None when nothing was
# recognized (no face, not enrolled, or a test mode).
Answer = namedtuple("Answer", ["result", "uid", "taken_at", "confidence"], defaults=[None])


class SetupError(Exception):
    """Something the operator has to fix before this can run (missing
    config, models or OpenCV, a bad API key or password). Retrying will
    not help."""


class CameraError(Exception):
    """The webcam or OpenCV failed for one tap. That tap gets no result, so
    the dashboard shows it as Unverified, which is the honest answer."""


def utc_iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
