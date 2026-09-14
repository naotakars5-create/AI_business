#!/usr/bin/env python3
"""LINE公式アカウントへの一斉配信（broadcast）スクリプト。標準ライブラリのみ使用。

配信形式: Flex Message のカルーセル（横スクロールするカード 3〜5枚）。
  内訳は企業2〜4社ぶん + 「レポート全文」カード1枚。
各カードには「要約を見る」（GitHub Pages の該当企業へのアンカーリンク）と
「元記事」（出典記事URL）の2つのボタンを付ける。

LINE Messaging API の制限値（2026-08-24 に公式ドキュメント・公式OpenAPI定義で確認）:
- 1リクエストの messages 配列は最大5オブジェクト（BroadcastRequest maxItems: 5）
  https://github.com/line/line-openapi/blob/main/messaging-api.yml
- Flex Message の altText は最大400文字、カルーセルのJSON全体は最大50KB
  https://developers.line.biz/ja/docs/messaging-api/using-flex-messages/
- カルーセルに入れられるバブル数の上限はドキュメント上10〜12。
  本スクリプトは最大5枚しか作らないため、上限には決して達しない
- テキストメッセージ text は最大5,000文字（--mode text 用）
  https://developers.line.biz/ja/reference/messaging-api/#text-message
- X-Line-Retry-Key: 任意の方法で生成したUUID。初回リクエストから24時間有効で、
  受理済みキーによる再送は 409 が返り二重配信されない
  https://developers.line.biz/ja/docs/messaging-api/retrying-api-request/
- 無料プラン（コミュニケーションプラン）は月200通まで。
  消費通数 = 配信したメッセージオブジェクト数 × 友だち数
  → カルーセルは何枚でも「1メッセージ」なので、1回の配信 = 友だち数ぶんの通数

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

TEXT_LIMIT = 5000         # テキスト1通あたり最大文字数
SAFETY_LIMIT = 4800       # 「（n/m）」と安全マージンを引いた実効上限（--mode text 用）
MAX_MESSAGES_PER_REQ = 5  # 1リクエストのメッセージオブジェクト数上限
ALT_TEXT_LIMIT = 400      # Flex の altText 上限
FLEX_JSON_LIMIT = 50_000  # カルーセルJSONのバイト上限（50KB）
MIN_CARDS, MAX_CARDS = 2, 4

DIVIDER = "─────"
DIVIDER_RE = re.compile(r"^─{3,}\s*$")

ACCENT = "#06C755"  # LINEグリーン


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


# ---------------------------------------------------------------- Flex カード

def _text(text, **kw):
    node = {"type": "text", "text": text, "wrap": True}
    node.update(kw)
    return node


def build_bubble(idx: int, company: dict, page_url: str) -> dict:
    """1社ぶんのカード（バブル）を作る。"""
    name = company["name"]
    tagline = company.get("tagline", "")
    lines = company.get("card", [])
    source_url = company.get("source_url", "")
    anchor_url = f"{page_url}#c{idx}"

    body_contents = [
        _text(f"{idx}. {name}", weight="bold", size="lg", color="#1A1D21"),
    ]
    if tagline:
        body_contents.append(_text(tagline, size="sm", color="#8C939C", margin="xs"))
    body_contents.append({"type": "separator", "margin": "md", "color": "#E3E6EA"})

    for ln in lines:
        body_contents.append({
            "type": "box", "layout": "horizontal", "margin": "md", "spacing": "sm",
            "contents": [
                _text("・", size="sm", color=ACCENT, flex=0),
                _text(ln, size="sm", color="#404751", flex=1),
            ],
        })

    footer_contents = [{
        "type": "button", "style": "primary", "height": "sm", "color": ACCENT,
        "action": {"type": "uri", "label": "要約を見る", "uri": anchor_url},
    }]
    if source_url.startswith("http"):
        footer_contents.append({
            "type": "button", "style": "link", "height": "sm",
            "action": {"type": "uri", "label": "元記事を読む", "uri": source_url},
        })

    return {
        "type": "bubble", "size": "mega",
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px",
                 "backgroundColor": "#FFFFFF", "contents": body_contents},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                   "paddingAll": "12px", "contents": footer_contents},
    }


def build_flex_message(data: dict, page_url: str, date_jp: str, vol) -> dict:
    """カルーセル1件（＝1メッセージオブジェクト）を組み立てる。"""
    companies = data.get("companies", [])
    if not MIN_CARDS <= len(companies) <= MAX_CARDS:
        raise SystemExit(
            f"ERROR: カード枚数は{MIN_CARDS}〜{MAX_CARDS}枚である必要があります（{len(companies)}件）")

    bubbles = [build_bubble(i, c, page_url) for i, c in enumerate(companies, 1)]

    # 末尾に「全部まとめて読む」カードを足す（一覧ページへの導線）
    bubbles.append({
        "type": "bubble", "size": "mega",
        "body": {"type": "box", "layout": "vertical", "paddingAll": "20px",
                 "justifyContent": "center", "spacing": "md", "contents": [
                     _text("今回のレポート全文", weight="bold", size="lg", color="#1A1D21"),
                     _text(f"{len(companies)}社の詳細・調達額・日本での壁を"
                           "まとめて読めます", size="sm", color="#8C939C"),
                 ]},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "12px", "contents": [{
            "type": "button", "style": "primary", "height": "sm", "color": ACCENT,
            "action": {"type": "uri", "label": "レポート全文を読む", "uri": page_url},
        }]},
    })

    names = "、".join(c["name"] for c in companies)
    alt = f"【米国先行サービス→日本落とし込み】{date_jp} vol.{vol}｜{names}"
    if len(alt) > ALT_TEXT_LIMIT:
        alt = alt[:ALT_TEXT_LIMIT - 1] + "…"

    return {"type": "flex", "altText": alt,
            "contents": {"type": "carousel", "contents": bubbles}}


# ---------------------------------------------------------------- テキスト分割

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
    """企業ブロックの境目だけで分割し、上限内に収まるよう貪欲に詰める（--mode text 用）。

    文や箇条書きの途中では絶対に切らない。
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
                "ブロック途中では分割しないため、本文を短くしてください")
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


# ---------------------------------------------------------------- API 呼び出し

def retry_key_for(date_str: str, batch_idx: int) -> str:
    """日付から決定的にUUIDを導出する。同日の再実行では同じキーになり、
    LINE側の24時間の冪等性（受理済みは409）で二重配信を防ぐ。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"ai-business-line-broadcast/{date_str}/batch-{batch_idx}"))


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
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    status, body = api_request(token, "GET",
                               f"{API_BASE}/v2/bot/insight/followers?date={yesterday}")
    followers = None
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


def send(messages: list, date_str: str) -> None:
    """メッセージを broadcast する。失敗時は終了コード1。"""
    token = load_token()
    batches = [messages[i:i + MAX_MESSAGES_PER_REQ]
               for i in range(0, len(messages), MAX_MESSAGES_PER_REQ)]
    for idx, batch in enumerate(batches):
        status, body = api_request(token, "POST", BROADCAST_URL,
                                   {"messages": batch}, retry_key=retry_key_for(date_str, idx))
        if status == 200:
            print(f"broadcast 成功 (batch {idx + 1}/{len(batches)}, {len(batch)}メッセージ)")
        elif status == 409:
            print(f"broadcast は受理済みのためスキップ (batch {idx + 1}/{len(batches)}, HTTP 409)")
        else:
            print(f"ERROR: broadcast 失敗 (batch {idx + 1}/{len(batches)}) HTTP {status}")
            print(f"レスポンスボディ: {body}")
            sys.exit(1)
    usage = fetch_usage_info(token)
    if usage:
        print(usage)


# ---------------------------------------------------------------- エントリポイント

def main() -> None:
    ap = argparse.ArgumentParser(description="LINE broadcast 配信")
    ap.add_argument("--mode", choices=["flex", "text"], default="flex",
                    help="flex=カード配信（既定） / text=テキスト配信")
    ap.add_argument("--data", help="[flex] カード用データJSON")
    ap.add_argument("--page-url", help="[flex] 要約ページのURL")
    ap.add_argument("--date-jp", default="", help="[flex] altText用の日付 YYYY/M/D")
    ap.add_argument("--vol", default="", help="[flex] altText用のvol番号")
    ap.add_argument("--file", help="[text] 配信する本文ファイル")
    ap.add_argument("--date", default=str(datetime.date.today()),
                    help="Retry-Key導出に使う日付 YYYY-MM-DD（既定: 今日）")
    ap.add_argument("--dry-run", action="store_true",
                    help="APIを呼ばず、送信内容と通数を表示する")
    args = ap.parse_args()

    if args.mode == "flex":
        if not args.data or not args.page_url:
            raise SystemExit("ERROR: --mode flex には --data と --page-url が必要です")
        data = json.loads(Path(args.data).read_text(encoding="utf-8"))
        msg = build_flex_message(data, args.page_url, args.date_jp, args.vol)
        size = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        if size > FLEX_JSON_LIMIT:
            raise SystemExit(f"ERROR: Flex JSONが50KBを超えています（{size}バイト）")
        cards = len(msg["contents"]["contents"])

        print(f"配信形式: Flex カルーセル（カード{cards}枚 = 企業{cards - 1}社 + 全文カード1枚）")
        print(f"JSONサイズ: {size:,}バイト / 上限50,000バイト")
        print(f"altText: {len(msg['altText'])}文字 / 上限{ALT_TEXT_LIMIT}文字")
        print("送信メッセージ数: 1通（カルーセルは何枚でも1メッセージ扱い）")
        print("※ 実際の消費通数は「1 × 友だち数」（無料プランは月200通まで）")

        if args.dry_run:
            print("\n========== カード内容（実際に配信される全文） ==========")
            for i, b in enumerate(msg["contents"]["contents"], 1):
                print(f"\n--- カード {i}/{cards} ---")
                for node in b["body"]["contents"]:
                    if node.get("type") == "text":
                        print(f"  {node['text']}")
                    elif node.get("type") == "box":
                        joined = "".join(c.get("text", "") for c in node["contents"])
                        print(f"  {joined}")
                for btn in b["footer"]["contents"]:
                    act = btn["action"]
                    print(f"  [ボタン] {act['label']} → {act['uri']}")
            print("\n========== dry-run のため配信していません ==========")
            return
        send([msg], args.date)
        return

    # --- text モード（フォールバック用） ---
    if not args.file:
        raise SystemExit("ERROR: --mode text には --file が必要です")
    text = Path(args.file).read_text(encoding="utf-8").strip()
    text = re.sub(r"^@@DATA$.*?^@@END$", "", text, flags=re.M | re.S).strip()
    messages = pack_messages(text)
    n = len(messages)
    print(f"分割後のメッセージ数: {n}通")
    for i, m in enumerate(messages, 1):
        print(f"  メッセージ{i}: {len(m)}文字")
    print("※ 実際の消費通数は「メッセージ数 × 友だち数」（無料プランは月200通まで）")

    if args.dry_run:
        for i, m in enumerate(messages, 1):
            print(f"\n========== メッセージ {i}/{n}（{len(m)}文字） ==========")
            print(m)
        print("\n========== dry-run のため配信していません ==========")
        return
    send([{"type": "text", "text": m} for m in messages], args.date)


if __name__ == "__main__":
    main()
