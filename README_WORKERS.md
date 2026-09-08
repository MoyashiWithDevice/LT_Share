# LT Share を Cloudflare Workers で公開する手順

Flask(`app.py`)は Workers 上で動かないため、`worker.js` に移植済みです。
D1・R2 はバインディング経由で直接利用します (REST / boto3 不要)。

## 構成

| ファイル | 説明 |
|---|---|
| `worker.js` | Worker 本体 (`app.py` の移植版。全ルート対応) |
| `wrangler.toml` | Workers 設定 (D1=`DB`/R2=`SLIDES`/Assets/`R2_PUBLIC_BASE_URL`) |
| `public/static/*` | 公開CSS (`static/` のコピー。`[assets]` から配信) |
| `package.json` | `npm run dev` / `npm run deploy` 用 |
| `.dev.vars.example` | ローカル開発用変数の雛形 |

## 初回デプロイ

```bash
# 1. 管理パスワードをシークレット登録 (.env の ADMIN_PASSWORD と同じ値推奨)
npx wrangler secret put ADMIN_PASSWORD

# 2. デプロイ
npm run deploy
# または
npx wrangler deploy
```

デプロイ後に表示される `https://lt-share.<subdomain>.workers.dev` にアクセス。
動作確認: `/health` が `{"d1_enabled":true,...}` を返せばOK。

## ローカル開発

```bash
cp .dev.vars.example .dev.vars   # ADMIN_PASSWORD 等を設定
npm run dev                       # http://localhost:8787
```

> 注意: `wrangler dev` は `.env` も読み込みますが、Worker が使うのは
> D1/R2 バインディングと `.dev.vars` / シークレットです。
> Flask 用の `CLOUDFLARE_*` / `R2_ACCESS_KEY_*` は Workers では不要です。

## よく使うコマンド

```bash
# D1 スキーマ投入 (本番。seed.sql は実行不要)
npm run db:schema
# 個別SQL
npx wrangler d1 execute lt-db --remote --command="SELECT * FROM sessions;"

# PDF を R2 に置く (命名規則: Slides/{開催回数字}/{発表順}.pdf)
npx wrangler r2 object put lt-pdf/Slides/1/1.pdf --file=./data/3.pdf --content-type=application/pdf
```

## ルート一覧 (Flask と同等)

- `GET /` (`?id=` あり/なし) / `GET /list` (`?session_id=` 可)
- `GET /slides/<id>` → R2 PDF へ302 / `GET /r2/<key>` → R2 直接配信
- `GET /api/sessions`, `POST /api/sessions`, `GET /api/sessions/<id>`
- `GET /api/presentations`, `GET /api/presentations/<id>`
- `PUT /api/presentations/<id>/session`, `POST /api/presentations/<id>/pdf`
- `GET/POST /admin/login`, `/admin/logout`, `/admin/`, `/admin/presentations`,
  `/admin/presentations/<id>/edit`, 発表の create/update/delete, 開催回の create/update/delete
- `GET /health`

## 仕組みの要点

- D1 は `env.DB.prepare(...).bind(...).all()/first()/run()` でアクセス。
- PDF は `R2_PUBLIC_BASE_URL` があれば公開URLへリダイレクト、なければ `/r2/<key>` で `env.SLIDES.get()` 配信。
- 管理画面認証は `ADMIN_PASSWORD` の SHA-256 を `lt_admin` Cookie に保存 (Flask セッションの代替)。
- フラッシュメッセージは `?flash=&kind=` クエリで受け渡し。
- Flask 版 (`app.py` / `templates/` / `requirements.txt`) は残してありますが、Workers 本番では使いません。
