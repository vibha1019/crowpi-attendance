# CrowPi Attendance Prototype

A working prototype: a CrowPi at the classroom door with an RFID scan pad,
~20 tags, a Flask + SQLite backend, and a live dashboard. One tap toggles a
student between entered/exited; presence state (present/tardy/stepped
out/left early/absent) is computed against the active class period.

## 1. Run the backend

```bash
cd crowpi-attendance
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Dashboard: http://localhost:5050

## 2. Quick test with no hardware at all

```bash
python seed.py                        # 4 demo students + a 2-minute demo period
cd reader
python reader.py --simulate
```

Type a fake UID and press enter to simulate a tap, e.g. `demo-001` to mark
Alice present, then `demo-001` again to toggle her to exit. Watch the
dashboard update live.

## 3. Register the real tags

Once the CrowPi and tags are in hand, run on the CrowPi itself (registration
mode prompts for a name on every scan instead of logging attendance):

```bash
cd reader
pip install -r requirements.txt       # Pi-only deps (mfrc522, spidev, RPi.GPIO)
python reader.py --register --backend http://<backend-host>:5050
```

Scan each of the ~20 tags once and type the student's name when prompted.

## 4. Start a real class period

```bash
curl -X POST http://localhost:5050/api/admin/period/start \
  -H "Content-Type: application/json" \
  -d '{"name": "Period 3", "duration_seconds": 2700, "grace_seconds": 300}'
```

(`duration_seconds`/`grace_seconds` can be set short for a live demo, e.g.
120s/15s, so the tardy/absent/left-early states can actually be shown off
without waiting through a real class period.)

## 5. Run attendance for real

```bash
cd reader
python reader.py --backend http://<backend-host>:5050
```

## Known limitation

This is a single checkpoint (one CrowPi, one scan pad) — it can tell you a
tag was scanned, not which direction someone was walking, so enter/exit is
inferred by toggling. Good enough for a working demo; a two-reader (front
door/back door) setup would remove the ambiguity if that becomes necessary
later.
