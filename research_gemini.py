#!/usr/bin/env python3
"""Gemini API（Google検索グラウンディング付き）でレポート本文を生成する。

標準入力からプロンプトを読み、生成結果を標準出力に書く。claude CLI の代替。
無料枠（Google AI Studio のAPIキー）で動く。標準ライブラリのみ使用。

APIの形式（2026-09-13 に公式ドキュメント・リファレンスで確認）:
  POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
  ヘッダ: x-goog-api-key: <APIキー>, Content-Type: application/json
  ボディ: {"contents":[{"parts":[{"text":"..."}]}], "tools":[{"google_search":{}}]}
  https://ai.google.dev/gemini-api/docs/google-search

モデル名は変わりうるので暗記で決め打ちせず、ListModels で実際に使えるものを調べて選ぶ。
GEMINI_MODEL 環境変数で明示指定も可能。

2段構えで動く:
  1. Google検索グラウンディング付きで生成を試す
  2. 全モデルが429（無料枠なし）なら、こちらでRSSから実際の記事を取得して
     本文に添え、グラウンディング無しで生成する。出典URLは取得した記事のものに限定され、
     モデルがURLを創作できないぶん、むしろ確実になる
"""

import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT = 600  # 検索グラウンディング付きは時間がかかる
TRANSIENT = (500, 502, 503, 504)  # Gemini側の一時的な不調。待てば直る


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


FEEDS = [
    ("TechCrunch スタートアップ", "https://techcrunch.com/category/startups/feed/"),
    ("Crunchbase News", "https://news.crunchbase.com/feed/"),
    ("Product Hunt", "https://www.producthunt.com/feed"),
    ("Y Combinator Launches", "https://www.ycombinator.com/launches/feed.xml"),
]
UA = "Mozilla/5.0 (compatible; weekly-report-bot)"


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def fetch_feed(url: str, limit: int = 15) -> list:
    """RSS/Atom を取得して記事一覧にする。失敗しても例外を投げず空を返す。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as res:
            root = ET.fromstring(res.read())
    except Exception as e:
        print(f"フィード取得失敗 {url}: {e}", file=sys.stderr)
        return []

    items = []
    # RSS
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        desc = strip_tags(node.findtext("description") or "")
        date = (node.findtext("pubDate") or "").strip()
        if title and link:
            items.append({"title": title, "url": link, "date": date, "summary": desc[:400]})
    # Atom
    if not items:
        ns = "{http://www.w3.org/2005/Atom}"
        for node in root.iter(f"{ns}entry"):
            title = (node.findtext(f"{ns}title") or "").strip()
            link_el = node.find(f"{ns}link")
            link = (link_el.get("href") if link_el is not None else "") or ""
            desc = strip_tags(node.findtext(f"{ns}summary") or node.findtext(f"{ns}content") or "")
            date = (node.findtext(f"{ns}updated") or node.findtext(f"{ns}published") or "").strip()
            if title and link:
                items.append({"title": title, "url": link, "date": date, "summary": desc[:400]})
    return items[:limit]


def build_sources_block() -> str:
    """各フィードから記事を集めてプロンプトに添える文字列を作る。"""
    lines, total = [], 0
    for name, url in FEEDS:
        items = fetch_feed(url)
        if not items:
            continue
        lines.append(f"\n## {name}")
        for it in items:
            lines.append(f"- タイトル: {it['title']}")
            lines.append(f"  URL: {it['url']}")
            if it["date"]:
                lines.append(f"  日付: {it['date']}")
            if it["summary"]:
                lines.append(f"  概要: {it['summary']}")
            total += 1
    if total == 0:
        return ""
    print(f"記事を{total}件取得しました", file=sys.stderr)
    return "\n".join(lines)


def api(key: str, path: str, payload=None):
    url = f"{API_BASE}/{path}"
    headers = {"x-goog-api-key": key}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        # APIキーがURLやログに漏れないよう、ボディのみ返す
        return e.code, body


def candidate_models(key: str) -> list:
    """使えるモデルを新しい順に並べて返す。

    最新モデルには無料枠が無いことがある（429 RESOURCE_EXHAUSTED）。
    そのため1つに決め打ちせず、新しい順に試して最初に通ったものを使う。
    """
    if os.environ.get("GEMINI_MODEL"):
        return [os.environ["GEMINI_MODEL"]]
    status, body = api(key, "models?pageSize=200")
    if status != 200:
        die(f"モデル一覧の取得に失敗 (HTTP {status}): {body}")

    supported = {m["name"]: m.get("supportedGenerationMethods", [])
                 for m in body.get("models", [])}

    scored = []
    for name, methods in supported.items():
        base = name.split("/")[-1]
        if "generateContent" not in methods:
            continue
        # 用途違いのモデルを除外
        if re.search(r"embedding|aqa|vision|tts|image|audio|native|live|robotics", base):
            continue
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", base)
        if not m:
            continue
        ver = float(m.group(1))
        # flash を優先（無料枠が大きい）。lite は品質が落ちるので後回し
        kind = 2 if ("flash" in base and "lite" not in base) else (1 if "pro" in base else 0)
        stable = 0 if re.search(r"preview|exp|thinking", base) else 1
        scored.append(((stable, ver, kind), base))

    if not scored:
        die("利用可能なGeminiモデルが見つかりませんでした")
    scored.sort(key=lambda x: x[0], reverse=True)
    # 同名の重複を除きつつ上位から最大8件試す
    seen, ordered = set(), []
    for _, base in scored:
        if base not in seen:
            seen.add(base)
            ordered.append(base)
    return ordered[:8]


def main() -> None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        die("GEMINI_API_KEY が設定されていません")

    prompt = sys.stdin.read()
    if not prompt.strip():
        die("プロンプトが空です")

    models = candidate_models(key)
    config = {"temperature": 0.4, "maxOutputTokens": 16384}

    def attempt(text: str, grounded: bool):
        """候補モデルを順に試す。(成功したbody, 最後のstatus, 最後のbody) を返す。"""
        payload = {"contents": [{"parts": [{"text": text}]}],
                   "generationConfig": config}
        if grounded:
            payload["tools"] = [{"google_search": {}}]
        last_status, last_body = None, None
        for model in models:
            # 5xx はGemini側の一時的な混雑。少し待って同じモデルで再試行する
            for wait in (0, 20, 60):
                if wait:
                    print(f"{model}: 混雑のため{wait}秒待って再試行します", file=sys.stderr)
                    time.sleep(wait)
                status, body = api(key, f"models/{model}:generateContent", payload)
                last_status, last_body = status, body
                if status not in TRANSIENT:
                    break
            if status == 200:
                print(f"使用モデル: {model}"
                      f"（{'Google検索連携あり' if grounded else '取得済み記事から生成'}）",
                      file=sys.stderr)
                return body, status, body
            # 429=無料枠が無い/使い切った, 404=使えない, 5xx=混雑が続く → 次の候補へ
            if status in (404, 429) or status in TRANSIENT:
                reason = {429: "無料枠なし/上限到達", 404: "利用不可"}.get(status, "混雑（5xx）")
                print(f"{model}: {reason} (HTTP {status})", file=sys.stderr)
                continue
            die(f"生成に失敗 (HTTP {status}): {body}")
        return None, last_status, last_body

    # 1段目: Google検索グラウンディング付き
    body, status, last_body = attempt(prompt, grounded=True)

    # 2段目: 検索連携に無料枠が無い場合、こちらで記事を取得して渡す
    if body is None:
        print("検索連携では生成できませんでした。記事を自分で取得して再試行します",
              file=sys.stderr)
        sources = build_sources_block()
        if not sources:
            die("記事の取得にも失敗しました（ネットワークを確認してください）。"
                f"検索連携の最後の応答: {last_body}")
        augmented = (prompt + "\n\n# 実際に取得した最新記事一覧（この中から選ぶこと）\n"
                     + sources +
                     "\n\n# 重要な追加ルール\n"
                     "- 上の一覧に無い企業は取り上げないこと\n"
                     "- source_url は上の一覧に書かれたURLをそのまま使うこと。URLを創作しない\n"
                     "- 一覧の情報だけでは調達額などが分からない場合は「未公開」と書くこと\n")
        body, status, last_body = attempt(augmented, grounded=False)
        if body is None:
            die("記事を渡した再試行でも生成できませんでした（APIキーの無料枠が"
                f"利用できない可能性があります）。最後の応答: {last_body}")

    candidates = body.get("candidates") or []
    if not candidates:
        die(f"応答に candidates がありません: {json.dumps(body, ensure_ascii=False)[:500]}")
    reason = candidates[0].get("finishReason", "")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        die(f"生成テキストが空です (finishReason={reason})")
    if reason not in ("STOP", "", None):
        # MAX_TOKENS等で途中で切れた場合、半端な本文を後段に渡さない
        die(f"生成が途中で終了しました (finishReason={reason})")
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
