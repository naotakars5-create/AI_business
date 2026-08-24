#!/usr/bin/env bash
# 週次パイプライン: 米国先行サービスをリサーチ → レポート生成 → LINE一斉配信
#
# 使い方:
#   ./run.sh            本番実行（生成 → archive保存 → excluded更新 → LINE配信）
#   ./run.sh --dry-run  配信せず、分割後メッセージをターミナル表示（excludedも更新しない）
#
# cron から起動される前提のため、すべて絶対パスで動く。
# どの工程で失敗しても LINE への配信は行わず、理由をログに残して終了する。
# 注意: .env の中身は絶対にログ・標準出力に出さない（set -x 禁止）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# cron は PATH がほぼ空なので、claude / python3 の一般的な設置場所を明示的に足す
export PATH="/opt/node22/bin:/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.claude/local:/usr/bin:/bin:$PATH"

LOG_DIR="$SCRIPT_DIR/logs"
ARCHIVE_DIR="$SCRIPT_DIR/archive"
mkdir -p "$LOG_DIR" "$ARCHIVE_DIR"

TODAY="$(date +%F)"                                   # YYYY-MM-DD
DATE_JP="$(python3 -c 'import datetime as d; t=d.date.today(); print(f"{t.year}/{t.month}/{t.day}")')"
LOG_FILE="$LOG_DIR/$TODAY.log"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log()  { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"; }
fail() { log "ERROR: $*"; log "LINE配信は行わず終了します"; exit 1; }
on_err() { fail "予期しないエラーで中断（run.sh ${1}行目付近）"; }
trap 'on_err $LINENO' ERR

# 7日より古いログを削除
find "$LOG_DIR" -type f -mtime +7 -delete 2>/dev/null || true

# 多重起動防止ロック（12時間超の残留ロックはクラッシュ由来とみなして解除）
LOCK_DIR="$SCRIPT_DIR/.run.lock"
TMP_DIR=""
cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if [ -n "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
}
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -n "$(find "$SCRIPT_DIR" -maxdepth 1 -name .run.lock -mmin +720 2>/dev/null)" ]; then
    log "12時間以上前の残留ロックを解除して続行します"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  mkdir "$LOCK_DIR" 2>/dev/null || { log "ERROR: 別の実行が進行中です（.run.lock あり）"; exit 1; }
fi
trap cleanup EXIT
TMP_DIR="$(mktemp -d)"

ARCHIVE_FILE="$ARCHIVE_DIR/$TODAY.md"
SENT_MARKER="$LOG_DIR/sent-$TODAY"
[ "$DRY_RUN" = 1 ] && ARCHIVE_FILE="$ARCHIVE_DIR/$TODAY.dryrun.md"

log "=== 実行開始 (dry-run=$DRY_RUN) ==="

# --- 二重配信防止 ---------------------------------------------------------
# 配信済みマーカーがあれば何もしない。
# archive だけあってマーカーが無い場合は「生成成功・配信失敗」なので、
# 再生成せず同じ本文・同じRetry-Keyで配信だけ再試行する。
SKIP_GEN=0
if [ "$DRY_RUN" = 0 ]; then
  if [ -f "$SENT_MARKER" ]; then
    log "本日分（$TODAY）は配信済みのため何もしません"
    exit 0
  fi
  if [ -f "$ARCHIVE_FILE" ]; then
    log "本日分の生成済みレポートを再利用し、配信のみ再試行します"
    SKIP_GEN=1
  fi
fi

# --- 1) excluded.json 読み込み・検証 --------------------------------------
EXCLUDED_JSON="$SCRIPT_DIR/excluded.json"
[ -f "$EXCLUDED_JSON" ] || fail "excluded.json がありません"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$EXCLUDED_JSON" \
  || fail "excluded.json が壊れています（JSONとして読めません）"

if [ "$SKIP_GEN" = 0 ]; then
  # --- 2) claude -p でリサーチ・本文生成 ----------------------------------
  # vol番号は archive/ の本番レポート数 + 1（dry-runの産物は数えない）
  VOL=$(( $(find "$ARCHIVE_DIR" -maxdepth 1 -name '*.md' ! -name '*.dryrun.md' | wc -l) + 1 ))
  log "vol.$VOL としてレポートを生成します（日付: $DATE_JP）"

  python3 - "$SCRIPT_DIR/prompt.md" "$EXCLUDED_JSON" "$DATE_JP" "$VOL" > "$TMP_DIR/prompt.txt" <<'PY'
import json, sys
tpl = open(sys.argv[1], encoding="utf-8").read()
excluded = json.load(open(sys.argv[2], encoding="utf-8"))
lines = "\n".join(f"- {c}" for c in excluded)
sys.stdout.write(tpl.replace("{{EXCLUDED}}", lines)
                    .replace("{{DATE}}", sys.argv[3])
                    .replace("{{VOL}}", sys.argv[4]))
PY

  CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
  [ -n "$CLAUDE_BIN" ] || fail "claude CLI が見つかりません（PATH: 主要な場所を確認済み）"
  log "claude によるリサーチ・本文生成を開始（数分かかります）"
  if ! "$CLAUDE_BIN" -p --allowedTools "WebSearch,WebFetch" \
        < "$TMP_DIR/prompt.txt" > "$TMP_DIR/report.raw" 2> "$TMP_DIR/claude.err"; then
    log "claude stderr（末尾）: $(tail -c 1000 "$TMP_DIR/claude.err" | tr '\n' ' ')"
    fail "本文生成に失敗しました"
  fi

  # --- 3) 生成物を検証して archive/ に保存 --------------------------------
  # 不正な本文はここで弾く。半端な状態でLINEに飛ばさない。
  COMPANIES="$(python3 - "$TMP_DIR/report.raw" "$ARCHIVE_FILE" "$DATE_JP" "$VOL" 2>"$TMP_DIR/validate.err" <<'PY'
import re, sys
raw = open(sys.argv[1], encoding="utf-8").read().strip()
archive, date_jp, vol = sys.argv[2], sys.argv[3], sys.argv[4]

def die(msg):
    print(f"検証NG: {msg}", file=sys.stderr)
    sys.exit(1)

if not raw:
    die("生成結果が空")
header = raw.splitlines()[0].strip()
expect = f"【米国先行サービス→日本落とし込み】{date_jp} vol.{vol}"
if header != expect:
    die(f"1行目が不正: {header!r}（期待: {expect!r}）")
for i in range(1, 7):
    if f"【{i}】" not in raw:
        die(f"【{i}】のブロックが見つからない")
m = re.search(r"^@@COMPANIES:(.+?)@@\s*$", raw, re.M)
if not m:
    die("@@COMPANIES 行が見つからない")
names = [n.strip() for n in m.group(1).split("|") if n.strip()]
if len(names) != 6:
    die(f"@@COMPANIES の社名が6件でない（{len(names)}件）")
body = re.sub(r"^@@COMPANIES:.*$", "", raw, flags=re.M).strip() + "\n"
if "**" in body or re.search(r"^#{1,6} ", body, re.M):
    die("本文にMarkdown記法（** や #）が残っている")
open(archive, "w", encoding="utf-8").write(body)
print(" | ".join(names))
PY
  )" || { log "検証エラー: $(cat "$TMP_DIR/validate.err" 2>/dev/null | tr '\n' ' ')"; fail "生成された本文が検証を通りませんでした"; }
  log "archive に保存しました: $ARCHIVE_FILE"

  # --- 4) 今回の6社を excluded.json に追記（dry-run では更新しない） ------
  if [ "$DRY_RUN" = 0 ]; then
    python3 - "$EXCLUDED_JSON" "$COMPANIES" <<'PY'
import json, sys
path = sys.argv[1]
names = [n.strip() for n in sys.argv[2].split("|") if n.strip()]
data = json.load(open(path, encoding="utf-8"))
for n in names:
    if n not in data:
        data.append(n)
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
    log "excluded.json に6社を追記しました: $COMPANIES"
  else
    log "dry-run のため excluded.json は更新しません（対象: $COMPANIES）"
  fi
fi

# --- 5) LINE配信 ----------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  log "dry-run: 配信せず、分割後メッセージを表示します"
  python3 "$SCRIPT_DIR/send_line.py" --file "$ARCHIVE_FILE" --date "$TODAY" --dry-run 2>&1 | tee -a "$LOG_FILE" \
    || fail "send_line.py (dry-run) が失敗しました"
else
  log "LINE broadcast 配信を開始します"
  python3 "$SCRIPT_DIR/send_line.py" --file "$ARCHIVE_FILE" --date "$TODAY" 2>&1 | tee -a "$LOG_FILE" \
    || fail "LINE配信に失敗しました。次回実行時に同じ本文・同じRetry-Keyで再試行されます"
  touch "$SENT_MARKER"
  log "配信完了"
fi

log "=== 実行終了 ==="
