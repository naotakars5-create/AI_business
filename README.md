# 米国先行サービス → 日本落とし込みレポート 週次LINE配信

米国で先行しているサービスを毎週リサーチし、日本への落とし込み候補レポート（6社）を
LINE公式アカウントの友だちに一斉配信するパイプライン。

## 構成

| ファイル | 役割 |
|---|---|
| `run.sh` | メインパイプライン（cron から起動） |
| `prompt.md` | `claude -p` に渡すリサーチ指示テンプレート |
| `send_line.py` | LINE broadcast 配信（Python 標準ライブラリのみ） |
| `excluded.json` | 紹介済み企業リスト（毎回6社が自動追記され、以後除外される） |
| `setup_cron.sh` | 毎週月曜 8:00 JST の cron ジョブ登録 |
| `.env` | `LINE_CHANNEL_ACCESS_TOKEN` のみ（gitignore 済み） |
| `archive/YYYY-MM-DD.md` | 生成レポートの保存先（gitignore 済み） |
| `logs/YYYY-MM-DD.log` | 実行ログ。7日より古いものは自動削除（gitignore 済み） |

## セットアップ（運用するマシンで）

前提: `python3`（3.8+）、`claude` CLI（ログイン済み）、`crontab` が使えること。

```bash
git clone https://github.com/naotakars5-create/AI_business
cd AI_business
cp .env.example .env
# .env に LINE Developers で発行したチャネルアクセストークン（長期）を貼る

./run.sh --dry-run   # 配信されず、分割後メッセージがターミナルに表示される
./setup_cron.sh      # 内容に問題がなければ cron を登録（既存ジョブは保全される）
```

## 動作の要点

- 実行フロー: excluded.json 読込 → `claude -p` でリサーチ・本文生成 → 検証 →
  `archive/` 保存 → 6社を excluded.json に追記 → `send_line.py` で配信 → ログ記録。
  **途中で失敗したら配信せず**、理由を `logs/` に残して終了する。
- 二重配信防止: 配信成功時に `logs/sent-YYYY-MM-DD` マーカーを作成。同日の再実行は
  マーカーがあれば何もせず、archive だけある（=配信のみ失敗した）場合は同じ本文・
  同じ `X-Line-Retry-Key`（日付から決定的に導出したUUID、LINE側で24時間有効）で
  配信だけ再試行する。
- メッセージ分割: テキスト1通5,000文字・1リクエスト5メッセージの上限に対し、
  企業ブロック（【1】〜【6】）の境目でのみ分割。複数通のときは先頭に「（2/3）」を付ける。
- 通数の目安: 毎回「分割後のメッセージ数」を表示する。実際の消費通数は
  **メッセージ数 × 友だち数**。無料プラン（コミュニケーションプラン）は月200通まで。
  例: 友だち20人に3通 → 60通消費。配信後に友だち数・今月の消費通数も取得して表示する
  （取得できない場合はスキップ）。

## 注意

- `.env` の中身はログ・標準出力に一切出さない設計。`git add .env` も不要（gitignore 済み）。
- レポート内で確認できなかった数字は「未公開」と表記される（プロンプトで推測値を禁止）。
- cron はこのリポジトリを clone した実機で `setup_cron.sh` により登録する。
  マシンのタイムゾーンが JST 以外でも月曜 8:00 JST 相当に変換して登録される。
