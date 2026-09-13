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
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT = 600  # 検索グラウンディング付きは時間がかかる


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


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


def pick_model(key: str) -> str:
    """使えるモデルの中から、検索グラウンディングに向いたものを自動で選ぶ。"""
    if os.environ.get("GEMINI_MODEL"):
        return os.environ["GEMINI_MODEL"]
    status, body = api(key, "models?pageSize=200")
    if status != 200:
        die(f"モデル一覧の取得に失敗 (HTTP {status}): {body}")

    def score(name: str):
        base = name.split("/")[-1]
        if "generateContent" not in supported.get(name, []):
            return None
        # 用途違いのモデルを除外
        if re.search(r"embedding|aqa|vision|tts|image|audio|native|live|robotics", base):
            return None
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", base)
        if not m:
            return None
        ver = float(m.group(1))
        # flash を優先（無料枠が大きい）。lite は品質が落ちるので後回し
        kind = 2 if ("flash" in base and "lite" not in base) else (1 if "pro" in base else 0)
        stable = 0 if re.search(r"preview|exp|thinking", base) else 1
        return (stable, ver, kind)

    supported = {m["name"]: m.get("supportedGenerationMethods", [])
                 for m in body.get("models", [])}
    best, best_score = None, None
    for name in supported:
        s = score(name)
        if s and (best_score is None or s > best_score):
            best, best_score = name.split("/")[-1], s
    if not best:
        die("利用可能なGeminiモデルが見つかりませんでした")
    return best


def main() -> None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        die("GEMINI_API_KEY が設定されていません")

    prompt = sys.stdin.read()
    if not prompt.strip():
        die("プロンプトが空です")

    model = pick_model(key)
    print(f"使用モデル: {model}", file=sys.stderr)

    status, body = api(key, f"models/{model}:generateContent", {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 16384},
    })
    if status != 200:
        die(f"生成に失敗 (HTTP {status}): {body}")

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
