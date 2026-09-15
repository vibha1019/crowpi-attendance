import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, request

from models import Event, Period, Student, db, utc_now

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'attendance.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)


def get_active_period():
    return Period.query.filter_by(active=True).order_by(Period.id.desc()).first()


def compute_state(student, period, now):
    events = (
        Event.query.filter_by(student_id=student.id, period_id=period.id)
        .order_by(Event.timestamp)
        .all()
    )
    window_open = now < period.end_time

    if not events:
        return "NOT_YET_ARRIVED" if window_open else "ABSENT"

    first_in = next((e for e in events if e.type == "enter"), None)
    late = first_in is not None and first_in.timestamp > period.start_time + timedelta(
        seconds=period.grace_seconds
    )
    currently_in = events[-1].type == "enter"

    if currently_in:
        return "TARDY" if late else "PRESENT"

    return "TEMP_OUT" if window_open else "LEFT_EARLY"


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/scan", methods=["POST"])
def scan():
    """Single entry point for every tag scan from the reader. No CLI mode
    flag needed: a brand new tag is parked as pending for an admin to name
    in the web panel, a known tag just logs an attendance event."""
    data = request.get_json(force=True)
    tag_uid = str(data.get("tag_uid", "")).strip()
    if not tag_uid:
        return jsonify({"error": "tag_uid required"}), 400

    student = Student.query.filter_by(tag_uid=tag_uid).first()
    if not student:
        student = Student(tag_uid=tag_uid, name=f"Unnamed ({tag_uid})", pending=True)
        db.session.add(student)
        db.session.commit()

    if student.pending:
        return jsonify({"status": "pending", "tag_uid": tag_uid})

    period = get_active_period()
    if not period:
        return jsonify({"error": "no active period"}), 409

    last_event = (
        Event.query.filter_by(student_id=student.id, period_id=period.id)
        .order_by(Event.timestamp.desc())
        .first()
    )
    next_type = "exit" if last_event and last_event.type == "enter" else "enter"

    event = Event(
        student_id=student.id,
        period_id=period.id,
        type=next_type,
        timestamp=utc_now(),
    )
    db.session.add(event)
    db.session.commit()

    return jsonify({"status": "logged", "student": student.name, "type": next_type})


@app.route("/api/admin/name_tag", methods=["POST"])
def name_tag():
    data = request.get_json(force=True)
    student_id = data.get("id")
    name = str(data.get("name", "")).strip()
    if not student_id or not name:
        return jsonify({"error": "id and name required"}), 400

    student = Student.query.get(student_id)
    if not student:
        return jsonify({"error": "no student with that id"}), 404

    student.name = name
    student.pending = False
    db.session.commit()

    return jsonify({"id": student.id, "name": student.name, "tag_uid": student.tag_uid})


@app.route("/admin")
def admin():
    return render_template("admin.html")


@app.route("/api/admin/overview")
def admin_overview():
    period = get_active_period()
    pending = Student.query.filter_by(pending=True).order_by(Student.id).all()
    roster = Student.query.filter_by(pending=False).order_by(Student.name).all()
    return jsonify(
        {
            "period": (
                {
                    "name": period.name,
                    "start_time": period.start_time.isoformat(),
                    "end_time": period.end_time.isoformat(),
                }
                if period
                else None
            ),
            "pending": [{"id": s.id, "tag_uid": s.tag_uid} for s in pending],
            "roster": [{"id": s.id, "name": s.name, "tag_uid": s.tag_uid} for s in roster],
        }
    )


@app.route("/api/admin/period/start", methods=["POST"])
def start_period():
    data = request.get_json(force=True) if request.data else {}
    name = data.get("name", "Demo Period")
    duration_seconds = int(data.get("duration_seconds", 120))
    grace_seconds = int(data.get("grace_seconds", 15))

    Period.query.filter_by(active=True).update({"active": False})

    period = Period(
        name=name,
        start_time=utc_now(),
        duration_seconds=duration_seconds,
        grace_seconds=grace_seconds,
        active=True,
    )
    db.session.add(period)
    db.session.commit()

    return jsonify({"id": period.id, "name": period.name})


@app.route("/api/status")
def status():
    period = get_active_period()
    if not period:
        return jsonify({"period": None, "students": []})

    now = utc_now()
    students = Student.query.filter_by(pending=False).order_by(Student.name).all()
    roster = [
        {"id": s.id, "name": s.name, "state": compute_state(s, period, now)}
        for s in students
    ]
    return jsonify(
        {
            "period": {
                "name": period.name,
                "start_time": period.start_time.isoformat(),
                "end_time": period.end_time.isoformat(),
                "seconds_remaining": max(0, int((period.end_time - now).total_seconds())),
            },
            "students": roster,
        }
    )


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, host="0.0.0.0", port=5050)
