"""Seed a handful of demo students and start a short demo period so the
dashboard/pipeline can be tested end-to-end before real tags are registered."""

from app import app
from models import Period, Student, db, utc_now

DEMO_STUDENTS = [
    ("demo-001", "Alice Chen"),
    ("demo-002", "Ben Ortiz"),
    ("demo-003", "Priya Nair"),
    ("demo-004", "Marcus Lee"),
]

with app.app_context():
    db.create_all()

    for tag_uid, name in DEMO_STUDENTS:
        if not Student.query.filter_by(tag_uid=tag_uid).first():
            db.session.add(Student(tag_uid=tag_uid, name=name))

    Period.query.filter_by(active=True).update({"active": False})
    db.session.add(
        Period(
            name="Demo Period",
            start_time=utc_now(),
            duration_seconds=120,
            grace_seconds=15,
            active=True,
        )
    )
    db.session.commit()
    print("Seeded demo students and started a 2-minute demo period.")
