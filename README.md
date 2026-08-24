# 米国先行サービス → 日本落とし込みレポート 週次LINE配信

米国で先行しているサービスを毎週リサーチし、日本への落とし込み候補（3〜5社）を
LINE公式アカウントの友だちに**カード形式（Flexカルーセル）**で一斉配信する。
カードをタップすると、要約ページ（GitHub Pages）と元記事の両方が開ける。

## 構成

| ファイル | 役割 |
|---|---|
| `run.sh` | メインパイプライン（cron から起動） |
| `prompt.md` | `claude -p` に渡すリサーチ指示テンプレート |
| `build_page.py` | 要約ページ（`docs/YYYY-MM-DD.html`）とバックナンバー一覧を生成 |
| `send_line.py` | LINE broadcast 配信（Python 標準ライブラリのみ） |
| `excluded.json` | 紹介済み企業リスト（毎回自動追記され、以後除外される） |
| `setup_cron.sh` | 毎週月曜 8:00 JST の cron ジョブ登録 |
| `.env` | `LINE_CHANNEL_ACCESS_TOKEN` のみ（gitignore 済み） |
| `docs/` | 公開ページ（GitHub Pages の公開元。コミット対象） |
| `archive/` | 生成レポート本文とカード用JSON（gitignore 済み） |
| `logs/` | 実行ログ。7日より古いものは自動削除（gitignore 済み） |

## セットアップ（運用するマシンで）

前提: `python3`（3.8+）、`claude` CLI（ログイン済み）、`git`、`curl`、`crontab`。

```bash
git clone https://github.com/naotakars5-create/AI_business
cd AI_business
cp .env.example .env
# .env に LINE Developers で発行したチャネルアクセストークン（長期）を貼る

./run.sh --dry-run   # 配信・pushせず、カード内容とページを手元で確認
./setup_cron.sh      # 問題なければ cron を登録（既存ジョブは保全される）
```

### GitHub Pages を有効にする（初回だけ・カードのリンク先になる）

GitHub の該当リポジトリで **Settings → Pages → Source: Deploy from a branch** を選び、
**Branch: `claude/sharp-tesla-sy1qob` / フォルダ: `/docs`** を指定して Save。
公開URLは `https://naotakars5-create.github.io/AI_business/` になる。
`run.sh` はこのURLを git remote から自動導出する（`PAGE_BASE_URL` 環境変数で上書き可）。

## 動作の要点

- 実行フロー: excluded.json 読込 → `claude -p` でリサーチ・生成 → **検証** →
  `archive/` 保存 → excluded.json 追記 → 要約ページ生成 → **git push で公開** →
  **公開反映を確認（最大5分待機）** → LINEにカード配信 → ログ記録。
  途中で失敗したら**配信せず**、理由を `logs/` に残して終了する。
  ページが公開されるまで配信しないので、カードのリンクが切れることはない。
- カード枚数: その週に紹介する企業数（3〜5社）＋「レポート全文」カード1枚。
  各カードのボタンは「要約を見る」（要約ページの該当企業へ直接ジャンプ）と
  「元記事を読む」（出典記事）の2つ。
- **消費通数: 1回の配信につき1通 ×友だち数**。カルーセルは何枚でも1メッセージ扱いのため、
  無料プラン（月200通）でも友だち200人までは月1回配信で収まる。実行のたびに通数を表示する。
- 二重配信防止: 配信成功時に `logs/sent-YYYY-MM-DD` マーカーを作成。同日の再実行は
  マーカーがあれば何もせず、生成済みで未配信なら**再生成せず**同じ内容・同じ
  `X-Line-Retry-Key`（日付から導出したUUID、LINE側で24時間有効）で配信だけ再試行する。

## 注意

- `.env` の中身はログ・標準出力に一切出さない設計。
- レポート内で確認できなかった数字は「未公開」と表記される（プロンプトで推測値を禁止）。
- `run.sh` は毎回 `docs/` と `excluded.json` をコミットして push する。
  運用マシンに push できる git 認証（SSH鍵または認証済みHTTPS）が必要。
- `send_line.py --mode text` で従来のテキスト配信もできる（フォールバック用）。
