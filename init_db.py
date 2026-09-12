"""Create the schema and the first admin user.

By default this refuses to touch a database that already has data — pass
--force to rebuild from scratch (which DESTROYS every bill, client and
product in it).

    python init_db.py            # safe: create if empty, else stop
    python init_db.py --force    # wipe and recreate
"""
import sys

from sqlalchemy import inspect

from app import app, db, User, Settings

FORCE = "--force" in sys.argv


def _has_data() -> bool:
    insp = inspect(db.engine)
    return insp.has_table("invoice") and db.session.query(
        db.func.count()).select_from(db.Table(
            "invoice", db.MetaData(), autoload_with=db.engine)).scalar() > 0


with app.app_context():
    if FORCE:
        db.drop_all()
    elif _has_data():
        sys.exit("Database already has invoices — refusing to wipe it. "
                 "Pass --force if you really want to rebuild (DESTROYS DATA).")

    db.create_all()

    if not User.query.first():
        admin = User(username="admin", email="admin@example.com", is_admin=True)
        admin.set_password("admin123456")  # min 8 chars; change after first login
        db.session.add(admin)
        print("Created admin / admin123456 — change it now.")
    if not Settings.query.first():
        db.session.add(Settings())
    db.session.commit()
    print("OK: schema ready.")
