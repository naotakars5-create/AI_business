#!/usr/bin/env python3
"""claude が生成したレポートを検証し、本文とカード用JSONに分けて保存する。

罫線（─────）はモデルが書き落とすことがあるため、企業ブロックの区切りは
「【n】」の見出しで判定する。保存する本文は罫線を入れ直して正規化するので、
ページ生成・テキスト配信のどちらも常に同じ構造を前提にできる。

使い方: validate_report.py <生成物> <本文の保存先> <JSONの保存先> <YYYY/M/D> <vol>
検証NGなら stderr に理由を出して終了コード1。標準出力には企業名を " | " 区切りで返す。
"""

import json
import re
import sys

MIN_COMPANIES, MAX_COMPANIES = 3, 5
DIVIDER = "─────"


def die(msg: str) -> None:
    print(f"検証NG: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    raw_path, archive_path, data_path, date_jp, vol = sys.argv[1:6]
    raw = open(raw_path, encoding="utf-8").read().strip()
    if not raw:
        die("生成結果が空")

    header = raw.splitlines()[0].strip()
    expect = f"【米国先行サービス→日本落とし込み】{date_jp} vol.{vol}"
    if header != expect:
        die(f"1行目が不正: {header!r}（期待: {expect!r}）")

    # --- カード用JSON ---
    m = re.search(r"^@@DATA$\s*(.+?)\s*^@@END$", raw, re.M | re.S)
    if not m:
        die("@@DATA〜@@END ブロックが見つからない")
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        die(f"カード用JSONが壊れている: {e}")

    companies = data.get("companies")
    if not isinstance(companies, list) or not MIN_COMPANIES <= len(companies) <= MAX_COMPANIES:
        n = len(companies) if isinstance(companies, list) else "不正"
        die(f"companies は{MIN_COMPANIES}〜{MAX_COMPANIES}件である必要がある（{n}）")
    for i, c in enumerate(companies, 1):
        for key in ("name", "tagline", "card", "source_url"):
            if not c.get(key):
                die(f"companies[{i}] に {key} がない")
        if not isinstance(c["card"], list) or not c["card"]:
            die(f"companies[{i}] の card が配列でない")
        if not str(c["source_url"]).startswith(("http://", "https://")):
            die(f"companies[{i}] の source_url がURLでない: {c['source_url']!r}")

    # --- 本文を企業ブロックと全体所感に分解する ---
    body_raw = raw[:m.start()].strip()
    rest = "\n".join(body_raw.splitlines()[1:])
    rest = re.sub(r"^─{3,}[ \t]*$\n?", "", rest, flags=re.M)  # 既存の罫線を一旦落とす

    m_sum = re.search(r"^◆.*$", rest, re.M)
    summary = rest[m_sum.start():].strip() if m_sum else ""
    company_area = rest[:m_sum.start()] if m_sum else rest

    blocks = [b.strip() for b in re.split(r"^(?=【\d+】)", company_area, flags=re.M) if b.strip()]
    if blocks and not blocks[0].startswith("【"):
        die(f"【1】より前に不明なテキストがある: {blocks[0][:40]!r}")
    if len(blocks) != len(companies):
        die(f"本文の企業ブロック数({len(blocks)})とcompanies件数({len(companies)})が不一致")
    for i, b in enumerate(blocks, 1):
        if not b.startswith(f"【{i}】"):
            die(f"{i}番目のブロックが【{i}】で始まっていない: {b[:20]!r}")
    if not summary:
        die("◆全体所感 が見つからない")
    if "**" in body_raw or re.search(r"^#{1,6} ", body_raw, re.M):
        die("本文にMarkdown記法（** や #）が残っている")

    # --- 罫線を入れ直して正規化した本文を保存 ---
    body = header + "\n" + f"\n{DIVIDER}\n".join(blocks) + f"\n{DIVIDER}\n" + summary + "\n"
    open(archive_path, "w", encoding="utf-8").write(body)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(" | ".join(c["name"] for c in companies))


if __name__ == "__main__":
    main()
