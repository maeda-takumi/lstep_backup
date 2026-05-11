# LSTEP chat-history scraper


LSTEP Manager のログイン後画面から、友だち一覧のリンクと表示名を SQLite に保存し、保存済みユーザのトークページからテキストチャット履歴を取得する Selenium スクリプトです。
## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Chrome を利用します。Selenium Manager により、通常は ChromeDriver を手動配置する必要はありません。

## 実行方法

### 1. 友だち一覧を取得する
```bash
python scripts/scrape_lstep.py
```

1. Chrome で `https://manager.linestep.net/account/login` が開きます。
2. 手動でログインし、友だちリスト画面まで移動します。
3. 友だちリストの `/line/detail/` リンクを検出すると、Enter などの確認なしで全ページの友だち href を SQLite に保存します。
4. ページャー（`nav[aria-label="Pagination"]` のページ番号ボタン、または「次」ボタン）で次ページへ進み、全ページを自動取得します。


友だち一覧取得前に従来どおり Enter 確認を入れたい場合は `--confirm-before-friends` を指定してください。

### 2. 保存済みユーザのチャットを取得する

```bash
python scripts/scrape_lstep_chats.py
```

1. `users.href` に保存された URL（例: `https://manager.linestep.net/line/detail/246185610`）の最後のパス要素を member ID として取得します。
2. member ID を `https://manager.linestep.net/line/visual?member={member}` に差し込み、トークページへ遷移します。
3. チャット欄を上方向へスクロールし、過去チャットが追加読み込みされるまで待機します。
4. これ以上過去チャットが読み込まれなくなるまで繰り返し、表示されたテキストメッセージを `chat_messages` に保存します。
既定の DB ファイルは `lstep_chat_history.db` です。

少量で動作確認する場合:

```bash
python scripts/scrape_lstep.py --max-pages 1
python scripts/scrape_lstep_chats.py --max-users 1
```

ログイン済みの Chrome プロファイルを使い、ログインページの待機を省略する場合:

```bash
python scripts/scrape_lstep_chats.py \
  --user-data-dir .chrome-lstep-profile \
  --skip-login-wait
```
## 保存テーブル

### `users`

| カラム | 内容 |
| --- | --- |
| `id` | オートナンバーの主キー |
| `name` | 友だち一覧リンクから取得した表示名 |
| `href` | 友だち詳細ページの URL |
| `created_at` | 初回保存日時（UTC） |
| `updated_at` | 更新日時（UTC） |

### `chat_messages`

| カラム | 内容 |
| --- | --- |
| `id` | オートナンバーの主キー |
| `user_id` | `users.id` への外部キー |
| `message_text` | チャットメッセージ本文 |
| `sent_at` | DOM から取得した日付・時刻文字列（取れない場合は `NULL`） |
| `sender` | 送信者情報（自分側は `me`、相手側は `you`） |
| `created_at` | 保存日時（UTC） |

`chat_messages` は `(user_id, message_text, sent_at, sender)` の重複を保存しないため、同じユーザに対して再実行しても同一メッセージは追加されません。
## DOM に合わせた調整

LSTEP の画面構造はアカウントや更新で変わる可能性があります。想定外のリンクやメッセージを拾う場合は、CSS セレクタを指定して絞り込んでください。

友だち一覧側:

```bash
python scripts/scrape_lstep.py \
  --friend-link-selector "a[href*='/line/detail/']" \
  --friend-href-keywords "/line/detail/" \
  --next-selector "nav[aria-label='Pagination'] button"
```

チャット取得側:

```bash

python scripts/scrape_lstep_chats.py \
  --talk-url-template "https://manager.linestep.net/line/visual?member={member}" \
  --chat-scroll-selector "div.tw-min-h-0.tw-flex-1.tw-overflow-y-scroll" \
  --message-selector "div[data-message-id]" \
  --message-text-selector "p.text-content" \
  --message-time-selector ".tw-text-xs.tw-text-n-soft" \
  --scroll-wait-seconds 1.0
  ```

友だち一覧取得前に従来どおり Enter 確認を入れたい場合は `--confirm-before-friends` を指定してください。
## 注意

- ログイン情報はコードや DB に保存しません。
- LSTEP の利用規約・robots・アクセス頻度に配慮して利用してください。

- 初回は `--max-pages 1` と `--max-users 1` などで少量取得し、セレクタが正しいことを確認してください。