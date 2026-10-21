# クラウドセッション（Claude Code on the web）の環境の設定

2026-10-21 に、手元の Mac のセッションからクラウドのセッション（claude.ai/code）へ移すために書いた。**この文書のセットアップの案は、まだ動かして確かめていない**（次のクラウドのセッションで確かめ、直す）。進め方の決まりは CLAUDE.md の「クラウドセッションでの進め方」。

## 1. 環境変数

値そのものはここに書かない。開発用はすべてダミーでよい（実データ・本物の鍵は使わない。CLAUDE.md の制約 4・10）。手元では `make env`（`scripts/init_env.py`）が `.env.example` から `.env` を作り、`REPLACE_ME` の鍵を生成する。クラウドでも同じ道具で作り、接続先だけを VM の構成に合わせて書き換える（下の 3 の案）。

| 名前 | 用途 | ダミーの値の作り方 |
|---|---|---|
| `DJANGO_SECRET_KEY` | Django の秘密鍵 | `init_env.py` が生成（`secrets.token_urlsafe(50)`） |
| `DJANGO_DEBUG` | 開発の表示 | `true` |
| `DJANGO_ALLOWED_HOSTS` | 許可するホスト名 | `localhost,127.0.0.1` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | CSRF の許可元 | `http://localhost:8000,http://127.0.0.1:8000` |
| `POSTGRES_DB`・`POSTGRES_USER`・`POSTGRES_PASSWORD` | DB の初期化（Docker の db コンテナ） | `.env.example` の開発用の値のまま |
| `DATABASE_URL` | DB の接続先 | Docker なら `.env.example` のまま（ホスト `db`）。Docker なしなら、ホストを `localhost` にする |
| `REDIS_URL` | Django-Q のジョブ | Docker なら `redis://redis:6379/0`、Docker なしなら `redis://localhost:6379/0` |
| `EMAIL_HOST`・`EMAIL_PORT`・`EMAIL_HOST_USER`・`EMAIL_HOST_PASSWORD`・`EMAIL_USE_TLS`・`DEFAULT_FROM_EMAIL` | メール（開発は Mailpit） | `.env.example` のまま。Docker なしで Mailpit が無いときは `EMAIL_HOST=localhost`（送信は失敗するが、単体テストはメールを外へ送らない） |
| `DJANGO_STORAGE_BACKEND`・`DJANGO_STORAGE_OPTIONS` | 添付の保存先 | ローカルのファイル（`FileSystemStorage`）。Docker なしなら `location` を `/app/media` から VM の書ける場所（例: リポジトリの `media/`）に変える |
| `AUTH_LOCKOUT_FAILURE_LIMIT`・`AUTH_LOCKOUT_MINUTES`・`SESSION_IDLE_TIMEOUT_MINUTES`・`SESSION_ABSOLUTE_TIMEOUT_HOURS` | ログインの失敗・セッション（第8章） | `.env.example` のまま（未設定でも既定値で動く） |
| `SUBMISSION_PDF_MAX_MB`・`SUBMISSION_MAX_FILES` | 提出用の統合 PDF の上限（T42） | `.env.example` のまま |
| `TRUST_CF_CONNECTING_IP` | 接続元 IP のヘッダー | `false` |
| `FIELD_ENCRYPTION_KEYS` | 列の暗号化の鍵（Fernet。カンマ区切り） | `init_env.py` が生成（32 バイトの urlsafe base64） |
| `BLIND_INDEX_KEY` | 暗号化した列の検索用の鍵 | `init_env.py` が生成（同上） |
| `ALLOW_DUMMY_DATA` | `seed_dummy` を許す | `true`（開発だけ） |
| `JOBS_INLINE` | ジョブをその場で実行 | 設定しない（テストの設定 `config/settings_test.py` が立てる） |
| `TEST_DB_SUFFIX` | e2e のテスト DB の名前を分ける | e2e を流すときだけ `e2e` |
| `DJANGO_ALLOW_ASYNC_UNSAFE` | Playwright と Django の同居（e2e） | e2e を流すときだけ `true`（`Dockerfile.e2e` と同じ） |

## 2. ネットワークで許可が要る接続先

アプリそのものは外部 API に接続しない（制約 1。例外は自社 SMTP）。次は開発の作業（依存の取得・一次資料の取得・push）で使う。

| 用途 | 接続先 |
|---|---|
| GitHub（clone・push・PR） | `github.com`（`git@github.com:forestcat-mm/ssw-support-system.git`） |
| Python のパッケージ（uv） | `pypi.org`・`files.pythonhosted.org` |
| uv そのもの | `astral.sh`・`github.com`（リリースの取得）、Docker のときは `ghcr.io`（`ghcr.io/astral-sh/uv:0.13.1`） |
| OS のパッケージ | Debian（`deb.debian.org`・`security.debian.org`。web のイメージは bookworm）、Ubuntu（`archive.ubuntu.com`・`security.ubuntu.com`。e2e のイメージは noble、VM が Ubuntu のときも） |
| Docker のイメージ | Docker Hub（`registry-1.docker.io`・`auth.docker.io`・`production.cloudflare.docker.com`。`python:3.12-slim-bookworm`・`postgres:16`・`redis:7`・`axllent/mailpit:v1.31`）、`mcr.microsoft.com`（`playwright/python:v1.63.0-noble`）、`ghcr.io` |
| Playwright のブラウザ（Docker を使わないとき） | `playwright.download.prss.microsoft.com`・`cdn.playwright.dev`（`playwright install chromium`） |
| 入管（一次資料・様式・ツール） | `www.moj.go.jp` |
| e-Gov 法令（一次資料） | `laws.e-gov.go.jp` |
| NIST（認証の根拠。ADR-0030） | `nvlpubs.nist.gov` |
| 字体の配布元（ADR-0046。作り直すときだけ） | `raw.githubusercontent.com`（`google/fonts`） |

一次資料の一覧と SHA-256 は `docs/sources.md`。取得は User-Agent を付け、間をあけて 1 件ずつ行う。

## 3. セットアップのスクリプトの案（未確認）

今の `Makefile` は `docker compose exec web …` で動かす。クラウドの VM で Docker が使えるかで、2 つの案を書く。

### 案 A: Docker が使える VM

```sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
python3 scripts/init_env.py          # .env を作り、鍵をダミーで生成（make env と同じ）
docker compose build web
docker compose --profile e2e build e2e   # e2e を流すときだけ（大きい）
docker compose up -d                  # web・qcluster・db・redis・mailpit
docker compose exec -T web python manage.py migrate
docker compose exec -T web python manage.py load_form_templates   # make forms
docker compose exec -T web python manage.py compilemessages -l ja
# 以後は手元と同じ: make check-quick T=… / make test / make checks
```

### 案 B: Docker が使えない VM（Ubuntu 24.04 を想定）

```sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# OS のパッケージ: PostgreSQL 16・Redis・gettext・LibreOffice（PDF の変換）・帳票の字体
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  postgresql-16 redis-server gettext \
  libreoffice-writer-nogui libreoffice-calc-nogui \
  fontconfig fonts-ipafont-mincho fonts-ipafont-gothic fonts-noto-cjk fonts-crosextra-carlito
sudo cp backend/config/fonts/99-form-fonts.conf /etc/fonts/conf.d/99-form-fonts.conf
sudo fc-cache -f
sudo service postgresql start
sudo service redis-server start

# DB: 開発用の利用者（テストは CREATEDB が要る）
sudo -u postgres psql -c "CREATE USER ssw WITH PASSWORD 'dev-only-change-me' CREATEDB;" || true
sudo -u postgres psql -c "CREATE DATABASE ssw OWNER ssw;" || true

# .env（鍵はダミーで生成し、接続先を localhost に）
python3 scripts/init_env.py
sed -i \
  -e 's#@db:5432#@localhost:5432#' \
  -e 's#redis://redis:#redis://localhost:#' \
  -e 's#^EMAIL_HOST=.*#EMAIL_HOST=localhost#' \
  -e "s#\"location\": \"/app/media\"#\"location\": \"$PWD/media\"#" \
  .env
set -a; . ./.env; set +a

# Python（uv）
curl -LsSf https://astral.sh/uv/0.13.1/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
cd backend
uv sync --frozen --group e2e
uv run python manage.py migrate
uv run python manage.py load_form_templates
uv run python manage.py compilemessages -l ja
uv run playwright install --with-deps chromium   # e2e を流すときだけ
```

案 B での検査（`make` は Docker を前提にしているので、同じことを直接流す。`backend/` で、`.env` を読み込んだ後）:

| 手元の make | 案 B で流すもの |
|---|---|
| `make lint` | `uv run ruff check . && uv run ruff format --check . && uv run mypy .` |
| `make check-quick T=…` | 上の lint と `uv run pytest -n auto --reuse-db -m "not slow" …` |
| `make test` | `uv run pytest -n auto --reuse-db` |
| `make messages` | `uv run python manage.py makemessages -l ja --no-location --no-obsolete && uv run python manage.py compilemessages -l ja` |
| `make e2e` | `TEST_DB_SUFFIX=e2e DJANGO_ALLOW_ASYNC_UNSAFE=true uv run pytest e2e -n 4`（ブラウザと LibreOffice が要る） |
| `make checks` | `tools/run_checks.sh` は Docker を前提にしている。案 B で流せないものは PR に「手元で流すこと」と書く（CLAUDE.md） |

### 確かめること（次のクラウドのセッションで）

- VM の OS・Docker の有無・`sudo` の可否。PostgreSQL 16 が入るか（Debian bookworm の既定は 15）。
- LibreOffice の PDF の変換が動き、帳票の頁数・字体が手元と同じか（`slow` のテスト。字体の置き換えは `backend/config/fonts/`）。
- `make checks` の各段（e2e・gate2・gate3・gate4）を流せるか。流せない段は PR に「手元で流すこと」と書く。
- 上の接続先のうち、許可が要るもの。
