"""LT Share - メタデータは D1、PDF実体は Cloudflare R2 から取得する Flask アプリ.

データモデル:
  sessions: 開催回マスタ (第1回 / 開催日)。日付はここで一元管理。
  presentations: 各発表。session_id で sessions に紐づく。日付はJOINで取得。
"""
import logging
import os
import sqlite3

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------- D1 設定 ----------
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CF_DATABASE_ID = os.getenv("CLOUDFLARE_DATABASE_ID", "")
CF_API_TOKEN = os.getenv("CLOUDFLARE_D1_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")
LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", "./dev.db")

D1_ENABLED = bool(CF_ACCOUNT_ID and CF_DATABASE_ID and CF_API_TOKEN)
D1_API_BASE = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    f"/d1/database/{CF_DATABASE_ID}"
    if D1_ENABLED
    else ""
)

# ---------- R2 設定 ----------
# PDFバイナリはR2に保管し、D1にはオブジェクトキー(pdf_key)のみ持つ。
# 公開バケット運用: R2_PUBLIC_BASE_URL を設定 (例: https://pub-xxx.r2.dev / https://slides.example.com)
# 非公開バケット運用: R2_ENDPOINT_URL + ACCESS_KEY + SECRET で署名付きURLを発行
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "")  # 例: https://<ACCOUNT_ID>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_PRESIGN_EXPIRES = int(os.getenv("R2_PRESIGN_EXPIRES", "3600") or "3600")

_s3_client = None


def r2_mode():
    """public / presigned / local のいずれかを返す。"""
    if R2_PUBLIC_BASE_URL:
        return "public"
    if R2_ENDPOINT_URL and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME:
        return "presigned"
    return "local"


def _s3():
    """R2(S3互換)クライアントを遅延生成・再利用。boto3未導入時は例外。"""
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    import boto3  # 遅延import: public/local 運用では不要

    _s3_client = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    )
    return _s3_client


def pdf_url_for(pdf_key):
    """R2オブジェクトキー -> ブラウザが表示できるURL。

    - publicモード: 公開URL直結 (署名不要・高速)
    - presignedモード: S3署名付きURLを発行
    - localモード: 旧 data/ フォルダ配信 (開発用フォールバック)
    """
    if not pdf_key:
        return ""
    key = pdf_key.lstrip("/")
    if R2_PUBLIC_BASE_URL:
        return f"{R2_PUBLIC_BASE_URL}/{key}"
    if r2_mode() == "presigned":
        try:
            return _s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": R2_BUCKET_NAME, "Key": key},
                ExpiresIn=R2_PRESIGN_EXPIRES,
            )
        except Exception:
            log.exception("R2署名付きURL発行エラー")
            raise
    # 開発用フォールバック
    return f"/data/{key}"


def d1_query(sql, params=None, timeout=10):
    """Cloudflare D1 REST API でクエリを1件実行し、rows(list[dict]) を返す."""
    url = f"{D1_API_BASE}/query"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"sql": sql, "params": params or []}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"D1 query failed: {data.get('errors')}")
    # result は [{results:[...], meta:{...}}] 形式
    return data["result"][0].get("results", [])


def local_query(sql, params=None):
    """D1未設定時のローカル開発用: SQLite から取得."""
    if not os.path.exists(LOCAL_DB_PATH):
        raise RuntimeError(
            f"LOCAL_DB_PATH({LOCAL_DB_PATH}) が存在しません。"
            " python scripts/init_local_db.py を実行するか、D1環境変数を設定してください。"
        )
    con = sqlite3.connect(LOCAL_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, params or [])
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def db_query(sql, params=None):
    """D1優先、未設定時はローカルSQLite。"""
    if D1_ENABLED:
        # D1 は `?` プレースホルダに対応
        return d1_query(sql, params)
    return local_query(sql, params)


def db_execute(sql, params=None):
    """INSERT/UPDATE用。D1時はクエリAPI、ローカル時はcommit付きで実行。"""
    if D1_ENABLED:
        d1_query(sql, params)
        return
    con = sqlite3.connect(LOCAL_DB_PATH)
    try:
        con.execute(sql, params or [])
        con.commit()
    finally:
        con.close()


# ---------- Sessions ----------


def row_to_session(row):
    event_date = (row.get("event_date") or "").strip()
    return {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "event_date": event_date,
        "event_date_display": event_date.replace("-", ".") if event_date else "",
        "presentation_count": row.get("presentation_count", 0) or 0,
    }


def get_sessions():
    """開催回一覧 (発表件数付き)。"""
    rows = db_query(
        """
        SELECT s.*, COUNT(p.id) AS presentation_count
        FROM sessions s
        LEFT JOIN presentations p ON p.session_id = s.id
        GROUP BY s.id ORDER BY s.event_date DESC, s.id DESC
        """
    )
    return [row_to_session(r) for r in rows]


def get_session(session_id):
    """開催回1件 + 紐づく発表一覧。"""
    rows = db_query("SELECT * FROM sessions WHERE id = ?", [session_id])
    if not rows:
        return None
    session = row_to_session(rows[0])
    session["presentations"] = get_all_presentations(session_id=session_id)
    return session


# ---------- Presentations ----------

_PRES_SELECT = (
    "SELECT p.*, s.id AS s_id, s.name AS s_name, s.event_date AS s_event_date "
    "FROM presentations p LEFT JOIN sessions s ON s.id = p.session_id"
)


def row_to_presentation(row):
    """DB行 -> テンプレート用dict。回数・日付は sessions JOIN 結果から取得。"""
    event_date = ((row.get("s_event_date") or "").strip())
    pdf_key = (row.get("pdf_key") or "").strip().lstrip("/")
    try:
        pdf_url = pdf_url_for(pdf_key)
    except Exception:
        pdf_url = f"/data/{pdf_key}" if pdf_key else ""
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "presenter_name": row.get("presenter_name") or "",
        "grade": row.get("grade") or "",
        "session_id": row.get("s_id"),
        "session": {
            "id": row.get("s_id"),
            "name": row.get("s_name") or "",
            "event_date": event_date,
            "event_date_display": event_date.replace("-", ".") if event_date else "",
        },
        "session_name": row.get("s_name") or "",
        "event_date": event_date,
        "event_date_display": event_date.replace("-", ".") if event_date else "",
        "comment": row.get("comment") or "",
        "pdf_key": pdf_key,
        "pdf_url": pdf_url,
        "pdf_file": pdf_key,
        "tags": [],  # 後で付与
    }


def fetch_tags_map(presentation_ids):
    """presentation_id -> [tag名] のマップを取得."""
    if not presentation_ids:
        return {}
    placeholders = ",".join("?" for _ in presentation_ids)
    rows = db_query(
        f"""
        SELECT pt.presentation_id AS pid, t.name AS name
        FROM presentation_tags pt
        JOIN tags t ON t.id = pt.tag_id
        WHERE pt.presentation_id IN ({placeholders})
        ORDER BY t.id
        """,
        list(presentation_ids),
    )
    tag_map = {pid: [] for pid in presentation_ids}
    for r in rows:
        tag_map.setdefault(r["pid"], []).append(r["name"])
    return tag_map


def _attach_tags(items):
    tag_map = fetch_tags_map([p["id"] for p in items])
    for p in items:
        p["tags"] = tag_map.get(p["id"], [])
    return items


def get_presentation(presentation_id):
    rows = db_query(f"{_PRES_SELECT} WHERE p.id = ?", [presentation_id])
    if not rows:
        return None
    return _attach_tags([row_to_presentation(rows[0])])[0]


def get_latest_presentation():
    rows = db_query(f"{_PRES_SELECT} ORDER BY p.id DESC LIMIT 1")
    if not rows:
        return None
    return _attach_tags([row_to_presentation(rows[0])])[0]


def get_all_presentations(session_id=None):
    if session_id is not None:
        rows = db_query(
            f"{_PRES_SELECT} WHERE p.session_id = ? ORDER BY s.event_date DESC, p.id DESC",
            [session_id],
        )
    else:
        rows = db_query(f"{_PRES_SELECT} ORDER BY s.event_date DESC, p.id DESC")
    return _attach_tags([row_to_presentation(r) for r in rows])


# ---------- Routes ----------


@app.route("/")
def index():
    """?id= 指定があればその発表、無ければ最新をD1から取得して表示。PDFはR2のURL。"""
    pid = request.args.get("id", type=int)
    try:
        presentation = get_presentation(pid) if pid else get_latest_presentation()
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    if presentation is None:
        abort(404, description="発表データが見つかりません")
    return render_template("index.html", presentation=presentation)


@app.route("/list")
def list_page():
    """?session_id= で開催回絞り込み可。"""
    session_id = request.args.get("session_id", type=int)
    try:
        presentations = get_all_presentations(session_id=session_id)
        sessions = get_sessions()
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    current_session = None
    if session_id is not None:
        current_session = next((s for s in sessions if s["id"] == session_id), None)
    return render_template(
        "list.html",
        presentations=presentations,
        sessions=sessions,
        current_session=current_session,
    )


@app.route("/slides/<int:presentation_id>")
def slide_redirect(presentation_id):
    """R2のPDFへリダイレクト (共有リンク用)。"""
    try:
        p = get_presentation(presentation_id)
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    if p is None or not p.get("pdf_url"):
        abort(404, description="スライドが見つかりません")
    return redirect(p["pdf_url"], code=302)


@app.route("/api/sessions")
def api_sessions():
    try:
        return jsonify(get_sessions())
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        return jsonify({"error": str(e)}), 502


@app.route("/api/sessions/<int:session_id>")
def api_session(session_id):
    try:
        s = get_session(session_id)
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        return jsonify({"error": str(e)}), 502
    if s is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(s)


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    """開催回を登録。{name: '第2回', event_date: '2026-10-03'} 同名は既存を返す。"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    event_date = (data.get("event_date") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        existing = db_query("SELECT * FROM sessions WHERE name = ?", [name])
        if existing:
            s = row_to_session(existing[0])
            s["created"] = False
            return jsonify(s), 200
        db_execute(
            "INSERT INTO sessions (name, event_date) VALUES (?, ?)", [name, event_date]
        )
        s = row_to_session(db_query("SELECT * FROM sessions WHERE name = ?", [name])[0])
        s["created"] = True
        return jsonify(s), 201
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        return jsonify({"error": str(e)}), 502


@app.route("/api/presentations")
def api_presentations():
    session_id = request.args.get("session_id", type=int)
    try:
        return jsonify(get_all_presentations(session_id=session_id))
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        return jsonify({"error": str(e)}), 502


@app.route("/api/presentations/<int:presentation_id>")
def api_presentation(presentation_id):
    try:
        p = get_presentation(presentation_id)
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        return jsonify({"error": str(e)}), 502
    if p is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(p)


@app.route("/api/presentations/<int:presentation_id>/session", methods=["PUT"])
def api_update_presentation_session(presentation_id):
    """発表の紐づく開催回を変更。{session_id: 2}"""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    if session_id is None:
        return jsonify({"error": "session_id is required"}), 400
    try:
        if get_presentation(presentation_id) is None:
            return jsonify({"error": "presentation not found"}), 404
        target = db_query("SELECT * FROM sessions WHERE id = ?", [session_id])
        if not target:
            return jsonify({"error": "session not found"}), 404
        db_execute(
            "UPDATE presentations SET session_id = ? WHERE id = ?",
            [session_id, presentation_id],
        )
        return jsonify(get_presentation(presentation_id))
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        return jsonify({"error": str(e)}), 502


@app.route("/api/presentations/<int:presentation_id>/pdf", methods=["POST"])
def api_upload_pdf(presentation_id):
    """PDFをR2へアップロードし、D1のpdf_keyを更新する。

    form-data: file=<pdf>, 任意 key=<オブジェクトキー> (省略時は既存キー or <id>.pdf)
    R2資格情報 (ENDPOINT/KEY/SECRET/BUCKET) が必要。
    """
    if r2_mode() != "presigned":
        return jsonify({"error": "R2 upload credentials not configured"}), 503
    if "file" not in request.files:
        return jsonify({"error": "file is required"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    try:
        current = get_presentation(presentation_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502
    if current is None:
        return jsonify({"error": "not found"}), 404
    key = (request.form.get("key") or current.get("pdf_key") or f"{presentation_id}.pdf").lstrip("/")
    try:
        _s3().upload_fileobj(
            f.stream, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "application/pdf"}
        )
        db_execute("UPDATE presentations SET pdf_key = ? WHERE id = ?", [key, presentation_id])
    except Exception as e:  # noqa: BLE001
        log.exception("R2アップロードエラー")
        return jsonify({"error": str(e)}), 502
    return jsonify({"id": presentation_id, "pdf_key": key, "pdf_url": pdf_url_for(key)})


@app.route("/health")
def health():
    return jsonify(
        {"d1_enabled": D1_ENABLED, "r2_mode": r2_mode(), "r2_bucket": R2_BUCKET_NAME}
    )


@app.route("/data/<path:filename>")
def data_file(filename):
    """開発用フォールバック: data/ フォルダ内の PDF を配信。本番はR2を使用。"""
    return send_from_directory("data", filename)


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
