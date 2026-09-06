# D1 + R2 連携セットアップ手順

- メタデータ(タイトル・ジャンル/タグ・日付・発表者・コメント・PDFキー)は全て Cloudflare D1 から取得します。
- PDFバイナリは Cloudflare R2 に保管します。D1にはR2オブジェクトキー(`pdf_key`)のみ保存します。
- HTML内にハードコードはありません(`templates/index.html` は `{{ presentation.* }}` と `{{ presentation.pdf_url }}` のみ)。

## 1. D1 作成

```bash
npm i -g wrangler
wrangler login
wrangler d1 create lt-db
# 表示された database_id を wrangler.toml に貼り付け
```

## 2. テーブル + 初期データ投入

```bash
wrangler d1 execute lt-db --file=./schema.sql
wrangler d1 execute lt-db --file=./seed.sql
# 本番DBなら --remote を付ける
wrangler d1 execute lt-db --remote --file=./schema.sql
wrangler d1 execute lt-db --remote --file=./seed.sql
```

## 3. R2 作成 + PDF アップロード

```bash
wrangler r2 bucket create lt-share-slides
```

公開バケット運用(推奨・簡単)の場合:

```bash
wrangler r2 bucket update lt-share-slides --enable-public-access
# またはカスタムドメイン接続 (例: https://slides.example.com)
# 公開URL (r2.dev またはカスタムドメイン) を .env の R2_PUBLIC_BASE_URL に設定
```

R2キー命名規則: `Slides/{開催回数字}/{発表順}.pdf`
(例: 第1回の1番目の発表 → `Slides/1/1.pdf`)
D1の`pdf_key`にはこのフルキーを格納します。
新規アップロード時は `POST /api/presentations/<id>/pdf` の `key` にこの形式で指定してください。

既存の `data/*.pdf` をR2へ移行:

```bash
pip install -r requirements.txt
# .env に R2_ENDPOINT_URL / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME を設定
python scripts/upload_to_r2.py
# 個別指定: python scripts/upload_to_r2.py data/3.pdf 3.pdf
```

R2 APIトークン作成: ダッシュボード -> R2 -> Manage R2 API Tokens -> Object Read & Write
- `R2_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com` 形式

## 4. APIトークン発行 (D1用)

https://dash.cloudflare.com/profile/api-tokens で作成
- 権限: Account / D1 / Edit

## 5. 環境変数設定

```bash
cp .env.example .env
# .env を編集
```

| 変数 | 説明 |
|---|---|
| CLOUDFLARE_ACCOUNT_ID | CFダッシュボードの Account ID |
| CLOUDFLARE_DATABASE_ID | `wrangler d1 list` で確認できるID |
| CLOUDFLARE_D1_API_TOKEN | 上記トークン |
| LOCAL_DB_PATH | D1未設定時のSQLiteパス(既定 `./dev.db`) |
| R2_BUCKET_NAME | R2バケット名 (例: `lt-share-slides`) |
| R2_PUBLIC_BASE_URL | 公開バケットのベースURL (例: `https://pub-xxx.r2.dev`)。設定すれば署名不要 |
| R2_ENDPOINT_URL | 非公開運用時のみ。`https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY | 非公開運用時のみ。署名付きURL発行+アップロードAPIに使用 |
| R2_PRESIGN_EXPIRES | 署名付きURL有効秒数(既定3600) |

配信モードは自動判定 (`/health` の `r2_mode` で確認):
- `public`: `R2_PUBLIC_BASE_URL` 設定時。公開URL直結
- `presigned`: R2資格情報設定時。S3署名付きURL発行
- `local`: 未設定時。`data/` フォルダ配信 (開発用フォールバック)

## 6. 起動

```bash
pip install -r requirements.txt
# ローカルのみで試す場合(D1未設定時):
python scripts/init_local_db.py
python app.py
```

## 仕様

- `GET /` : `?id=` があれば該当ID、無ければ最新1件を `presentations` から取得。タグは `presentation_tags + tags` から取得。回数・日付は `sessions` をJOINして取得。PDFは `{{ presentation.pdf_url }}` (R2) で表示。
- `GET /list` : 全件を開催日降順で取得。`?session_id=` で開催回絞り込み可。
- `GET /api/sessions` : 開催回一覧(発表件数付き)。`POST /api/sessions` で開催回登録(`{name, event_date}`)。
- `GET /api/sessions/<id>` : 開催回+紐づく発表一覧。
- `PUT /api/presentations/<id>/session` : 発表の紐づく開催回を変更(`{session_id}`)。
- `GET /slides/<id>` : R2のPDFへ302リダイレクト (共有リンク用)。
- `GET /api/presentations` / `GET /api/presentations/<id>` : JSON API (`pdf_key`, `pdf_url` 付き)。
- `POST /api/presentations/<id>/pdf` : PDFをR2へアップロードしD1の`pdf_key`を更新 (form-data `file`, 任意 `key`)。R2資格情報が必要。
- D1接続は `https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query` のREST API経由。
- D1環境変数が全て揃っている場合のみD1を使用し、欠けている場合はローカルSQLite(`dev.db`)にフォールバックします。
- R2未設定時は開発用に `data/` フォルダ配信にフォールバックします。本番では `R2_PUBLIC_BASE_URL` または署名付きURLを使用し、`data/` への依存はありません。
