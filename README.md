# CrowPi Attendance Reader

The RFID reading point for the Classroom Presence System. A CrowPi at the
classroom door reads tag taps and posts each one to OCS (`flask_csh`), which
resolves the tag to a student, applies the bell schedule, and records the
attendance event. Live attendance is shown on the Presence dashboard in
`pages_csh` (`/capstone/presence-system/dashboard/`).

This repo only contains the reader (`reader/`). The backend lives in
`flask_csh` (`api/presence_api.py`, `api/rfid_api.py`).

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
| `CLASSROOM_ID` | OCS classroom id to log attendance against                    |
| `DEVICE_ID`    | Optional label for this reader, e.g. `crowpi-room1`           |

The CrowPi's clock must be NTP-synced: each tap is stamped with the time it
happened on the device.

## 2. Test from a laptop (no hardware)

```bash
pip install requests
set -a; source reader.env; set +a
python reader.py --simulate
```

Type a registered tag UID and press enter to simulate a tap.

## 3. Run on the CrowPi

```bash
cd reader
pip install -r requirements.txt       # Pi-only deps (mfrc522, spidev, RPi.GPIO)
sudo cp crowpi-reader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crowpi-reader
journalctl -u crowpi-reader -f        # watch taps
```

The unit loads `reader/reader.env` via `EnvironmentFile`.

## Registering tags

Tags are bound to OCS users through OCS's admin-authenticated
`POST /api/rfid/register` (`{"tag_uid": "...", "user_id": ...}`). A tap from
an unregistered tag is reported as `unregistered` and not logged.

## Known limitation

This is a single checkpoint (one CrowPi, one scan pad): it can tell a tag
was scanned, not which direction someone was walking, so enter/exit is
inferred by toggling within each class period.
