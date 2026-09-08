# LT Share を Cloudflare Python Workers で公開する手順

FlaskをPython Workers化済み。エントリは `src/worker.py` (`wsgi.entrypoint`)。
D1・R2 はバインディング経由で直接利用します (REST / boto3 / sqlite 不要)。

## 構成

| ファイル | 説明 |
|---|---|
| `src/worker.py` | Worker 本体 (Flask+WSGI。全ルート対応。単一正本) |
| `templates/` | Jinja正本。`src/worker.py` に埋め込み併用 (下記参照) |
| `public/static/*` | 公開CSS (`static/` のコピー。`[assets]` から配信) |
| `wrangler.toml` | Workers 設定 (`python_workers`/`src/worker.py`/D1=`DB`/R2=`SLIDES`/Assets) |
| `pyproject.toml` | Python依存 (`flask`, `workers-runtime-sdk`) |
| `package.json` | `npm run dev` / `npm run deploy` 用 (pywrangler委譲) |
| `.dev.vars.example` | ローカル開発用変数の雛形 |

## 初回デプロイ

```bash
# 1. シークレット登録
npx wrangler secret put ADMIN_PASSWORD
npx wrangler secret put SECRET_KEY

# 2. デプロイ
npm run deploy
# または
uv run pywrangler deploy
```

デプロイ後に表示される `https://lt-share.<subdomain>.workers.dev` にアクセス。
動作確認: `/health` が `{"d1_enabled":true,"runtime":"python-workers",...}` を返せばOK。

## Cloudflare Builds (自動デプロイ) の設定

> ⚠ Deploy command は必ず `uv run pywrangler deploy` にすること。
> 既定の `npx wrangler deploy` では Python 依存がバンドルされず
> `ModuleNotFoundError: No module named 'flask'` で失敗する
> (素の wrangler は `requirements.txt` を見ても vendoring しない。
> `pywrangler deploy` が先に `python_modules/` へ vendor してから wrangler に委譲する)。

## ローカル開発

```bash
cp .dev.vars.example .dev.vars   # ADMIN_PASSWORD/SECRET_KEY 等を設定
uv sync
npm run dev                       # == uv run pywrangler dev (http://localhost:8787)
# D1ローカルにスキーマ投入
npm run db:schema:local
```

> 注意: Worker が使うのは D1/R2 バインディングと `.dev.vars` / シークレットです。
> 変更は `src/worker.py` + `templates/` に入れること。

## テンプレート変更時の必須手順

Python Workers本番は `templates/` が同梱されないため、`src/worker.py` 内の
`_EMBEDDED_TEMPLATES` (DictLoader) がフォールバックになります。

```bash
# templates/*.html 編集後に必ず実行 (src/worker.py の埋め込みを更新)
uv run python scripts/sync_worker_templates.py
```

CSSも同様に同期:

```bash
npm run sync:assets   # static/*.css -> public/static/
```

## よく使うコマンド

```bash
# D1 スキーマ投入 (本番)
npm run db:schema
# 個別SQL
uv run pywrangler d1 execute lt-db --remote --command="SELECT * FROM sessions;"

# PDF を R2 に置く (命名規則: Slides/{開催回数字}/{発表順}.pdf)
npx wrangler r2 object put lt-pdf/Slides/1/1.pdf --file=./data/3.pdf --content-type=application/pdf
```

## ルート一覧

- `GET /` (`?id=` あり/なし) / `GET /list` (`?session_id=`/`?q=`/`?has_slide=` 可)
- `GET /<回>/<順>` (前後記事footer付き) / `GET /slides/<id>` → 資料へ302 / `GET /r2/<key>` → R2配信(public時は公開URLへ302)
- `GET /api/sessions`, `POST /api/sessions`, `GET /api/sessions/<id>`
- `GET /api/presentations`, `GET /api/presentations/<id>`
- `PUT /api/presentations/<id>/session`, `POST /api/presentations/<id>/pdf` (R2バインディングput), `DELETE .../pdf`
- `GET/POST /admin/login`, `/admin/logout`, `/admin/`, `/admin/presentations`,
  `/admin/presentations/<id>/edit`, 発表の create/update/delete, 開催回の create/update/delete
- `GET /health`

## 仕組みの要点

- D1 は `request.environ["workers.env"].DB.prepare(...).bind(...).all()/first()/run()` + `run_sync` でアクセス。
- PDF は `R2_PUBLIC_BASE_URL` があれば公開URLへリダイレクト、なければ `/r2/<key>` で `env.SLIDES.get()` 配信。署名発行・`data/` 配信は廃止。
- 管理画面認証は Flask `session` + `SECRET_KEY`。`ADMIN_PASSWORD` 未設定時は保護なし (開発用)。
