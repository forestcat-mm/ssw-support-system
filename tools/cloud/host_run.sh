#!/bin/bash
# RUNNER=host（クラウドの VM。Docker なし）で、web のコンテナの中と同じように backend/ でコマンドを
# 流す（Makefile から使う。docs/cloud-session.md）。.env を読み、uv の venv（uv.lock のとおり。
# e2e のグループも入れる）で動かす。PostgreSQL・Redis・.env は SessionStart フックが整える。
#
# 使い方: tools/cloud/host_run.sh <コマンド> [引数...]   例: tools/cloud/host_run.sh pytest tests/core
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
if [ ! -f "$root/.env" ]; then
  echo "host_run: .env が無い（SessionStart フック tools/cloud/session_start.sh が作る）" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. "$root/.env"
set +a
export UV_SYSTEM_CERTS=1
unset UV_NATIVE_TLS
cd "$root/backend"
exec uv run --frozen --group e2e "$@"
