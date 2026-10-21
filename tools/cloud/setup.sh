#!/bin/bash
# クラウドの環境の設定画面の setup script（docs/cloud-session.md の 3）。OS の道具だけを入れる。
# 設定画面の「Setup script」には、このファイルの全文をそのまま貼る（このファイルが正本。変えたら
# 貼り直す。貼り直すと環境の cache が作り直される）。版はテストで Dockerfile・pyproject と照らす
# （tests/test_cloud_tools.py）。リポジトリが無くても動くように、ほかのファイルを読まない。
# - root で動く（sudo は要らない）。失敗しても続け、必ず exit 0 で終わる（失敗するとセッションが
#   始まらない）。どの段が失敗したかは LOG（git の外）に書き、SessionStart フックが知らせる。
# - この VM で測った時間: apt 約 15 秒・uv 約 3 秒・Chromium 約 40 秒（合わせて約 1 分）。
UV_VERSION=0.13.1          # backend/Dockerfile・Dockerfile.e2e の ghcr.io/astral-sh/uv と同じ
PLAYWRIGHT_VERSION=1.63.0  # backend/pyproject.toml の e2e グループの playwright と同じ
PYTHON_VERSION=3.12        # backend/.python-version と同じ（web のイメージの python:3.12）
LOG=/var/log/ssw-setup.log
export DEBIAN_FRONTEND=noninteractive

: > "$LOG"; : > "$LOG.full"
step() {
  local name=$1 start=$SECONDS code
  shift
  "$@" >> "$LOG.full" 2>&1
  code=$?
  if [ "$code" -eq 0 ]; then
    echo "$name ok ($((SECONDS - start))s)" >> "$LOG"
  else
    echo "$name FAILED exit=$code ($((SECONDS - start))s)" >> "$LOG"
  fi
}

step apt-update apt-get update -q
# VM に既にある: postgresql-16・redis-server・LibreOffice 24.2・IPA ゴシック・Carlito・python3.12。
# 足す: gettext・GUI なしの LibreOffice（Docker のイメージと同じ名前）・IPA 明朝・Noto CJK
step apt-install apt-get install -y -q --no-install-recommends \
  "python$PYTHON_VERSION" postgresql-16 redis-server gettext \
  libreoffice-writer-nogui libreoffice-calc-nogui \
  fontconfig fonts-ipafont-mincho fonts-ipafont-gothic fonts-noto-cjk fonts-crosextra-carlito
# uv（astral.sh は許可されていないので PyPI から。VM の既定の uv を同じ場所で置き換える）
step uv uv tool install --force "uv==$UV_VERSION"
# Playwright の Chromium（VM の既定の chromium-1194 は playwright 1.63 に合わない。同じ場所に足す）
step chromium env PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  uvx --python "$PYTHON_VERSION" --from "playwright==$PLAYWRIGHT_VERSION" \
  playwright install --with-deps chromium
echo "done ($SECONDS s)" >> "$LOG"
exit 0
