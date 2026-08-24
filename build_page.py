#!/usr/bin/env python3
"""週次レポートの要約ページ（GitHub Pages 用HTML）を生成する。

- docs/YYYY-MM-DD.html : その週のレポート詳細ページ。各社に #c1〜#c5 のアンカーを持ち、
  LINEカードの「要約を見る」からその企業の位置に直接飛べる。各社に元記事リンクを併記。
- docs/index.html      : バックナンバー一覧（docs/*.html を走査して毎回作り直す）

標準ライブラリのみ使用。
"""

import argparse
import html
import json
import re
from pathlib import Path

DIVIDER_RE = re.compile(r"^─{3,}\s*$")

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1a1d21;--muted:#5b6470;--line:#e3e6ea;--accent:#06c755}
@media(prefers-color-scheme:dark){
:root{--bg:#15181c;--card:#1e2227;--fg:#e8eaed;--muted:#9aa4b0;--line:#2c3138;--accent:#06c755}}
*{box-sizing:border-box}
body{margin:0;padding:0 16px 56px;background:var(--bg);color:var(--fg);
font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
line-height:1.75;font-size:16px;-webkit-text-size-adjust:100%}
.wrap{max-width:720px;margin:0 auto}
header.top{padding:28px 0 18px;border-bottom:2px solid var(--accent);margin-bottom:24px}
h1{font-size:19px;margin:0 0 6px;line-height:1.5}
.date{color:var(--muted);font-size:13px;margin:0}
.lead{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px;margin:0 0 24px;font-size:14px;color:var(--muted)}
section.co{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 18px 16px;margin:0 0 18px;scroll-margin-top:16px}
section.co h2{font-size:17px;margin:0 0 2px;line-height:1.5}
.num{display:inline-block;background:var(--accent);color:#fff;font-size:12px;font-weight:700;
border-radius:6px;padding:2px 8px;margin-right:8px;vertical-align:2px}
.tag{color:var(--muted);font-size:13px;margin:0 0 12px}
ul{margin:0;padding:0;list-style:none}
li{position:relative;padding-left:14px;margin-bottom:7px;font-size:14.5px}
li:before{content:"";position:absolute;left:0;top:11px;width:5px;height:5px;
border-radius:50%;background:var(--accent)}
li b{font-weight:700}
.src{display:inline-block;margin-top:12px;font-size:13.5px;color:var(--accent);
text-decoration:none;border:1px solid var(--accent);border-radius:8px;padding:7px 14px}
.summary{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);
border-radius:12px;padding:16px 18px;margin:26px 0 0;font-size:14.5px}
.summary h2{font-size:15px;margin:0 0 8px}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:12.5px}
footer a{color:var(--accent)}
.backs{margin:0;padding:0;list-style:none}
.backs li{padding-left:0;margin-bottom:0;border-bottom:1px solid var(--line)}
.backs li:before{display:none}
.backs a{display:block;padding:15px 2px;color:var(--fg);text-decoration:none;font-size:15px}
.backs .vol{color:var(--muted);font-size:12.5px;display:block;margin-top:2px}
"""

PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title><style>{css}</style></head>
<body><div class="wrap">
<header class="top"><h1>米国先行サービス → 日本落とし込み</h1>
<p class="date">{date_jp}　vol.{vol}</p></header>
{lead}
{sections}
{summary}
<footer>数字は各社の公表資料・報道に基づきます。確認できなかったものは「未公開」と記載しています。<br>
<a href="./index.html">← バックナンバー一覧</a></footer>
</div></body></html>
"""

INDEX = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>米国先行サービス → 日本落とし込み｜バックナンバー</title><style>{css}</style></head>
<body><div class="wrap">
<header class="top"><h1>米国先行サービス → 日本落とし込み</h1>
<p class="date">バックナンバー</p></header>
<ul class="backs">{items}</ul>
</div></body></html>
"""


def parse_body(body: str):
    """本文を企業ブロック（【n】…）と全体所感（◆…）に分解する。

    区切りは罫線ではなく「【n】」見出しで判定するので、罫線が無くても壊れない。
    """
    rest = "\n".join(body.strip().splitlines()[1:])
    rest = re.sub(r"^─{3,}[ \t]*$\n?", "", rest, flags=re.M)
    m = re.search(r"^◆.*$", rest, re.M)
    summary = rest[m.start():].strip() if m else ""
    area = rest[:m.start()] if m else rest
    companies = [b.strip() for b in re.split(r"^(?=【\d+】)", area, flags=re.M)
                 if b.strip().startswith("【")]
    return companies, summary


def render_company(idx: int, block: str, meta: dict) -> str:
    """1社分のセクションHTML。1行目=見出し、以降の「・」行=箇条書き。"""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    heading = re.sub(r"^【\d+】", "", lines[0])
    items = []
    for ln in lines[1:]:
        ln = ln.lstrip("・").strip()
        if not ln:
            continue
        # 「日本の同種：…」のようなラベルを太字にする
        m = re.match(r"^([^：:]{2,12})[：:](.*)$", ln)
        if m:
            items.append(f"<li><b>{html.escape(m.group(1))}</b>：{html.escape(m.group(2).strip())}</li>")
        else:
            items.append(f"<li>{html.escape(ln)}</li>")

    src = ""
    url = (meta or {}).get("source_url", "")
    if url.startswith("https://") or url.startswith("http://"):
        src = f'<a class="src" href="{html.escape(url)}" target="_blank" rel="noopener">元記事を読む →</a>'

    tag = (meta or {}).get("tagline", "")
    tag_html = f'<p class="tag">{html.escape(tag)}</p>' if tag else ""

    return (f'<section class="co" id="c{idx}">'
            f'<h2><span class="num">{idx}</span>{html.escape(heading)}</h2>'
            f'{tag_html}<ul>{"".join(items)}</ul>{src}</section>')


def build_index(docs_dir: Path) -> None:
    """docs/ 配下の日付ページを走査してバックナンバー一覧を作り直す。"""
    pages = sorted(
        (p for p in docs_dir.glob("*.html") if re.match(r"^\d{4}-\d{2}-\d{2}\.html$", p.name)),
        key=lambda p: p.name, reverse=True)
    items = []
    for p in pages:
        m = re.search(r'<p class="date">(.+?)　vol\.(\d+)</p>', p.read_text(encoding="utf-8"))
        date_jp, vol = (m.group(1), m.group(2)) if m else (p.stem, "?")
        items.append(f'<li><a href="./{p.name}">{html.escape(date_jp)} 号'
                     f'<span class="vol">vol.{html.escape(vol)}</span></a></li>')
    if not items:
        items.append('<li><a href="#">まだレポートがありません</a></li>')
    (docs_dir / "index.html").write_text(
        INDEX.format(css=CSS, items="".join(items)), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="週次レポートの要約ページを生成")
    ap.add_argument("--body", required=True, help="本文ファイル（archive/YYYY-MM-DD.md）")
    ap.add_argument("--data", required=True, help="カード用データJSON")
    ap.add_argument("--out-dir", required=True, help="出力先（docs/）")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--date-jp", required=True, help="YYYY/M/D")
    ap.add_argument("--vol", required=True)
    args = ap.parse_args()

    body = Path(args.body).read_text(encoding="utf-8")
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    companies_meta = data.get("companies", [])
    blocks, summary_block = parse_body(body)

    if len(blocks) != len(companies_meta):
        raise SystemExit(f"ERROR: 本文の企業数({len(blocks)})とデータの件数({len(companies_meta)})が不一致")

    sections = "".join(render_company(i, b, companies_meta[i - 1])
                       for i, b in enumerate(blocks, 1))

    lead = ""
    short = data.get("summary_short", "").strip()
    if short:
        lead = f'<p class="lead">{html.escape(short)}</p>'

    summary_html = ""
    if summary_block:
        lines = [ln.strip() for ln in summary_block.splitlines() if ln.strip()]
        text = "<br>".join(html.escape(ln) for ln in lines[1:]) or html.escape(summary_block)
        summary_html = f'<div class="summary"><h2>全体所感</h2>{text}</div>'

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    page = PAGE.format(
        title=f"米国先行サービス→日本落とし込み {args.date_jp} vol.{args.vol}",
        css=CSS, date_jp=html.escape(args.date_jp), vol=html.escape(str(args.vol)),
        lead=lead, sections=sections, summary=summary_html)
    (out_dir / f"{args.date}.html").write_text(page, encoding="utf-8")
    build_index(out_dir)
    print(f"ページを生成しました: {out_dir / (args.date + '.html')}")


if __name__ == "__main__":
    main()
