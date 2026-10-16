"""Django 設定。

環境ごとに変わる値はすべて環境変数から読む
（CLAUDE.md 制約4 / 仕様書第9章「場所に依存しないコードの5原則」）。
秘密情報や接続先には既定値を置かず、未設定なら起動時に ImproperlyConfigured で止める。
"""

from pathlib import Path

import django_stubs_ext
import environ

# ModelAdmin[...] などの型引数を実行時に許可する（django-stubs 推奨）。
django_stubs_ext.monkeypatch()

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
REPO_ROOT = BASE_DIR.parent

env = environ.Env()

# --- 基本 ---
SECRET_KEY: str = env.str("DJANGO_SECRET_KEY")
DEBUG: bool = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS: list[str] = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "apps.core.admin_apps.SupportAdminConfig",  # django.contrib.admin（ロール判定の管理サイト）
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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

# --- データベース ---
DATABASES = {"default": env.db("DATABASE_URL")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Redis（ジョブ実行基盤。Django-Q2 は組み込むタスクで設定する） ---
REDIS_URL: str = env.str("REDIS_URL")

# --- メール（開発は Mailpit、本番は自社 SMTP） ---
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST: str = env.str("EMAIL_HOST")
EMAIL_PORT: int = env.int("EMAIL_PORT")
EMAIL_HOST_USER: str = env.str("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD: str = env.str("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS: bool = env.bool("EMAIL_USE_TLS", default=False)
DEFAULT_FROM_EMAIL: str = env.str("DEFAULT_FROM_EMAIL")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# --- 認証 ---
AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = ["apps.core.backends.RoleBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- 国際化（日付は Asia/Tokyo の timezone-aware で扱う） ---
LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Tokyo"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

# --- 静的ファイル ---
STATIC_URL = "static/"
STATIC_ROOT = REPO_ROOT / "staticfiles"

# --- ファイル保存（Attachment・ロゴ） ---
# バックエンドとオプションを環境変数で渡し、コードを変えずに保存先を切り替える。
#   手元: django.core.files.storage.FileSystemStorage + {"location": "/app/media"}
#   本番: storages.backends.s3.S3Storage（django-storages）
#         + {"bucket_name": ..., "endpoint_url": ...}
# アップロードファイルは公開 URL で配信しない（認可・監査付きのダウンロードは T05）。
STORAGES = {
    "default": {
        "BACKEND": env.str("DJANGO_STORAGE_BACKEND"),
        "OPTIONS": env.json("DJANGO_STORAGE_OPTIONS", default={}),
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
