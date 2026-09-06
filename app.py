"""LT Share - メタデータは D1、PDF実体は Cloudflare R2 から取得する Flask アプリ.

データモデル:
  sessions: 開催回マスタ (第1回 / 開催日)。日付はここで一元管理。
  presentations: 各発表。session_id で sessions に紐づく。日付はJOINで取得。
"""
import logging
import os
import re
import sqlite3
from functools import wraps

import requests
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_from_directory,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------- Admin 認証設定 ----------
# ADMIN_PASSWORD を .env に設定すると管理画面にパスワードが必要になる。
# 未設定時は開発用としてパスワードなしで入れる (警告表示あり)。
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def is_admin():
    """管理者としてログイン済みか。パスワード未設定時は常にTrue (開発用)。"""
    if not ADMIN_PASSWORD:
        return True
    return bool(session.get("admin"))


def admin_required(view):
    """管理画面用デコレータ。未ログイン時はログイン画面へリダイレクト。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash("管理者ログインが必要です", "error")
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_api_required(view):
    """書き込み系API用。未ログイン時はJSON 401を返す (パスワード設定時のみ)。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if ADMIN_PASSWORD and not session.get("admin"):
            return jsonify({"error": "admin login required"}), 401
        return view(*args, **kwargs)
    return wrapped

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


# 学年はDBに数字のみ ("1"〜"4" または空) で保存し、表示時に "Grade N" を付ける。
GRADE_CHOICES = ("1", "2", "3", "4")


def normalize_grade_input(raw):
    """登録用: '', '1'〜'4'、旧形式 'Grade N' を受け付け、数字 or '' を返す。受付不可は None。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if text == "":
        return ""
    if text in GRADE_CHOICES:
        return text
    m = re.fullmatch(r"[Gg][Rr][Aa][Dd][Ee]\s*([1-4])", text)
    if m:
        return m.group(1)
    return None


def grade_display(grade):
    """表示用: '3' -> 'Grade 3'、'' -> ''。旧形式の混在時も正規化して表示。"""
    g = normalize_grade_input(grade)
    if g:
        return f"Grade {g}"
    return ""


# PDFキー (R2オブジェクトキー) は Slides/{開催回}/{発表順}.pdf の形式で自動生成。
# "Slides" プレフィックスは固定で、登録画面では入力させない。
PDF_KEY_PREFIX = "Slides"


def session_round_number(sess):
    """開催回名 (例: 第12回) から回数数字を抽出。数字がなければIDを使う。"""
    m = re.search(r"(\d+)", (sess.get("name") or ""))
    if m:
        return m.group(1)
    return str(sess.get("id"))


def build_pdf_key(sess, order):
    """R2オブジェクトキーを自動生成。形式: Slides/{開催回}/{発表順}.pdf"""
    return f"{PDF_KEY_PREFIX}/{session_round_number(sess)}/{int(order)}.pdf"


def pdf_order_from_key(pdf_key):
    """既存キー (例: Slides/1/3.pdf) から発表順 ('3') を抽出。解析できなければ ''。"""
    if not pdf_key:
        return ""
    name = pdf_key.strip().lstrip("/").rsplit("/", 1)[-1]
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    if name.isdigit() and int(name) >= 1:
        return str(int(name))
    return ""


def row_to_presentation(row):
    """DB行 -> テンプレート用dict。回数・日付は sessions JOIN 結果から取得。"""
    event_date = ((row.get("s_event_date") or "").strip())
    pdf_key = (row.get("pdf_key") or "").strip().lstrip("/")
    try:
        pdf_url = pdf_url_for(pdf_key)
    except Exception:
        pdf_url = f"/data/{pdf_key}" if pdf_key else ""
    _grade = normalize_grade_input(row.get("grade"))
    grade = _grade if _grade is not None else ""
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "presenter_name": row.get("presenter_name") or "",
        "grade": grade,
        "grade_display": grade_display(grade),
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
        "presentation_order": pdf_order_from_key(pdf_key),
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


def parse_tag_input(raw):
    """カンマ/スペース/# 区切りのタグ入力を正規化。重複除去・順序保持。"""
    if not raw:
        return []
    text = raw.replace("、", ",").replace("#", ",").replace("　", " ")
    names = []
    for chunk in text.replace(",", " ").split():
        name = chunk.strip()
        if name and name not in names:
            names.append(name)
    return names


def set_presentation_tags(presentation_id, tag_names):
    """発表のタグ紐づけを置き換える。存在しないタグ名は自動作成。"""
    names = [n.strip() for n in (tag_names or []) if (n or "").strip()]
    # 重複除去 (順序保持)
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    db_execute("DELETE FROM presentation_tags WHERE presentation_id = ?", [presentation_id])
    for name in uniq:
        db_execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", [name])
        rows = db_query("SELECT id FROM tags WHERE name = ?", [name])
        if rows:
            db_execute(
                "INSERT OR IGNORE INTO presentation_tags (presentation_id, tag_id) VALUES (?, ?)",
                [presentation_id, rows[0]["id"]],
            )


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
@admin_api_required
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
@admin_api_required
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
@admin_api_required
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


# ---------- Admin (管理画面) ----------


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """管理者ログイン。ADMIN_PASSWORD未設定時はそのままダッシュボードへ。"""
    if not ADMIN_PASSWORD:
        return redirect(url_for("admin_dashboard"))
    if session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        next_url = request.form.get("next") or url_for("admin_dashboard")
        if password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(next_url)
        error = "パスワードが違います"
    return render_template("admin_login.html", error=error, next=request.args.get("next", "/admin/"))


@app.route("/admin/logout", methods=["GET", "POST"])
def admin_logout():
    session.pop("admin", None)
    flash("ログアウトしました", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin/")
@app.route("/admin")
@admin_required
def admin_dashboard():
    """DB登録情報の一覧 + 追加/編集/削除フォーム。"""
    try:
        presentations = get_all_presentations()
        sessions = get_sessions()
    except Exception as e:  # noqa: BLE001
        log.exception("Admin取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    return render_template(
        "admin.html",
        presentations=presentations,
        sessions=sessions,
        password_set=bool(ADMIN_PASSWORD),
    )


@app.route("/admin/presentations/")
@app.route("/admin/presentations")
@admin_required
def admin_presentations():
    """発表の一覧 (発表回・発表順・タイトル)。行を選択して編集ページへ遷移。"""
    try:
        presentations = get_all_presentations()
        sessions = get_sessions()
    except Exception as e:  # noqa: BLE001
        log.exception("Admin取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    # 開催回が新しい順 → 発表順が小さい順に並べ替え
    rank = {s["id"]: i for i, s in enumerate(sessions)}
    def _key(p):
        o = p.get("presentation_order") or ""
        return (
            rank.get(p.get("session_id"), len(rank)),
            int(o) if o.isdigit() else 10**9,
            p.get("id"),
        )
    presentations.sort(key=_key)
    return render_template(
        "admin_presentations.html",
        presentations=presentations,
        password_set=bool(ADMIN_PASSWORD),
    )


@app.route("/admin/presentations/<int:presentation_id>/edit")
@admin_required
def admin_edit_presentation(presentation_id):
    """発表の編集ページ。"""
    try:
        presentation = get_presentation(presentation_id)
        sessions = get_sessions()
    except Exception as e:  # noqa: BLE001
        log.exception("Admin取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    if presentation is None:
        abort(404, description="発表データが見つかりません")
    return render_template(
        "admin_presentation_edit.html",
        presentation=presentation,
        sessions=sessions,
        password_set=bool(ADMIN_PASSWORD),
    )


@app.route("/admin/presentations/create", methods=["POST"])
@admin_required
def admin_create_presentation():
    title = (request.form.get("title") or "").strip()
    presenter_name = (request.form.get("presenter_name") or "").strip()
    grade = normalize_grade_input(request.form.get("grade"))
    session_id = request.form.get("session_id", type=int)
    order_raw = (request.form.get("presentation_order") or "").strip()
    comment = (request.form.get("comment") or "").strip()
    tag_names = parse_tag_input(request.form.get("tags", ""))
    if not title:
        flash("タイトルは必須です", "error")
        return redirect(url_for("admin_dashboard") + "#presentations")
    if grade is None:
        flash("学年は1〜4の数字で指定してください", "error")
        return redirect(url_for("admin_dashboard") + "#presentations")
    if session_id is None:
        flash("開催回を選択してください（PDFキー生成に必要です）", "error")
        return redirect(url_for("admin_dashboard") + "#presentations")
    if not order_raw.isdigit() or int(order_raw) < 1:
        flash("発表順は1以上の数字で指定してください", "error")
        return redirect(url_for("admin_dashboard") + "#presentations")
    try:
        sess_rows = db_query("SELECT * FROM sessions WHERE id = ?", [session_id])
        if not sess_rows:
            flash("指定の開催回が存在しません", "error")
            return redirect(url_for("admin_dashboard") + "#presentations")
        # PDFキーは開催回＋発表順から自動生成 (例: 第1回＋3 → Slides/1/3.pdf)
        pdf_key = build_pdf_key(sess_rows[0], int(order_raw))
        db_execute(
            "INSERT INTO presentations (title, presenter_name, grade, session_id, comment, pdf_key)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [title, presenter_name, grade, session_id, comment, pdf_key],
        )
        new_id = db_query("SELECT id FROM presentations ORDER BY id DESC LIMIT 1")[0]["id"]
        set_presentation_tags(new_id, tag_names)
        flash(f"発表「{title}」を追加しました", "success")
    except Exception as e:  # noqa: BLE001
        log.exception("Admin追加エラー")
        flash(f"追加に失敗しました: {e}", "error")
    return redirect(url_for("admin_dashboard") + "#presentations")


@app.route("/admin/presentations/<int:presentation_id>/update", methods=["POST"])
@admin_required
def admin_update_presentation(presentation_id):
    edit_url = url_for("admin_edit_presentation", presentation_id=presentation_id)
    try:
        current = get_presentation(presentation_id)
    except Exception as e:  # noqa: BLE001
        flash(f"取得に失敗しました: {e}", "error")
        return redirect(url_for("admin_presentations"))
    if current is None:
        flash("発表が見つかりません", "error")
        return redirect(url_for("admin_presentations"))
    title = (request.form.get("title") or "").strip()
    presenter_name = (request.form.get("presenter_name") or "").strip()
    grade = normalize_grade_input(request.form.get("grade"))
    raw_sid = (request.form.get("session_id") or "").strip()
    session_id = int(raw_sid) if raw_sid else None
    comment = (request.form.get("comment") or "").strip()
    order_raw = (request.form.get("presentation_order") or "").strip()
    tag_names = parse_tag_input(request.form.get("tags", ""))
    if not title:
        flash("タイトルは必須です", "error")
        return redirect(edit_url)
    if grade is None:
        flash("学年は1〜4の数字で指定してください", "error")
        return redirect(edit_url)
    if not order_raw.isdigit() or int(order_raw) < 1:
        flash("発表順は1以上の数字で指定してください", "error")
        return redirect(edit_url)
    if session_id is None:
        flash("開催回を選択してください", "error")
        return redirect(edit_url)
    try:
        sess_rows = db_query("SELECT * FROM sessions WHERE id = ?", [session_id])
        if not sess_rows:
            flash("指定の開催回が存在しません", "error")
            return redirect(edit_url)
        # PDFキーは開催回＋発表順から自動生成（開催回・発表順に未設定は許可しない）
        pdf_key = build_pdf_key(sess_rows[0], int(order_raw))
        db_execute(
            "UPDATE presentations SET title=?, presenter_name=?, grade=?, session_id=?, comment=?, pdf_key=?"
            " WHERE id=?",
            [title, presenter_name, grade, session_id, comment, pdf_key, presentation_id],
        )
        set_presentation_tags(presentation_id, tag_names)
        flash(f"発表 #{presentation_id} を更新しました", "success")
    except Exception as e:  # noqa: BLE001
        log.exception("Admin更新エラー")
        flash(f"更新に失敗しました: {e}", "error")
    return redirect(edit_url)


@app.route("/admin/presentations/<int:presentation_id>/delete", methods=["POST"])
@admin_required
def admin_delete_presentation(presentation_id):
    try:
        db_execute("DELETE FROM presentation_tags WHERE presentation_id = ?", [presentation_id])
        db_execute("DELETE FROM presentations WHERE id = ?", [presentation_id])
        flash(f"発表 #{presentation_id} を削除しました", "success")
    except Exception as e:  # noqa: BLE001
        log.exception("Admin削除エラー")
        flash(f"削除に失敗しました: {e}", "error")
    return redirect(url_for("admin_presentations"))


@app.route("/admin/sessions/create", methods=["POST"])
@admin_required
def admin_create_session():
    name = (request.form.get("name") or "").strip()
    event_date = (request.form.get("event_date") or "").strip()
    if not name:
        flash("開催回名は必須です", "error")
        return redirect(url_for("admin_dashboard") + "#sessions")
    try:
        db_execute("INSERT INTO sessions (name, event_date) VALUES (?, ?)", [name, event_date])
        flash(f"開催回「{name}」を追加しました", "success")
    except Exception as e:  # noqa: BLE001
        log.exception("Admin追加エラー")
        flash(f"追加に失敗しました (同名の開催回がある可能性があります): {e}", "error")
    return redirect(url_for("admin_dashboard") + "#sessions")


@app.route("/admin/sessions/<int:session_id>/update", methods=["POST"])
@admin_required
def admin_update_session(session_id):
    name = (request.form.get("name") or "").strip()
    event_date = (request.form.get("event_date") or "").strip()
    if not name:
        flash("開催回名は必須です", "error")
        return redirect(url_for("admin_dashboard") + "#sessions")
    try:
        db_execute(
            "UPDATE sessions SET name=?, event_date=? WHERE id=?", [name, event_date, session_id]
        )
        flash(f"開催回 #{session_id} を更新しました", "success")
    except Exception as e:  # noqa: BLE001
        log.exception("Admin更新エラー")
        flash(f"更新に失敗しました: {e}", "error")
    return redirect(url_for("admin_dashboard") + "#sessions")


@app.route("/admin/sessions/<int:session_id>/delete", methods=["POST"])
@admin_required
def admin_delete_session(session_id):
    try:
        # 開催回の未設定は許可しないため、紐づく発表がある開催回は削除不可
        linked = db_query("SELECT COUNT(*) AS c FROM presentations WHERE session_id = ?", [session_id])
        if linked and linked[0].get("c"):
            flash(f"開催回 #{session_id} は発表が紐づいているため削除できません (発表を先に移動・削除してください)", "error")
            return redirect(url_for("admin_dashboard") + "#sessions")
        db_execute("DELETE FROM sessions WHERE id = ?", [session_id])
        flash(f"開催回 #{session_id} を削除しました", "success")
    except Exception as e:  # noqa: BLE001
        log.exception("Admin削除エラー")
        flash(f"削除に失敗しました: {e}", "error")
    return redirect(url_for("admin_dashboard") + "#sessions")


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
