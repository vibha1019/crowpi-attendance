"""A tiny fake OpenCV (and numpy) for the unit tests, so they run without
OpenCV, model files or a webcam.

A "picture" is a FakeImage: a list of (width, person) faces plus a shape.
A photo file is JSON bytes describing one (see photo_bytes). The fake
detector finds exactly those faces, and the fake recognizer scores 0.9 for
the same person and 0.1 for anyone else, unless a test sets other scores.
"""

import json


class FakeCvError(Exception):
    pass


class FakeImage(list):
    def __init__(self, faces=(), shape=(480, 640, 3)):
        super().__init__(faces)
        self.shape = tuple(shape)

    def copy(self):
        return FakeImage(self, self.shape)


def photo_bytes(faces, shape=(480, 640, 3)):
    """Bytes the fake imdecode turns into a FakeImage with these faces."""
    return json.dumps({"faces": faces, "shape": list(shape)}).encode()


class FakeFeature:
    def __init__(self, person):
        self.person = person

    def copy(self):
        return FakeFeature(self.person)


class FakeBuffer:
    def __init__(self, data):
        self.data = data
        self.size = len(data)


class FakeNumpy:
    uint8 = "uint8"

    @staticmethod
    def frombuffer(data, dtype=None):
        return FakeBuffer(bytes(data))


class FakeCapture:
    """A fake VideoCapture that hands out the given frames, then fails."""

    def __init__(self, frames=(), opened=True, failed_reads_first=0):
        self.frames = list(frames)
        self.opened = opened
        self.failed_reads_first = failed_reads_first
        self.settings = {}
        self.released = False

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.settings[prop] = value
        return True

    def read(self):
        if self.failed_reads_first > 0:
            self.failed_reads_first -= 1
            return False, None
        if not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def release(self):
        self.released = True


class FakeCv2:
    __version__ = "4.99-fake"
    error = FakeCvError
    FaceRecognizerSF_FR_COSINE = 0
    INTER_AREA = 3
    IMREAD_COLOR = 1
    CAP_ANY = 0
    CAP_AVFOUNDATION = 1200
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4

    def __init__(self):
        self.fail_detect = False
        self.fail_model_load = False
        self.match_scores = {}  # (seen person, reference person) -> score, overrides 0.9 / 0.1
        self.resized_to = []    # (width, height) of every resize
        self.input_sizes = []   # every setInputSize
        self.cameras = {}       # camera number -> FakeCapture
        self.opened = []        # (camera number, backend) of every VideoCapture
        cv2 = self

        class Detector:
            def setInputSize(self, size):
                cv2.input_sizes.append(tuple(size))

            def detect(self, image):
                if cv2.fail_detect:
                    raise FakeCvError("simulated OpenCV failure in detect()")
                rows = [(0, 0, width, width, person) for width, person in image]
                return 1, (rows or None)

        class Recognizer:
            def alignCrop(self, image, face):
                return face[4]

            def feature(self, aligned):
                return FakeFeature(aligned)

            def match(self, first, second, distance_type):
                assert distance_type == FakeCv2.FaceRecognizerSF_FR_COSINE
                same = 0.9 if first.person == second.person else 0.1
                return cv2.match_scores.get((first.person, second.person), same)

        def make_factory(cls):
            def create(*args):
                if cv2.fail_model_load:
                    raise FakeCvError("simulated: cannot read the ONNX model")
                return cls()
            return type("Factory", (), {"create": staticmethod(create)})

        self.FaceDetectorYN = make_factory(Detector)
        self.FaceRecognizerSF = make_factory(Recognizer)

    def imdecode(self, buffer, flags):
        try:
            photo = json.loads(buffer.data)
        except ValueError:
            return None  # like the real imdecode: unreadable data gives None
        return FakeImage([tuple(face) for face in photo["faces"]], photo["shape"])

    def resize(self, image, size, interpolation=None):
        self.resized_to.append(tuple(size))
        return FakeImage(image, (size[1], size[0], 3))

    def VideoCapture(self, index, backend):
        self.opened.append((index, backend))
        return self.cameras.get(index) or FakeCapture(opened=False)
