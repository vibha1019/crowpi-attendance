"""Runs Vibha's presence backend (a flask_csh checkout) on 127.0.0.1 for the
laptop demo: a throwaway SQLite file, a demo teacher, and the demo
students. demo.py starts it with flask_csh's own Python; it is not meant
to be run by hand.

usage: python flask_server.py <flask_dir> <db_path> <port>
env:   RFID_API_KEY, DEMO_TEACHER_UID, DEMO_TEACHER_PASSWORD,
       DEMO_PEOPLE (JSON list of [uid, name])
"""

import json
import os
import secrets
import sys

flask_dir, db_path, port = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]), int(sys.argv[3])

# Never let a .env or a MySQL setting point this run at a real database.
os.environ.update(DB_ENDPOINT="", DB_USERNAME="", DB_PASSWORD="", IS_PRODUCTION="false", CAMERA_ENABLED="true")
for name in ("RFID_API_KEY", "DEMO_TEACHER_UID", "DEMO_TEACHER_PASSWORD", "DEMO_PEOPLE"):
    if not os.environ.get(name):
        sys.exit(f"{name} must be set (demo.py sets it)")

import flask_sqlalchemy  # noqa: E402

_original_init_app = flask_sqlalchemy.SQLAlchemy.init_app


def init_app_on_demo_db(self, app):
    """flask_csh picks its database while importing; this swaps in the demo file."""
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_BACKUP_URI"] = None
    _original_init_app(self, app)


flask_sqlalchemy.SQLAlchemy.init_app = init_app_on_demo_db

os.chdir(flask_dir)
sys.path.insert(0, flask_dir)
import main  # noqa: E402,F401  registers every blueprint, like the real server
from __init__ import app, db  # noqa: E402
from model.user import User  # noqa: E402

with app.app_context():
    url = db.engine.url
    if url.drivername != "sqlite" or os.path.realpath(url.database) != os.path.realpath(db_path):
        sys.exit(f"Refusing to run: the database is {url}, not the demo file.")
    db.create_all()
    people = [(name, uid, "User", secrets.token_urlsafe(16)) for uid, name in json.loads(os.environ["DEMO_PEOPLE"])]
    people.append(("Demo Teacher", os.environ["DEMO_TEACHER_UID"], "Teacher", os.environ["DEMO_TEACHER_PASSWORD"]))
    for name, uid, role, password in people:
        user = User(name=name, uid=uid, password=password, role=role)
        user.active = True
        db.session.add(user)
    db.session.commit()
    print(f"Demo backend: users {[person[1] for person in people]}", flush=True)

app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
