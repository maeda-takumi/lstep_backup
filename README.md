# LSTEP chat-history scraper

LSTEP Manager のログイン後画面から、友だち一覧のリンクと表示名、各友だちページで表示されるチャット履歴を SQLite に保存する Selenium スクリプトです。

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Chrome を利用します。Selenium Manager により、通常は ChromeDriver を手動配置する必要はありません。

## 実行方法

```bash
python scripts/scrape_lstep.py
```

1. Chrome で `https://manager.linestep.net/account/login` が開きます。
2. 手動でログインし、友だちリスト画面まで移動します。
3. ターミナルで Enter を押すと、全ページの友だちリンクを SQLite に保存します。
4. 続けて Enter を押すと、保存済み友だち URL を順番に開いて、表示されるチャット履歴を保存します。

既定の DB ファイルは `lstep_chat_history.db` です。

## 保存テーブル

### `users`

| カラム | 内容 |
| --- | --- |
| `id` | オートナンバーの主キー |
| `name` | 友だち一覧リンクから取得した表示名 |
| `href` | 友だち詳細・チャット画面の URL |
| `created_at` | 初回保存日時（UTC） |
| `updated_at` | 更新日時（UTC） |

### `chat_messages`

| カラム | 内容 |
| --- | --- |
| `id` | オートナンバーの主キー |
| `user_id` | `users.id` への外部キー |
| `message_text` | チャットメッセージ本文 |
| `sender` | DOM 属性から取れた送信者情報（取れない場合は `NULL`） |
| `sent_at` | DOM 属性から取れた送信日時（取れない場合は `NULL`） |
| `created_at` | 保存日時（UTC） |

## DOM に合わせた調整

LSTEP の画面構造はアカウントや更新で変わる可能性があります。想定外のリンクやメッセージを拾う場合は、CSS セレクタを指定して絞り込んでください。

```bash
python scripts/scrape_lstep.py \
  --friend-link-selector "a[href*='/friends/']" \
  --friend-href-keywords "friends" \
  --next-selector ".pagination .next:not(.disabled)" \
  --chat-message-selector ".message-row"
```

友だち一覧だけを先に確認する場合:

```bash
python scripts/scrape_lstep.py --skip-chat --max-pages 1
```

## 注意

- ログイン情報はコードや DB に保存しません。
- LSTEP の利用規約・robots・アクセス頻度に配慮して利用してください。
- 初回は `--max-pages 1 --skip-chat` などで少量取得し、セレクタが正しいことを確認してください。
