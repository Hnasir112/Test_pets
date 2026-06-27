"""
Run once after first deployment to create all database tables.
Safe to re-run — uses CREATE TABLE IF NOT EXISTS semantics.

Usage:
    docker compose exec api python scripts/init_db.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.base import engine, Base
import app.models  # noqa: F401 — registers all models with Base.metadata

def main():
    print("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    print("Done.")

    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Tables present: {', '.join(sorted(tables))}")

if __name__ == "__main__":
    main()
