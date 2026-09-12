import os

try:  # optional python-dotenv: real env vars always win (override=False)
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass


def _database_uri():
    uri = os.environ.get("DATABASE_URL")
    if uri:
        # Render/Heroku style postgres:// -> postgresql://
        if uri.startswith("postgres://"):
            uri = uri.replace("postgres://", "postgresql://", 1)
        return uri
    # Default: instance/billing.db next to the app (Flask instance folder)
    return "sqlite:///billing.db"


def _engine_options(uri: str):
    # SQLite (esp. QueuePool-less NullPool) does NOT accept pool_size/max_overflow.
    if uri.startswith("sqlite"):
        return {"pool_pre_ping": True}
    return {
        "pool_pre_ping": True,
        "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", "3600")),
        "pool_size": int(os.environ.get("DB_POOL_SIZE", "10")),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "20")),
    }


_DATABASE_URI = _database_uri()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")
    SQLALCHEMY_DATABASE_URI = _DATABASE_URI
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = _engine_options(_DATABASE_URI)

    # Security cookies (relaxed Secure flag locally; enable in prod via env)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    PERMANENT_SESSION_LIFETIME = 8 * 3600  # 8h

    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None  # form tokens don't expire mid-billing
