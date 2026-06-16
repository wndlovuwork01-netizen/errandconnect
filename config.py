import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = "postgresql://neondb_owner:npg_7kQrT3mbRoJd@ep-divine-cake-agsqwqph-pooler.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300
    }

    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
