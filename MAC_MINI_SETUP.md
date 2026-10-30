# Mac mini setup: read this first

This file is for whoever sets up the camera demo on the Mac mini, including
a Claude session that has no other context. Follow it top to bottom.

## What this is

Kush Shah's part of the Classroom Presence System, a Del Norte High School
capstone (team: Vibha Mandayam, RFID reader and backend; Ruta Sirdeshmukh;
Kush Shah, camera).

A student is read at the classroom door by an RFID reader. Vibha's backend
(`flask_csh`) records the read. The camera software in this folder looks at
the person at the door, compares their face with each student's reference
photo, and sends one result back: `match`, `no_match`, `no_face` or
`not_enrolled`. The teacher's dashboard shows these as Verified, Mismatch,
No face and Tap only.

Face recognition runs on this computer with OpenCV (YuNet finds faces,
SFace compares them). Camera pictures stay in memory and are never saved or
sent. The only pictures saved are reference photos taken on purpose with
`--enroll`, in `camera/faces/`.

## What's in this folder

- `camera/`: the camera client (`camera_client.py`) and its tests. The face
  models are already in `camera/models/`.
- `demo/`: a one-command demo that runs everything on one computer: Vibha's
  backend on a throwaway database, the camera client, and a web page with tap
  buttons that stand in for the RFID reader.
- `README.md`: full details. The sections that matter here are
  "Camera (Mac Mini)" and "Feature demo on a laptop".

## Where it stands

- Tested on Kush's MacBook on Oct 20, 2026. All 129 unit tests pass. The
  demo passed every scenario (Verified, Mismatch, No face, Tap only) with the
  real face models, using still photos in place of a live webcam.
- It has never run on this Mac mini's webcam. That's the goal now.

## Goal

Run the demo on this Mac mini with its webcam, and show Kush each result on
the demo page.

## Steps

Run these from the `crowpi-attendance` folder unless a step says otherwise.

1. **Check Python.** `python3 --version`. 3.9 or newer works; 3.12 is best.
   If `python3` is missing, macOS offers to install the developer tools:
   accept that.

2. **Set up the camera client.**
   ```bash
   cd camera
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   venv/bin/python -m unittest
   cd ..
   ```
   The tests should end with `OK` (129 tests). If `camera/models/` is
   missing or empty, run `camera/get_models.sh`.

3. **Set up Vibha's backend next to this folder.** Use commit `2bddb96`:
   it's the version the demo was tested with. Her `main` branch has since
   changed the login and user-account code, and the demo hasn't been tested
   against it.
   ```bash
   cd ..
   git clone https://github.com/vibha1019/flask_csh flask_csh_demo
   cd flask_csh_demo
   git checkout 2bddb96
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   cd ../crowpi-attendance
   ```
   Don't change anything in `flask_csh_demo`.

4. **Find the webcam.**
   ```bash
   camera/venv/bin/python camera/camera_client.py --list-cameras
   ```
   macOS asks whether the app running the command (Claude or Terminal) may
   use the camera. Kush must click **Allow**, then quit and reopen that app
   if the camera still doesn't open. If Claude's own shell still can't open
   the camera after that, ask Kush to run steps 4 to 6 in the Terminal app
   instead, and give him the exact commands. Note which camera number is the
   webcam (often 0; an iPhone nearby can also show up as a camera).

5. **Take Kush's reference photo.**
   ```bash
   camera/venv/bin/python camera/camera_client.py --enroll kush
   ```
   Put `CAMERA_INDEX=1` (or the right number) in front if the webcam isn't
   camera 0. A window opens. Kush faces the camera, and when the box turns
   green he clicks the window and presses space. The photo is saved as
   `camera/faces/kush.jpg`. For the Mismatch scenario, a second person can
   be enrolled the same way with `--enroll vibha`. Leave `ruta` without a
   photo, to show Tap only.

6. **Start the demo.**
   ```bash
   camera/venv/bin/python demo/demo.py --flask-dir ../flask_csh_demo --flask-python ../flask_csh_demo/venv/bin/python
   ```
   Add `--camera-index 1` (or the right number) if needed. The page opens at
   http://127.0.0.1:8600. Click a tap button, or press 1, 2 or 3.

   | Do this | The page should show |
   | --- | --- |
   | Tap Kush's card, Kush facing the camera | Present, Verified (about 1 s) |
   | Tap Vibha's card, Kush facing the camera | Present, Mismatch (about 4 s; needs a `vibha` photo) |
   | Tap Kush's card again, nobody in view | Stepped out, No face (about 4 s) |
   | Tap Ruta's card | Present, Tap only |

   "Start a new demo period" resets everyone. Ctrl+C in the terminal stops
   everything and deletes the throwaway database.

## Rules

- This is a shared computer. Don't commit or push anything, and don't sign
  in to GitHub.
- Never print, save or ask for API keys or passwords. The demo makes its own
  random ones and never shows them.
- Don't save camera pictures, except the `--enroll` photos in
  `camera/faces/`. Delete those when Kush asks.
- If another program is using the webcam (for example the Jarvis team's
  software), ask Kush before closing it.

## If something goes wrong

| Problem | Fix |
| --- | --- |
| "Could not open camera" or "no camera" | Camera permission: System Settings > Privacy & Security > Camera, turn on the app running the command, then quit and reopen it. Or run the command in Terminal. |
| The window shows the wrong camera | Use another number: `CAMERA_INDEX=1` for `--enroll`, `--camera-index 1` for the demo. |
| "has no reference photo" | Do step 5 first. |
| "Can't open the demo page on port 8600" | Add `--port 8601`. |
| "The backend stopped while starting" | The message shows the backend's last lines. Usually a missing package: rerun step 3's `pip install`. |
| A tap shows Unverified | The camera client stopped. Its last lines are in the "Camera client" box on the page. |

## Later, not now

Running against the real classroom server needs `BACKEND_URL`,
`RFID_API_KEY` and `ROOM_ID` from Vibha, in `camera/camera.env` (see
README, "One-time setup on the Mac Mini"). Kush has to get those from Vibha;
never guess them.
