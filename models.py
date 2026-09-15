from datetime import datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utc_now():
    """Naive UTC now (kept naive so it compares cleanly with stored timestamps)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    tag_uid = db.Column(db.String(64), unique=True, nullable=False)
    pending = db.Column(db.Boolean, nullable=False, default=False)


class Period(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    duration_seconds = db.Column(db.Integer, nullable=False)
    grace_seconds = db.Column(db.Integer, nullable=False, default=15)
    active = db.Column(db.Boolean, default=False)

    @property
    def end_time(self):
        return self.start_time + timedelta(seconds=self.duration_seconds)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    period_id = db.Column(db.Integer, db.ForeignKey("period.id"), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'enter' or 'exit'
    timestamp = db.Column(db.DateTime, nullable=False, default=utc_now)

    student = db.relationship("Student")
    period = db.relationship("Period")
