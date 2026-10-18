"""Django 設定。

環境ごとに変わる値はすべて環境変数から読む
（CLAUDE.md 制約4 / 仕様書第9章「場所に依存しないコードの5原則」）。
秘密情報や接続先には既定値を置かず、未設定なら起動時に ImproperlyConfigured で止める。
"""

from datetime import timedelta
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
    "django.contrib.postgres",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "axes",
    "django_q",
    "apps.core",
    "apps.workers",
    "apps.hosts",
    "apps.contracts",
    "apps.filings",
    "apps.support",
    "apps.seed",
    "apps.forms_engine",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "apps.core.middleware.MFARequiredMiddleware",
    "apps.core.middleware.AuditContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # 管理画面のテンプレートの上書き（admin/base_site.html。アプリより先に探す）
        "DIRS": [BASE_DIR / "config" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.ui",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- データベース ---
DATABASES = {"default": env.db("DATABASE_URL")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- ダミーデータ（seed_dummy）。DEBUG かつ true のときだけ実行できる（本番には入れない）---
ALLOW_DUMMY_DATA: bool = env.bool("ALLOW_DUMMY_DATA", default=False)

# --- 列暗号化（第8章）。鍵は環境変数で渡し、既定値は持たない（make env で生成）---
# 先頭の鍵で暗号化し、すべての鍵で復号を試す。ローテーション手順は services/crypto.py。
FIELD_ENCRYPTION_KEYS: list[str] = env.list("FIELD_ENCRYPTION_KEYS")
BLIND_INDEX_KEY: str = env.str("BLIND_INDEX_KEY")

# --- ジョブ実行（Django-Q2 + Redis。ADR-0005）。日次の起票などを qcluster が実行する ---
REDIS_URL: str = env.str("REDIS_URL")
Q_CLUSTER = {
    "name": "ssw",
    "workers": 2,
    "timeout": 600,
    "retry": 900,
    "max_attempts": 3,
    "catch_up": False,  # 止まっていた間の回数分は実行しない（日次コマンドは冪等で、次回で追いつく）
    "redis": REDIS_URL,
}

# ジョブをその場で実行する（単体テストと e2e だけ。services.jobs）
JOBS_INLINE: bool = env.bool("JOBS_INLINE", default=False)

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
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",  # 失敗回数によるロック（先頭に置く）
    "apps.core.backends.RoleBackend",
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "/"  # 業務画面のトップ。管理画面へはナビの「管理画面」から

# ログイン失敗によるロック（第8章）。
# 前段の Cloudflare 経由では IP が当てにならないため、ユーザー名単位でロックする。
# TOTP・回復用コードの入力ミスも同じ回数に数える（apps.core.views）。
AXES_FAILURE_LIMIT: int = env.int("AUTH_LOCKOUT_FAILURE_LIMIT", default=5)
AXES_COOLOFF_TIME = timedelta(minutes=env.int("AUTH_LOCKOUT_MINUTES", default=30))
AXES_LOCKOUT_PARAMETERS = ["username"]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "accounts/locked.html"
AXES_CLIENT_IP_CALLABLE = "services.client_ip.client_ip"  # 監査ログと同じ取り方
# axes.W006（IP をロック条件に含めない警告）は方針どおりのため抑止する。
# 本番で接続元 IP（CF-Connecting-IP）を取れるようにした時点で IP 条件の追加を判断する（本番準備）。
SILENCED_SYSTEM_CHECKS = ["axes.W006"]

# 接続元 IP（監査ログ・ロック）。Cloudflare Tunnel 経由でしか届かない本番でだけ true にする。
TRUST_CF_CONNECTING_IP: bool = env.bool("TRUST_CF_CONNECTING_IP", default=False)

# 無操作でのセッション失効（第8章）。リクエストのたびに期限を延ばす。
SESSION_COOKIE_AGE: int = env.int("SESSION_IDLE_TIMEOUT_MINUTES", default=30) * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
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

# --- 帳票（段階3。ADR-0018）---
# 様式の original・template・manifest と、マッピング定義の置き場（リポジトリで管理する。制約5）。
FORM_TEMPLATES_ROOT = REPO_ROOT / "templates" / "forms"
FORM_MAPPINGS_ROOT = REPO_ROOT / "mappings"
# LibreOffice headless の PDF 変換の時間の上限（秒）
FORM_CONVERT_TIMEOUT = 120
