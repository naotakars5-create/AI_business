#!/usr/bin/env bash
# run.sh を「毎週月曜 8:00 JST」に実行する cron ジョブを登録する。
# - 既存の crontab を必ず保全し、未登録の場合のみ末尾に追記する
# - マシンのタイムゾーンが JST 以外でも、月曜8:00 JST 相当のローカル時刻に変換して登録する
# ※ dry-run で内容を確認し、OKを出したあとに「実際に運用するマシン」で実行すること。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_SH="$SCRIPT_DIR/run.sh"

command -v crontab >/dev/null 2>&1 || {
  echo "ERROR: このマシンに crontab がありません。cron が使えるマシンで実行してください" >&2
  exit 1
}
[ -x "$RUN_SH" ] || chmod +x "$RUN_SH"

# 月曜 8:00 JST をこのマシンのローカル時刻に変換して cron の「分 時 * * 曜日」を得る
CRON_TIME="$(python3 - <<'PY'
import datetime
JST = datetime.timezone(datetime.timedelta(hours=9))
# 基準として任意の月曜 8:00 JST を取り、ローカル時刻へ変換（曜日跨ぎも反映）
base = datetime.datetime(2026, 1, 5, 8, 0, tzinfo=JST)  # 2026-01-05 は月曜
local = base.astimezone()
dow = local.isoweekday() % 7  # cron: 0=日曜
print(f"{local.minute} {local.hour} * * {dow}")
PY
)"

CRON_LINE="$CRON_TIME $RUN_SH >> $SCRIPT_DIR/logs/cron.log 2>&1"
COMMENT_LINE="# 米国先行サービス週次LINE配信（毎週月曜 8:00 JST 相当）"

EXISTING="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "$EXISTING" | grep -Fq "$RUN_SH"; then
  echo "既に登録済みです。現在の crontab:"
  crontab -l
  exit 0
fi

{
  if [ -n "$EXISTING" ]; then printf '%s\n' "$EXISTING"; fi
  printf '%s\n' "$COMMENT_LINE"
  printf '%s\n' "$CRON_LINE"
} | crontab -

echo "登録しました。現在の crontab:"
crontab -l
echo
echo "注意: このマシンのタイムゾーンが夏時間(DST)を使う地域の場合、"
echo "夏時間の切替時に実行時刻が1時間ずれます（日本のマシンなら影響なし）。"
