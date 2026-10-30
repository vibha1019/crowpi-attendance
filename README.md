# Presence Reader

The RFID reading point for the Classroom Presence System. A Raspberry Pi at the
classroom door reads tag taps and posts each one to OCS (`flask_csh`), which
resolves the tag to a student, applies the bell schedule, and records the
attendance event. Live attendance is shown on the Presence dashboard in
`pages_csh` (`/capstone/presence-system/dashboard/`).

This repo contains the reader (`reader/`) and the camera client for the
Mac Mini (`camera/`, see [Camera (Mac Mini)](#camera-mac-mini)). The
backend lives in `flask_csh` (`api/presence_api.py`, `api/rfid_api.py`).

## 1. Configure

```bash
cd reader
cp reader.env.example reader.env   # untracked, never commit it
```

Fill in `reader.env`:

| Variable       | Meaning                                                       |
|----------------|---------------------------------------------------------------|
| `BACKEND_URL`  | OCS base URL, e.g. `http://192.168.1.242:8587`                |
| `RFID_API_KEY` | Must match `RFID_API_KEY` in `flask_csh/.env`                 |
| `ROOM_ID`      | OCS presence room id the taps belong to (`/api/presence/rooms`) |
| `DEVICE_ID`    | Optional label for this reader, e.g. `pi-room1`               |

The Pi's clock must be NTP-synced: each tap is stamped with the time it
happened on the device.

## 2. Test from a laptop (no hardware)

```bash
pip install requests
set -a; source reader.env; set +a
python reader.py --simulate
```

Type a registered tag UID and press enter to simulate a tap.

## 3. Run on the Raspberry Pi

```bash
cd reader
pip install -r requirements.txt       # Pi-only deps (mfrc522, spidev, RPi.GPIO)
sudo cp presence-reader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now presence-reader
journalctl -u presence-reader -f      # watch taps
```

The unit loads `reader/reader.env` via `EnvironmentFile`.

## Registering tags

Tags are bound to OCS users through OCS's admin-authenticated
`POST /api/rfid/register` (`{"tag_uid": "...", "user_id": ...}`). A tap from
an unregistered tag is reported as `unregistered` and not logged.

## Known limitation

This is a single checkpoint (one Pi, one scan pad): it can tell a tag
was scanned, not which direction someone was walking, so enter/exit is
inferred by toggling within each class period.

## Camera (Mac Mini)

`camera/camera_client.py` is the camera half of the tap station
([Jarvis3000#17](https://github.com/Arnav210/Jarvis3000/issues/17),
[#13](https://github.com/vibha1019/crowpi-attendance/issues/13)). It runs
on the Mac Mini, with the Logitech Brio on the monitor next to the Raspberry Pi.

About once a second it asks OCS for new taps. For each tap it looks at the
person at the tap station, compares their face with the registered faces,
and sends one result back. Face recognition runs on the Mac Mini, with
OpenCV's ready-made YuNet (finds faces) and SFace (tells faces apart)
models. Pictures are never saved and never sent (except a reference photo
you take on purpose with `--enroll`). Only the result goes to OCS.

To try it all on a laptop first, see [Feature demo on a
laptop](#feature-demo-on-a-laptop).

### Modes

| Mode        | What it sends                          | Use it for                                                      |
|-------------|----------------------------------------|-----------------------------------------------------------------|
| `camera`    | Face recognition with the Brio         | The real thing: the demo                                        |
| `no-face`   | `no_face` for every tap, no camera     | Checking OCS and the API key from a new machine                 |
| `rehearsal` | Whatever a person types for each tap   | Practicing on a local OCS only. Never against the classroom server |

**Rehearsal is not face recognition.** A person types the result. Never
present rehearsal results as recognition. The tool refuses to run it
against anything but a local OCS (`http://localhost:8587`).

### What each result means on the dashboard

| Camera sends                   | When                                                   | Dashboard shows |
|--------------------------------|--------------------------------------------------------|-----------------|
| `match`, uid = the tapper      | The face is the student whose tag was tapped          | Verified        |
| `match`, uid = someone else    | The face is another registered student (proxy tap)    | Mismatch        |
| `no_match`                     | A face, but nobody registered is close enough         | Mismatch        |
| `no_face`                      | No face seen in 3 seconds                              | No face         |
| `not_enrolled`                 | The tapper has no registered face (opted out, or not in `FACE_UIDS`) | Tap only |
| nothing                        | Camera client off, or the camera failed on that tap   | Checking..., then Unverified after 15 s ("RFID only" while `CAMERA_ENABLED` is off in `flask_csh`) |

Each result also carries `confidence`: the face score from 0 to 1 (see
below). Attendance itself (Present, Tardy, Stepped Out...) still comes
from the taps and the bell schedule.

### How camera mode decides

1. If the student who tapped has no reference face, it sends
   `not_enrolled` right away. It does not look at the camera.
2. Otherwise it looks at new camera frames for up to 3 seconds
   (`FACE_WAIT_SECONDS`), since students often look down at the pad
   first. In each frame it takes the largest face: the person closest to
   the camera.
3. It compares that face with every reference face. The score is how
   alike two faces are, from about 0 to 1. At or above
   `MATCH_THRESHOLD` (0.363, OpenCV's standard value for SFace) it counts
   as the same person.
4. A frame that matches the student who tapped decides at once
   (`match`). If a frame matches someone else, it keeps looking until the
   time is up, and only then sends `match` with that person's uid (a
   proxy tap). If no frame matched anyone, it sends `no_match` with the
   closest score. If it never saw a face, it sends `no_face`.

The score is a similarity, not a percentage: a correct match often scores
well under 1.

### One-time setup on the Mac Mini

```bash
cd crowpi-attendance/camera
python3 -m venv venv               # a private Python for this folder
source venv/bin/activate
pip install -r requirements.txt    # requests, OpenCV, numpy
./get_models.sh                    # downloads the two face models (about 40 MB)
cp camera.env.example camera.env   # untracked, never commit it
chmod 600 camera.env               # it holds a key and a password
```

Then fill in `camera.env` (every setting is explained in the file):

- `BACKEND_URL`, `RFID_API_KEY` and `ROOM_ID`: the same values as the
  Pi's `reader.env`.
- `SPRING_UID` and `SPRING_PASSWORD`: a Teacher account on Spring, so it
  can read the registered faces. Admin works too, but it can do far more
  than read faces, so prefer a Teacher account. A student account gets
  "403". Keep the password in single quotes. No Teacher login? Use
  `FACE_SOURCE=folder` instead (see Troubleshooting).
- `FACE_UIDS`: the demo team's OCS uids, e.g. `vibham,kush1434`. Spring
  still sends every registered face at startup, but only these are kept.
- `CAMERA_INDEX`: found in step 2 below.

If you used an older `camera.env`: `CLASSROOM_ID` still works, but rename
it to `ROOM_ID`. Delete any `DEVICE_ID=camera-test-tool` line, so camera
mode labels its results `mac-mini-cam`.

Every new Terminal window needs:

```bash
cd crowpi-attendance/camera
source venv/bin/activate
set -a; source camera.env; set +a
```

### Testing on the real camera, step by step

1. **Load the settings** (the three lines above). Run everything in
   Terminal on the Mac Mini's own screen, not over SSH: macOS only lets
   an app you approved use the camera, and the preview window needs the
   screen.
2. **Find the Brio:**
   ```bash
   python camera_client.py --list-cameras
   ```
   The first time, macOS asks whether Terminal may use the camera. Click
   Allow, quit Terminal fully (Cmd+Q), reopen it, and run this again.
   The Brio should show as `works, <width>x<height>`. Any size works
   (it asks for 1280x720, but macOS may pick another); note it down. At
   640x480, people should stand within about 1 m. Put its number in
   `camera.env` as `CAMERA_INDEX`, and load the settings again.
3. **Check the reference faces:**
   ```bash
   python camera_client.py --check-faces
   ```
   Every demo team member should have an `ok` line. A `!` line says what
   is wrong, e.g. "no face found in it": retake that photo on the OCS
   profile page, facing the camera in good light. This check needs no
   camera and no OCS, so you can run it on any Mac the night before, to
   be sure the Spring login works.
4. **Aim and tune with the live preview:**
   ```bash
   python camera_client.py --preview
   ```
   A window shows each face with a box, the closest person's uid and the
   score. Green means a match, red means no match. The thick box is the
   face a tap would be checked against. Aim the Brio so the person
   tapping gets the thick box. Have each teammate stand where they would
   tap and check that their box is green. To quit, click the preview
   window and press `q` (or press Ctrl+C in Terminal). Nothing is saved
   or sent. The bottom line shows frames per second. If it is under
   about 10, change `MAX_DETECT_SIDE` in `face_engine.py` from 1280 to
   640 (faster, but faces have less detail).
5. **Make sure taps reach OCS:** the Pi reader is running, and the
   room has a period going (a schedule block, or start one with
   `POST /api/presence/rooms/<id>/periods`, Teacher or Admin login).
   Taps outside a period are ignored and never reach the camera.
6. **Run the camera.** `caffeinate -i` keeps the Mac from going to
   sleep while it runs:
   ```bash
   caffeinate -i python camera_client.py --mode camera
   ```
   It loads the faces, turns on the camera, and prints
   `Connected to ..., watching room 1.` Tap a tag. Within a few seconds:
   ```
   Tap by vibham (enter) at 2026-10-21T18:42:07.123Z
   tap 3f2b8c1e: match (vibham, score 0.62) -> VERIFIED
   ```
   The last word is what OCS stored (`VERIFIED`, `MISMATCH`, `NO_FACE`
   or `TAP_ONLY`). The dashboard should show the same. You can also check
   `GET /api/presence/rooms/<id>/status` (Teacher or Admin login).
7. **Try the demo scenarios** from #13: a normal tap (Verified), tapping a
   teammate's tag (Mismatch), someone not in `FACE_UIDS` (Tap only), and
   turning away from the camera (No face). For the proxy tap, the tag's
   owner must stay out of the camera's view: if the camera sees the owner
   at any point during the look, it sends Verified. For normal taps,
   teammates should stand back, so the tapper is the largest face.

Stop it with Ctrl+C. Only run one camera client per room: each tap takes
one result, so a second client gets `already_checked`.

**Tuning.** If the right person often gets `no_match`, first improve the
reference photo or the light. Then try lowering `MATCH_THRESHOLD` a
little (e.g. 0.33). If someone is matched as the wrong person, raise it.
Use `--preview` to see real scores before changing it.

### Privacy

- Camera pictures stay in memory for a moment and are never saved or
  sent. `--preview` only shows them on screen. The one exception is
  `--enroll <uid>`: it saves one photo to `camera/faces/` when you press
  space, and nothing else.
- Spring sends every registered face (photo and name) each time the
  client starts, into memory only. Set `FACE_UIDS` so only the demo
  team's are kept; the rest are dropped right away. Nothing is written to
  disk.
- OCS only gets the result: which result, the recognized uid, and the
  score.
- `camera.env` holds the device key and a Teacher password. Keep it
  `chmod 600` and never commit it (it is gitignored).
- The Mac Mini's screen faces the tap station. During class, minimize
  Terminal (or turn the monitor off) and close `--preview` after aiming:
  both show students' uids and results.
- The camera stays on (its light is on) the whole time the client runs.
  A picture is only checked when someone taps.
- With `FACE_SOURCE=folder`, photos in `camera/faces/` are gitignored.
  Delete them after the demo.

### Troubleshooting

| Problem | Fix |
|---|---|
| "Could not open camera" or "opened but sent no picture" | Camera permission: System Settings > Privacy & Security > Camera, turn on Terminal (or iTerm / VS Code, whichever you run it from). Quit that app fully and reopen it. |
| The preview shows the wrong camera | Run `--list-cameras` and set `CAMERA_INDEX`. An iPhone near the Mac Mini can appear as a camera (Continuity Camera) and shift the numbers. Move it away, or turn off Continuity Camera on the phone (Settings > General > AirPlay & Continuity). |
| The Brio is not listed | Plug it straight into the Mac Mini, not a hub, and try another port. `system_profiler SPCameraDataType` lists the cameras macOS sees. Only its normal camera is used; its IR sensor is not. |
| "No usable reference faces" | Run `--check-faces`. Check the `FACE_UIDS` spelling (uids are case-sensitive). If Spring is down, set `FACE_SOURCE=folder` and put photos in `camera/faces/` named `<uid>.jpg` (more as `<uid>_2.jpg`), or take them with `python camera_client.py --enroll <uid>`. |
| Spring login fails (401) | Wrong `SPRING_UID` or `SPRING_PASSWORD`. Keep the password in single quotes in `camera.env`. |
| Spring says 403 | That account is not a Teacher or Admin. Use a Teacher login, or set `FACE_SOURCE=folder` (see "No usable reference faces"). |
| "Spring sent its login cookie only for https://*.opencodingsociety.com" | You pointed `SPRING_URL` at a local Spring on its default settings. Use `https://spring.opencodingsociety.com`, or start the local Spring with `jwt.cookie.secure=false`. |
| "Could not reach Spring ... (SSLError)" | macOS's built-in Python 3.9 has an old SSL library. Make the venv with a newer Python if the Mac has one (`python3.12 -m venv venv`), or run `pip install 'urllib3<2'` in the venv. |
| "OCS refused the pending-taps request (401)" | `RFID_API_KEY` does not match `flask_csh/.env`. A 404 "no room" means `ROOM_ID` is wrong. |
| "has a face in Spring but no user in OCS" | That uid exists in Spring but not in the classroom OCS. The uids must be the same in both. The tap stays Unverified. |
| "RFID_API_KEY ... hidden characters" | Retype the key in `camera.env` (no spaces inside it). |
| Every tap says "Skipping tap ... too late" | The Mac Mini's or the Pi's clock is off. Turn on System Settings > General > Date & Time > Set time automatically on the Mac, and check `timedatectl` on the Pi says the clock is synchronized. |
| Every tap is `no_face` | Aim the camera with `--preview`. Check the light. Raise `FACE_WAIT_SECONDS` (up to 5). |
| Camera worked, then every tap says "no new picture" | The Brio was unplugged or the Mac slept. Plug it back in and restart the client (OpenCV does not reconnect). Start it with `caffeinate -i`. |
| Every tap is `not_enrolled` | The line after each tap says why: not in `FACE_UIDS`, a photo that could not be used, or a uid spelled differently. Run `--check-faces`. |
| "not stored: that tap already had a camera result" | Another camera client is running for this room. Stop it. |
| "Face model file(s) missing" | Run `./get_models.sh`. If it reports a tiny file, open the printed link in a browser and save the file into `camera/models/`. |
| `No module named cv2` | Activate the venv: `source venv/bin/activate`. |
| The preview fails with "function is not implemented" | The headless OpenCV is installed. Run `pip uninstall -y opencv-python-headless opencv-python` then `pip install -r requirements.txt`. |
| A `NotOpenSSLWarning` at start | Usually harmless. It comes from macOS's built-in Python 3.9. If Spring then fails with `SSLError`, see that row. |

### Known limits

- No anti-spoofing. It does not check that the face is a real, live
  person: a photo or a phone screen showing a registered student's face
  will match. The Brio's IR sensor is not used. Treat Verified as "looks
  like the student", not proof.
- Anyone can change a student's registered face without logging in
  (Spring's `POST /api/face/register/public`). Someone could put their
  own face on a classmate's uid, then tap that classmate's tag and show
  as Verified. Until Spring requires a login there, a registered face is
  not trustworthy.
- A tap on a tag whose owner has no registered face (opted out) is never
  checked by the camera, so a proxy tap on that tag is not caught.
- If the tag's owner is in view during a proxy tap (for example behind
  the person tapping), the camera may see the owner and send Verified.
- The camera only checks the person at the tap station (the largest
  face), not who is in the room.
- If students tap within a few seconds of each other, the next student's
  face can be checked for the previous tap.
- New faces registered in Spring are only picked up when the client is
  restarted.

### Checking OCS without a camera (no-face mode)

```bash
python camera_client.py --mode no-face
```

Tap a registered tag (or use `reader.py --simulate`). You should see
`tap 3f2b8c1e: no_face -> NO_FACE`. That proves OCS, the API key and
the network work from this machine. Don't run it during a real class:
every tap it answers gets a permanent "No face". `DEVICE_ID` defaults to
`camera-test-tool` here, so these results can be told apart.

Other options: `--once` answers whatever is pending right now and exits.
`--poll-seconds 2` polls less often; `--poll-seconds 0.5` picks taps up
sooner.

### Practicing the demo without a camera (rehearsal mode)

Run this only against a local `flask_csh` with a throwaway database
(`BACKEND_URL=http://localhost:8587`); the tool refuses any other server.
Never use it at the demo.

```bash
python camera_client.py --mode rehearsal
```

For each tap, type the result the camera would send:

| #13 scenario             | Type                                        | Should show                          |
|--------------------------|---------------------------------------------|--------------------------------------|
| 1. Normal arrival        | `m` (match the student who tapped)          | Present, Verified                    |
| 2. Proxy tap             | `o`, then the uid of who really tapped      | Mismatch                             |
| 3. Opt-out               | `e` (`not_enrolled`)                        | Present, Tap only                    |
| 4. Tardy and stepped out | `m` for each of the two taps (in, then out) | Tardy, then Stepped Out (from taps)  |
| 5. Network drop          | Often nothing (see below)                   | The tap, with the time it happened   |

`n` (`no_match`) and `f` (`no_face`) are there too. In scenario 5 the tap
reaches OCS late, so the tool usually skips it (more than 8 seconds old)
and does not ask. Results are stored with `DEVICE_ID`
`camera-test-tool-rehearsal`, so they show a person typed them.

### How it behaves (all modes)

- Each tap gets exactly one result. Taps older than 8 seconds are
  skipped, since the student may have walked off.
- If OCS says a matched uid doesn't exist, rehearsal mode asks about
  that tap again (to fix a typo). Camera mode does not, since it would
  see the same face again; that tap stays Unverified.
- If the camera or OpenCV fails on a tap, or something unexpected goes
  wrong, that tap gets no result and the next tap is still answered.
- If OCS can't be reached, a result is retried a few times, then queued
  and retried every 5 seconds, like the reader. Retries reuse the same
  `event_id`, so nothing is stored twice.
- A tap with no result shows as Unverified, which is better than a
  wrong result.

### Tests

```bash
cd camera
python3 -m unittest -v
```

No network, camera or OpenCV needed: the tests run a small fake OCS and
a fake Spring on localhost, and use a fake OpenCV.

## Feature demo on a laptop

`demo/demo.py` runs the whole camera check on one Mac, with no Raspberry Pi and
no Mac Mini:

- Vibha's presence backend (`flask_csh`) on this Mac, on a throwaway
  database, with a room, three people (Kush, Vibha, Ruta) and their tags,
  and a 60-minute demo period.
- The camera client in camera mode on the laptop's webcam. This is the
  real face check, the same code the Mac Mini will run.
- A page with a tap button per person in place of the RFID reader, and
  the live roster with each tap's camera check.

Only the tap is a stand-in. Moving to the Mac Mini later needs no code
change: the same `camera_client.py` runs there with `camera.env`.

### One-time setup

1. The camera setup from [One-time setup on the Mac
   Mini](#one-time-setup-on-the-mac-mini) (venv, requirements, models).
   No `camera.env` is needed for the demo.
2. A checkout of Vibha's `flask_csh` with the presence system, next to
   this folder, using `flask_csh`'s existing venv:
   ```bash
   git -C ../flask_csh fetch origin
   git -C ../flask_csh worktree add --detach ../flask_csh_demo origin/main
   ```
3. Reference photos, taken in Terminal (it needs the camera):
   ```bash
   cd camera
   venv/bin/python camera_client.py --enroll kush
   ```
   Face the camera. When the box turns green, click the camera window
   and press space. The photo is saved as `camera/faces/kush.jpg`. For
   the Mismatch scenario, also enroll `vibha`: Vibha in front of the
   camera, or a photo of her held up on a phone. Leave `ruta` without a
   photo to show Tap only. If the window shows the wrong camera (an
   iPhone nearby can be one), put `CAMERA_INDEX=1` in front of the
   command.

### Running it

From the `crowpi-attendance` folder, in Terminal:

```bash
camera/venv/bin/python demo/demo.py --flask-dir ../flask_csh_demo --flask-python ../flask_csh/venv/bin/python
```

The page opens at http://127.0.0.1:8600. Click a tap button (or press
1, 2 or 3) and watch the Camera check column:

| Do this                                         | Shows                       |
|-------------------------------------------------|-----------------------------|
| Tap Kush's card, Kush facing the camera         | Present, Verified           |
| Tap Vibha's card, Kush facing the camera        | Present, Mismatch           |
| Tap Kush's card again, nobody in view           | Stepped out, No face        |
| Tap Ruta's card (no photo on file)              | Present, Tap only           |

Verified comes back in about a second. Mismatch and No face take about
4 seconds, since the camera keeps looking for the right face for 3
seconds. "Start a new demo period" sets everyone back to Not yet arrived.
Ctrl+C stops everything.

- `--camera-index 1` picks another camera (a Brio plugged into the
  laptop, for example).
- The people and their tags are in `PEOPLE` at the top of `demo/demo.py`.
- Everything runs on 127.0.0.1. The API key and teacher password are
  random for each run and never printed. The database is deleted when the
  demo stops. Reference photos stay in `camera/faces/` (gitignored):
  delete them when you're done.
- This page is not the Pages dashboard. It shows the same states, with
  the same words, from the same status API.
