#!/bin/bash
# 検査（make lint・make test・make e2e・make gate2 など）を順に流し、結果をログに書く（make checks から使う）。
#
# 使い方: tools/run_checks.sh <ログ> <1 件の上限秒> "<名前>::<コマンド>" ...
#   例: tools/run_checks.sh .checks/logs/2026-10-21_1530_phase4-blockD_checks.log 900 "LINT::make lint" "TEST::make test"
# - 各検査の開始・終了の時刻と終了コードを、成否にかかわらずログに書く。上限を超えたら止め
#   （終了コード 124）、残った e2e のコンテナも止める。
# - 最後に必ず「=== DONE」を書く（途中で止めても書く）。待つ側はこの行と上限の時間で待つ。
# - コマンドの出力そのものは <ログ> の .log を .full.log にしたファイルに書く。終了コードは、1 つでも失敗があれば 1。
# - 同じフォルダの latest.log・latest.full.log を、このログを指すリンクにする（いちばん新しいログを開く入口）。
# - ログの先頭に、どこで動かしたか（RUNNER。Makefile の docker か host）を書く。pytest が飛ばした
#   テストの件数と理由（-rs の SKIPPED の行）も、検査ごとに写す。
set -u
cd "$(dirname "$0")/.." || exit 1
log=$1; limit=$2; shift 2
full="${log%.log}.full.log"
dir=$(dirname "$log")
mkdir -p "$dir"
: > "$log"; : > "$full"
ln -sfn "$(basename "$log")" "$dir/latest.log"
ln -sfn "$(basename "$full")" "$dir/latest.full.log"
failed=0
trap 'echo "=== DONE failed=$failed" >> "$log"' EXIT
echo "=== RUNNER=${RUNNER:-docker} (start $(date '+%Y-%m-%d %H:%M:%S'))" >> "$log"
for item in "$@"; do
  name=${item%%::*}; cmd=${item#*::}
  echo "=== $name (start $(date +%H:%M:%S))" >> "$log"
  echo "=== $name" >> "$full"
  bash -c "$cmd" >> "$full" 2>&1 &
  pid=$!; waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$limit" ]; do sleep 5; waited=$((waited + 5)); done
  if kill -0 "$pid" 2>/dev/null; then
    pkill -TERM -P "$pid"; kill "$pid" 2>/dev/null
    if command -v docker >/dev/null; then
      docker ps -q --filter name=e2e-run | xargs docker stop >/dev/null 2>&1
    fi
    code=124; echo "TIMEOUT after ${limit}s" >> "$log"
  else
    wait "$pid"; code=$?
  fi
  # pytest の結果の行（件数）と、make のエラーの行だけを要約に写す
  sed -n "/^=== $name\$/,\$p" "$full" | grep -E "[0-9]+ (passed|failed|errors?)\b.* in [0-9.]+s|^make: \*\*\*|^Found [0-9]+ error|^Success:" | tail -n 3 >> "$log"
  sed -n "/^=== $name\$/,\$p" "$full" | grep -E "^SKIPPED \[[0-9]+\]" | head -n 10 >> "$log"
  echo "=== $name exit=$code (end $(date +%H:%M:%S))" >> "$log"
  [ "$code" -ne 0 ] && failed=$((failed + 1))
done
cat "$log"
[ "$failed" -eq 0 ]
