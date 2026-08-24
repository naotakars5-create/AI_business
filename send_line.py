#!/usr/bin/env python3
"""LINE公式アカウントへの一斉配信（broadcast）スクリプト。標準ライブラリのみ使用。

LINE Messaging API の制限値（2026-08-24 に公式ドキュメント・公式OpenAPI定義で確認）:
- テキストメッセージ text の最大文字数: 5,000
  https://developers.line.biz/ja/reference/messaging-api/#text-message
- 1リクエストの messages 配列は最大5オブジェクト（BroadcastRequest maxItems: 5）
  https://github.com/line/line-openapi/blob/main/messaging-api.yml
- X-Line-Retry-Key: 任意の方法で生成したUUID。初回リクエストから24時間有効で、
  受理済みキーによる再送は 409 が返り二重配信されない
  https://developers.line.biz/ja/docs/messaging-api/retrying-api-request/
- 無料プラン（コミュニケーションプラン）は月200通まで。
  消費通数 = 配信したメッセージオブジェクト数 × 友だち数

注意: このスクリプトはトークンを標準出力・ログに一切出さない。
"""

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"

API_BASE = "https://api.line.me"
BROADCAST_URL = API_BASE + "/v2/bot/message/broadcast"

TEXT_LIMIT = 5000        # LINEのテキスト1通あたり最大文字数
SAFETY_LIMIT = 4800      # 「（n/m）」プレフィックスと安全マージンを引いた実効上限
MAX_MESSAGES_PER_REQ = 5 # 1リクエストで送れるメッセージオブジェクト数

DIVIDER = "─────"
DIVIDER_RE = re.compile(r"^─{3,}\s*$")


def load_token() -> str:
    """`.env` から LINE_CHANNEL_ACCESS_TOKEN を読む（値はどこにも出力しない）。"""
    if not ENV_FILE.exists():
        raise SystemExit("ERROR: .env がありません。.env.example をコピーして作成してください")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("LINE_CHANNEL_ACCESS_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
            if token:
                return token
    raise SystemExit("ERROR: .env の LINE_CHANNEL_ACCESS_TOKEN が空です")


def split_blocks(text: str) -> list:
    """`─────` の罫線行を境目に本文をブロックへ分ける。罫線自体は含めない。"""
    blocks, current = [], []
    for line in text.splitlines():
        if DIVIDER_RE.match(line):
            if current:
                blocks.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def pack_messages(text: str) -> list:
    """ブロック（企業単位）の境目だけで分割し、上限内に収まるよう貪欲に詰める。

    文や箇条書きの途中では絶対に切らない。1ブロックが単体で上限を超える場合は
    分割せずエラーにする（配信前に必ず気づけるように）。
    """
    blocks = split_blocks(text)
    if not blocks:
        raise SystemExit("ERROR: 本文が空です")
    joiner = "\n" + DIVIDER + "\n"
    messages, current = [], ""
    for block in blocks:
        if len(block) > SAFETY_LIMIT:
            raise SystemExit(
                f"ERROR: 1ブロックが{SAFETY_LIMIT}文字を超えています（{len(block)}文字）。"
                "ブロック途中では分割しないため、本文を短くしてください"
            )
        candidate = block if not current else current + joiner + block
        if len(candidate) <= SAFETY_LIMIT:
            current = candidate
        else:
            messages.append(current)
            current = block
    if current:
        messages.append(current)
    if len(messages) > 1:
        n = len(messages)
        messages = [f"（{i}/{n}）\n{m}" for i, m in enumerate(messages, 1)]
    return messages


def retry_key_for(date_str: str, batch_idx: int) -> str:
    """日付から決定的にUUIDを導出する。同日の再実行では同じキーになり、
    LINE側の24時間の冪等性（受理済みは409）で二重配信を防ぐ。"""
    name = f"ai-business-line-broadcast/{date_str}/batch-{batch_idx}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def api_request(token: str, method: str, url: str, payload=None, retry_key=None):
    """LINE APIを呼ぶ。戻り値は (status, body_text)。HTTPエラーもボディ付きで返す。"""
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if retry_key:
        headers["X-Line-Retry-Key"] = retry_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def fetch_usage_info(token: str) -> str:
    """友だち数・月間上限・今月の消費通数を取得して要約する（すべてベストエフォート）。"""
    lines = []
    followers = None
    status, body = api_request(token, "GET", API_BASE + "/v2/bot/insight/followers?date="
                               + (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d"))
    if status == 200:
        try:
            followers = json.loads(body).get("followers")
        except ValueError:
            pass
    if followers is not None:
        lines.append(f"友だち数（前日時点の目安）: {followers}")
    status, body = api_request(token, "GET", API_BASE + "/v2/bot/message/quota")
    if status == 200:
        try:
            q = json.loads(body)
            if q.get("type") == "limited":
                lines.append(f"月間配信上限: {q.get('value')}通")
        except ValueError:
            pass
    status, body = api_request(token, "GET", API_BASE + "/v2/bot/message/quota/consumption")
    if status == 200:
        try:
            lines.append(f"今月の消費済み通数: {json.loads(body).get('totalUsage')}通")
        except ValueError:
            pass
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="LINE broadcast 配信")
    parser.add_argument("--file", required=True, help="配信する本文ファイル（archive/YYYY-MM-DD.md）")
    parser.add_argument("--date", default=str(datetime.date.today()),
                        help="Retry-Key導出に使う日付 YYYY-MM-DD（既定: 今日）")
    parser.add_argument("--dry-run", action="store_true",
                        help="APIを呼ばず、分割後の各メッセージと通数を表示する")
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8").strip()
    # 万一 機械処理用の行が残っていても配信には載せない
    text = re.sub(r"^@@COMPANIES:.*$", "", text, flags=re.M).strip()

    messages = pack_messages(text)
    n = len(messages)
    print(f"分割後のメッセージ数: {n}通")
    for i, m in enumerate(messages, 1):
        print(f"  メッセージ{i}: {len(m)}文字")
    print("※ 実際の消費通数は「メッセージ数 × 友だち数」（無料プランは月200通まで）")

    if args.dry_run:
        for i, m in enumerate(messages, 1):
            print()
            print(f"========== メッセージ {i}/{n}（{len(m)}文字・実際に送られる全文） ==========")
            print(m)
        print()
        print("========== dry-run のため配信していません ==========")
        return

    token = load_token()
    batches = [messages[i:i + MAX_MESSAGES_PER_REQ]
               for i in range(0, n, MAX_MESSAGES_PER_REQ)]
    for idx, batch in enumerate(batches):
        key = retry_key_for(args.date, idx)
        payload = {"messages": [{"type": "text", "text": m} for m in batch]}
        status, body = api_request(token, "POST", BROADCAST_URL, payload, retry_key=key)
        if status == 200:
            print(f"broadcast 成功 (batch {idx + 1}/{len(batches)}, {len(batch)}メッセージ)")
        elif status == 409:
            # 同じRetry-Keyのリクエストが受理済み = 既に配信されている
            print(f"broadcast は受理済みのためスキップ (batch {idx + 1}/{len(batches)}, HTTP 409)")
        else:
            print(f"ERROR: broadcast 失敗 (batch {idx + 1}/{len(batches)}) HTTP {status}")
            print(f"レスポンスボディ: {body}")
            sys.exit(1)

    usage = fetch_usage_info(token)
    if usage:
        print(usage)


if __name__ == "__main__":
    main()
