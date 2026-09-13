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
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 16384},
    }

    body = None
    for model in models:
        status, body = api(key, f"models/{model}:generateContent", payload)
        if status == 200:
            print(f"使用モデル: {model}", file=sys.stderr)
            break
        # 429=無料枠が無い/使い切った, 404=そのモデルでは使えない → 次の候補へ
        if status in (404, 429):
            reason = "無料枠なし/上限到達" if status == 429 else "利用不可"
            print(f"{model}: {reason} (HTTP {status}) のため次の候補を試します", file=sys.stderr)
            continue
        die(f"生成に失敗 (HTTP {status}): {body}")
    else:
        die("試した全モデルで生成できませんでした（無料枠の上限に達している可能性があります）。"
            f"候補: {', '.join(models)} / 最後の応答: {body}")

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
