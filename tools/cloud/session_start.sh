#!/bin/bash
# クラウドのセッションの始めに、Docker を使わない開発の環境（RUNNER=host）を整える
# （.claude/settings.json の SessionStart フックから呼ぶ。docs/cloud-session.md の 4）。
# - CLAUDE_CODE_REMOTE=true のときだけ動く（手元では何もしない）。何度動いても同じ結果になる。
# - OS の道具は環境の設定画面の setup script（tools/cloud/setup.sh）が入れる。ここでは毎回、
#   それがそろっているか（soffice・字体の規則・Chromium・uv の版・Python 3.12）を確かめ、
#   足りないもの・失敗を、知らせの先頭に出す。
# - 失敗しても止めずに続け、exit 0 で終わる（知らせで分かるようにする）。
set -u
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
root=${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$root" || exit 0
started=$SECONDS
export UV_SYSTEM_CERTS=1
unset UV_NATIVE_TLS
BROWSERS=/opt/pw-browsers
ENV_BACKUP=/root/.config/ssw-support-system/env  # .env の写し（git の外。鍵を失わないため）

problems=()  # 足りないもの・失敗（知らせの先頭に出す）
notes=()     # 知らせておくこと（.env を戻した・DB を作り直した など）
found=()     # そろっているものの版
problem() { problems+=("$*"); }
note() { notes+=("$*"); }

# コマンドを流し、失敗したら出力の終わりの 3 行を添えて problem にする
run() {
  local what=$1 out
  shift
  if ! out=$("$@" 2>&1); then
    problem "$what が失敗した: $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
    return 1
  fi
}

# 0. setup script の結果（環境の cache を作ったときに一度だけ流れ、ログが残る）
if [ -f /var/log/ssw-setup.log ]; then
  while read -r line; do problem "setup script の段 $line（/var/log/ssw-setup.log.full）"; done \
    < <(grep FAILED /var/log/ssw-setup.log)
else
  problem "setup script のログ /var/log/ssw-setup.log が無い（環境の設定画面に tools/cloud/setup.sh を貼っていない）"
fi

# 1. PostgreSQL・Redis（cache には動いている状態が残らないので、毎回起こす）
service postgresql start > /dev/null 2>&1
service redis-server start > /dev/null 2>&1  # ulimit の警告は出るが、動く
pg_isready -q -h localhost || problem "PostgreSQL が動いていない（service postgresql start）"
[ "$(redis-cli ping 2> /dev/null)" = "PONG" ] || problem "Redis が動いていない（service redis-server start）"

# 2. 帳票の字体の置き換えの規則（リポジトリの版を写す）
rules=backend/config/fonts/99-form-fonts.conf
if ! cmp -s "$rules" /etc/fonts/conf.d/99-form-fonts.conf; then
  cp "$rules" /etc/fonts/conf.d/99-form-fonts.conf && fc-cache -f > /dev/null 2>&1
fi
cmp -s "$rules" /etc/fonts/conf.d/99-form-fonts.conf \
  || problem "字体の規則の写し /etc/fonts/conf.d/99-form-fonts.conf がリポジトリと違う"

# 3. .env（既存の値は変えない。消えたときは写しから戻す。接続先だけを VM の構成に合わせる）
new_env=0
if [ ! -f .env ] && [ -f "$ENV_BACKUP" ]; then
  cp "$ENV_BACKUP" .env && note ".env が無かったので、写し $ENV_BACKUP から戻した（鍵は前と同じ）"
fi
[ -f .env ] || new_env=1
run ".env の作成（scripts/init_env.py）" python3 scripts/init_env.py
sed -i \
  -e 's#@db:5432#@localhost:5432#' \
  -e 's#redis://redis:#redis://localhost:#' \
  -e 's#^EMAIL_HOST=mailpit$#EMAIL_HOST=localhost#' \
  -e "s#\"location\": \"/app/media\"#\"location\": \"$root/media\"#" \
  -e "s#^DJANGO_STORAGE_OPTIONS=\({.*}\)\$#DJANGO_STORAGE_OPTIONS='\1'#" \
  .env
(umask 077 && mkdir -p "$(dirname "$ENV_BACKUP")" && cp .env "$ENV_BACKUP")
chmod 700 "$(dirname "$ENV_BACKUP")"
chmod 600 "$ENV_BACKUP"
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# 4. DB の利用者と DB（テストは CREATEDB が要る）。.env を新しく作ったのに DB が残っていたら、
#    古い鍵で暗号化した列が読めないので、開発用の DB を作り直す（ダミーだけ。制約 10）
psql_su() { su postgres -c "psql -qtAX -v ON_ERROR_STOP=1 -c \"$1\""; }
if [ -z "$(psql_su "SELECT 1 FROM pg_roles WHERE rolname='$POSTGRES_USER'")" ]; then
  run "DB の利用者の作成" psql_su "CREATE USER $POSTGRES_USER WITH PASSWORD '$POSTGRES_PASSWORD' CREATEDB"
fi
db_exists=$(psql_su "SELECT 1 FROM pg_database WHERE datname='$POSTGRES_DB'")
if [ "$new_env" = 1 ] && [ -n "$db_exists" ]; then
  if run "開発用の DB の削除" psql_su "DROP DATABASE $POSTGRES_DB WITH (FORCE)"; then
    note "開発用の DB $POSTGRES_DB を作り直した（.env も写しも無く、新しい鍵になったため。古い鍵で暗号化したダミーの行は読めない。ダミーは make seed で作り直す）"
    db_exists=""
  fi
fi
[ -n "$db_exists" ] || run "開発用の DB の作成" psql_su "CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER"

# 5. 道具の版（setup script が入れたもの）
expected_uv=$(sed -n 's#.*ghcr.io/astral-sh/uv:\([0-9.]*\).*#\1#p' backend/Dockerfile | head -n 1)
actual_uv=$(uv --version 2> /dev/null | awk '{print $2}')
if [ "$actual_uv" = "$expected_uv" ]; then found+=("uv $actual_uv"); else
  problem "uv の版が ${actual_uv:-無い}（Dockerfile は $expected_uv。setup script を貼り直す）"
fi
if soffice_version=$(soffice --version 2> /dev/null) && [ -n "$soffice_version" ]; then
  found+=("$(printf '%s' "$soffice_version" | awk '{print $1, $2}')")
else
  problem "soffice（LibreOffice）が無い（帳票の PDF の変換ができない）"
fi

# 6. Python の依存・マイグレーション・様式マスタ・UI 文言（どれも何度流しても同じ結果）
cd backend || exit 0
expected_python=$(tr -d '[:space:]' < .python-version)
if run "uv sync" uv sync --frozen --group e2e -q; then
  actual_python=$(.venv/bin/python -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
  case "$actual_python" in
    "$expected_python".*) found+=("Python $actual_python") ;;
    *) problem "venv の Python が $actual_python（.python-version は $expected_python）" ;;
  esac
  run "migrate" uv run --frozen python manage.py migrate --no-input -v 0
  run "load_form_templates" uv run --frozen python manage.py load_form_templates
  run "compilemessages" uv run --frozen python manage.py compilemessages -l ja -v 0
  # Chromium: いまの playwright が使う版が、BROWSERS にそろっているか
  missing=$(PLAYWRIGHT_BROWSERS_PATH=$BROWSERS uv run --frozen --group e2e \
    playwright install --dry-run chromium 2> /dev/null \
    | sed -n 's#^ *Install location: *##p' | while read -r dir; do [ -d "$dir" ] || echo "$dir"; done)
  if [ -n "$missing" ]; then
    problem "Chromium が無い: $(printf '%s' "$missing" | tr '\n' ' ')（setup script を貼り直す）"
  else
    found+=("Chromium（$BROWSERS）")
  fi
fi
cd "$root" || exit 0

# 7. 以後の Bash で使う値（Claude Code が CLAUDE_ENV_FILE を読む）
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export RUNNER=host"
    echo "export PLAYWRIGHT_BROWSERS_PATH=$BROWSERS"
    echo "export UV_SYSTEM_CERTS=1"
    echo "unset UV_NATIVE_TLS"
    echo "set -a; . \"$root/.env\"; set +a"
  } >> "$CLAUDE_ENV_FILE"
fi

# 8. 知らせ（足りないもの・失敗を先頭に。人には systemMessage、Claude には additionalContext で）
{
  if [ ${#problems[@]} -gt 0 ]; then
    echo "【クラウドの環境: 足りないもの・失敗が ${#problems[@]} 件】"
    printf -- '- %s\n' "${problems[@]}"
  else
    echo "クラウドの環境: そろっている（RUNNER=host）"
  fi
  [ ${#notes[@]} -gt 0 ] && printf -- '- %s\n' "${notes[@]}"
  versions=$(printf '%s・' "${found[@]}")
  echo "- 版: ${versions%・}。かかった時間 $((SECONDS - started)) 秒"
} | python3 -c '
import json, sys
text = sys.stdin.read().strip()
print(json.dumps({"systemMessage": text, "hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": text}}, ensure_ascii=False))
'
exit 0
