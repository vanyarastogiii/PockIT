"""
Django settings for the PockIt project.

Configuration comes from environment variables (see ../.env.example).
If POSTGRES_DB is not set, it falls back to SQLite so you can start quickly,
but row locking (select_for_update) only really works on PostgreSQL.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    """Tiny .env reader so you don't need an extra package.
    Real environment variables always win over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv(BASE_DIR.parent / ".env")  # the .env file in the project root


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me-in-production-please")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
    "drf_spectacular_sidecar",  # Swagger UI files bundled locally, no CDN needed
    # local
    "accounts",
    "wallets",
    "merchants",
    "demoshop",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Serves the HTML/CSS/JS front end and admin static files from Django itself,
    # so one process (one terminal, one deployment) runs the whole app.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------- database
# 1. DATABASE_URL (set automatically by hosts like Render/Railway)
# 2. POSTGRES_* variables (local Docker Postgres)
# 3. otherwise SQLite, so you can start with zero setup
if os.environ.get("DATABASE_URL"):
    import dj_database_url

    DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"], conn_max_age=60)}
elif os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "wallet"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "wallet"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)  # avoids a WhiteNoise warning before collectstatic
WHITENOISE_USE_FINDERS = DEBUG  # in development, serve admin CSS without collectstatic
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# The front end folder is served at the site root: / -> frontend/index.html
FRONTEND_DIR = BASE_DIR.parent / "frontend"
if FRONTEND_DIR.exists():
    WHITENOISE_ROOT = FRONTEND_DIR
    WHITENOISE_INDEX_FILE = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- API
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    # Rate limiting: views opt in with throttle_scope = "transfers"
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "transfers": os.environ.get("TRANSFER_RATE_LIMIT", "10/min"),
        "requests": os.environ.get("REQUEST_RATE_LIMIT", "20/min"),
        "lookups": os.environ.get("LOOKUP_RATE_LIMIT", "60/min"),
        "checkout": os.environ.get("CHECKOUT_RATE_LIMIT", "30/min"),
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# Interactive API docs at /api/docs
SPECTACULAR_SETTINGS = {
    "TITLE": "PockIt API",
    "DESCRIPTION": (
        "A small payments API: wallets, transfers and payment requests. "
        "Money moves inside database transactions with row locks, every payment "
        "takes an Idempotency-Key, and every movement is written to a double-entry ledger.\n\n"
        "To try it: call /api/auth/login, copy the `access` token, click **Authorize** and paste it."
    ),
    "VERSION": "1.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "SWAGGER_UI_SETTINGS": {"persistAuthorization": True, "tryItOutEnabled": True},
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
}

# The front end runs on a different port in development (VS Code Live Server = 5500)
CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,http://localhost:8080",
).split(",")
# Browsers block custom headers across origins unless we allow them explicitly.
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*default_headers, "idempotency-key")
CORS_EXPOSE_HEADERS = ["Idempotent-Replayed"]  # let JS read this response header

# ---------------------------------------------------------------- production
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # behind Render's HTTPS proxy
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# Render sets this to your app's public hostname, e.g. pockit.onrender.com
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])

# Needed for logging in to /admin over HTTPS ("*.onrender.com" style entries become wildcards).
CSRF_TRUSTED_ORIGINS = [
    f"https://*{host}" if host.startswith(".") else f"https://{host}"
    for host in ALLOWED_HOSTS
    if host not in ("localhost", "127.0.0.1", "backend")
]

# Wallet rules
MAX_TOPUP_AMOUNT = 100000  # ₹1,00,000 per top-up
MAX_ORDER_AMOUNT = 100000  # largest single checkout

# ---------------------------------------------------------------- risk rules
# A payment that matches any rule is put ON HOLD for review instead of going
# through. Amounts are in rupees. See wallets/risk.py.
RISK_RULES = {
    "ENABLED": env_bool("RISK_ENABLED", True),
    "NEW_ACCOUNT_HOURS": 24,         # accounts younger than this...
    "NEW_ACCOUNT_MAX_AMOUNT": 5000,  # ...can send up to this without review
    "VELOCITY_WINDOW_MINUTES": 10,   # more than this many payments...
    "VELOCITY_MAX_PAYMENTS": 5,      # ...in the window gets reviewed
    "NEW_RECIPIENT_MAX_AMOUNT": 10000,  # first payment to someone new
    "LARGE_PAYMENT_AMOUNT": 50000,   # anything this big is reviewed
}

# ---------------------------------------------------------------- merchants
# Public address of this site, used to build the demo shop's webhook URL.
SITE_URL = os.environ.get("SITE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "http://127.0.0.1:8000"
WEBHOOK_MAX_ATTEMPTS = 6        # then the event is marked FAILED
WEBHOOK_TIMEOUT_SECONDS = 5
WEBHOOK_DELIVER_IN_BACKGROUND = env_bool("WEBHOOK_DELIVER_IN_BACKGROUND", True)
if "test" in sys.argv:
    WEBHOOK_DELIVER_IN_BACKGROUND = False  # tests deliver webhooks explicitly
