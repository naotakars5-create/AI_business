#!/usr/bin/env bash
# 週次パイプライン:
#   米国先行サービスをリサーチ → 要約ページ(GitHub Pages)を公開 → LINEにカード配信
#
# 使い方:
#   ./run.sh            本番実行
#   ./run.sh --dry-run  配信・push せず、カード内容とページを手元で確認
#
# cron から起動される前提のため、すべて絶対パスで動く。
# どの工程で失敗しても LINE への配信は行わず、理由をログに残して終了する。
# 注意: .env の中身は絶対にログ・標準出力に出さない（set -x 禁止）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# cron は PATH がほぼ空なので、claude / python3 / git の一般的な設置場所を明示的に足す
export PATH="/opt/node22/bin:/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.claude/local:/usr/bin:/bin:$PATH"

LOG_DIR="$SCRIPT_DIR/logs"
ARCHIVE_DIR="$SCRIPT_DIR/archive"
DOCS_DIR="$SCRIPT_DIR/docs"
mkdir -p "$LOG_DIR" "$ARCHIVE_DIR" "$DOCS_DIR"

TODAY="$(date +%F)"
DATE_JP="$(python3 -c 'import datetime as d; t=d.date.today(); print(f"{t.year}/{t.month}/{t.day}")')"
LOG_FILE="$LOG_DIR/$TODAY.log"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log()  { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG_FILE"; }
fail() { log "ERROR: $*"; log "LINE配信は行わず終了します"; exit 1; }
on_err() { fail "予期しないエラーで中断（run.sh ${1}行目付近）"; }
trap 'on_err $LINENO' ERR

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
DATA_FILE="$ARCHIVE_DIR/$TODAY.json"
SENT_MARKER="$LOG_DIR/sent-$TODAY"
if [ "$DRY_RUN" = 1 ]; then
  ARCHIVE_FILE="$ARCHIVE_DIR/$TODAY.dryrun.md"
  DATA_FILE="$ARCHIVE_DIR/$TODAY.dryrun.json"
fi

log "=== 実行開始 (dry-run=$DRY_RUN) ==="

# --- 公開ページのベースURL（git remote から自動導出） ---------------------
# 例: https://github.com/owner/repo → https://owner.github.io/repo
if [ -z "${PAGE_BASE_URL:-}" ]; then
  REMOTE="$(git -C "$SCRIPT_DIR" config --get remote.origin.url 2>/dev/null || true)"
  PAGE_BASE_URL="$(python3 - "$REMOTE" <<'PY'
import re, sys
m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", sys.argv[1] or "")
print(f"https://{m.group(1).lower()}.github.io/{m.group(2)}" if m else "")
PY
)"
fi
[ -n "$PAGE_BASE_URL" ] || fail "公開ページのURLを判定できません（PAGE_BASE_URL を設定してください）"
PAGE_URL="$PAGE_BASE_URL/$TODAY.html"

# --- 二重配信防止 ---------------------------------------------------------
SKIP_GEN=0
if [ "$DRY_RUN" = 0 ]; then
  if [ -f "$SENT_MARKER" ]; then
    log "本日分（$TODAY）は配信済みのため何もしません"
    exit 0
  fi
  if [ -f "$ARCHIVE_FILE" ] && [ -f "$DATA_FILE" ]; then
    log "本日分の生成済みレポートを再利用し、公開・配信のみ再試行します"
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
  # vol番号は「git管理下にある公開済みページ数 + 1」。archive/ はgit管理外なので、
  # GitHub Actions のようにクローンし直す環境でも番号が巻き戻らない。
  # dry-runで作った未コミットのページは数に入らない。
  VOL=$(( $(git -C "$SCRIPT_DIR" ls-files 'docs/*.html' 2>/dev/null \
            | grep -cE 'docs/[0-9]{4}-[0-9]{2}-[0-9]{2}\.html$') + 1 ))
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
  [ -n "$CLAUDE_BIN" ] || fail "claude CLI が見つかりません"
  log "claude によるリサーチ・本文生成を開始（数分かかります）"
  if ! "$CLAUDE_BIN" -p --allowedTools "WebSearch,WebFetch" \
        < "$TMP_DIR/prompt.txt" > "$TMP_DIR/report.raw" 2> "$TMP_DIR/claude.err"; then
    log "claude stderr（末尾）: $(tail -c 1000 "$TMP_DIR/claude.err" | tr '\n' ' ')"
    fail "本文生成に失敗しました"
  fi

  # --- 3) 生成物を検証して archive/ に保存 --------------------------------
  # 本文とカード用JSONの両方をここで厳密に検証する。半端な状態でLINEに飛ばさない。
  COMPANIES="$(python3 "$SCRIPT_DIR/validate_report.py" "$TMP_DIR/report.raw" \
      "$ARCHIVE_FILE" "$DATA_FILE" "$DATE_JP" "$VOL" 2>"$TMP_DIR/validate.err")" || {
    # 生成物を残しておくと原因を追える（レポート本文のみ。秘匿情報は含まれない）
    cp "$TMP_DIR/report.raw" "$LOG_DIR/failed-$TODAY.txt" 2>/dev/null || true
    log "検証エラー: $(tr '\n' ' ' < "$TMP_DIR/validate.err" 2>/dev/null)"
    log "生成物は $LOG_DIR/failed-$TODAY.txt に保存しました"
    fail "生成された本文が検証を通りませんでした"
  }
  log "archive に保存しました: $ARCHIVE_FILE"

  # --- 4) 今回の企業を excluded.json に追記（dry-run では更新しない） -----
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
    log "excluded.json に追記しました: $COMPANIES"
  else
    log "dry-run のため excluded.json は更新しません（対象: $COMPANIES）"
  fi
fi

# vol番号を確定（再利用時も archive のヘッダーから読み直す）
VOL="$(head -1 "$ARCHIVE_FILE" | sed -n 's/.*vol\.\([0-9]*\).*/\1/p')"
[ -n "$VOL" ] || fail "vol番号を判定できませんでした"

# --- 5) 要約ページを生成 --------------------------------------------------
python3 "$SCRIPT_DIR/build_page.py" --body "$ARCHIVE_FILE" --data "$DATA_FILE" \
  --out-dir "$DOCS_DIR" --date "$TODAY" --date-jp "$DATE_JP" --vol "$VOL" 2>&1 | tee -a "$LOG_FILE" \
  || fail "要約ページの生成に失敗しました"

# --- 6) ページを公開（git push）してから配信 ------------------------------
if [ "$DRY_RUN" = 0 ]; then
  BRANCH="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD)"
  log "要約ページを公開します（branch: $BRANCH）"
  git -C "$SCRIPT_DIR" add docs/ excluded.json
  if ! git -C "$SCRIPT_DIR" diff --cached --quiet; then
    git -C "$SCRIPT_DIR" commit -q -m "レポート公開 $TODAY vol.$VOL" || fail "コミットに失敗しました"
  fi
  PUSHED=0
  for delay in 2 4 8 16; do
    if git -C "$SCRIPT_DIR" push -u origin "$BRANCH" >/dev/null 2>&1; then PUSHED=1; break; fi
    log "push に失敗。${delay}秒後に再試行します"
    sleep "$delay"
  done
  [ "$PUSHED" = 1 ] || fail "要約ページの push に失敗しました（リンク切れを避けるため配信しません）"

  # GitHub Pages のビルド完了を待つ（最大5分）。公開前に配信するとリンク切れになる
  log "ページの公開反映を待機します: $PAGE_URL"
  LIVE=0
  for _ in $(seq 1 30); do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' -L "$PAGE_URL" || echo 000)"
    if [ "$CODE" = "200" ]; then LIVE=1; break; fi
    sleep 10
  done
  [ "$LIVE" = 1 ] || fail "ページが公開されません（$PAGE_URL）。GitHub Pages の設定を確認してください"
  log "ページ公開を確認しました"
fi

# --- 7) LINE配信 ----------------------------------------------------------
SEND_ARGS=(--mode flex --data "$DATA_FILE" --page-url "$PAGE_URL"
           --date-jp "$DATE_JP" --vol "$VOL" --date "$TODAY")
if [ "$DRY_RUN" = 1 ]; then
  log "dry-run: 配信せず、カード内容を表示します（ページURL: $PAGE_URL）"
  python3 "$SCRIPT_DIR/send_line.py" "${SEND_ARGS[@]}" --dry-run 2>&1 | tee -a "$LOG_FILE" \
    || fail "send_line.py (dry-run) が失敗しました"
else
  log "LINE配信を開始します"
  python3 "$SCRIPT_DIR/send_line.py" "${SEND_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE" \
    || fail "LINE配信に失敗しました。次回実行時に同じ内容・同じRetry-Keyで再試行されます"
  touch "$SENT_MARKER"
  log "配信完了"
fi

log "=== 実行終了 ==="
