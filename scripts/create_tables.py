import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.base import Base, engine
from app.models import *  # noqa: registers all models

Base.metadata.create_all(bind=engine)
print("All tables created successfully.")
