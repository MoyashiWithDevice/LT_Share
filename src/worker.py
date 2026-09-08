"""LT Share - Python Workers版 (Flask + WSGI).

app.py の Workers移植:
  - D1/R2 はバインディング経由 (request.environ["workers.env"]) + run_sync
  - requests / boto3 / sqlite3 / dotenv / data/フォールバックは使わない
  - 静的CSSは [assets] (./public) から配信
  - templates/ は FilesystemLoader (pywrangler同梱) + DictLoader埋め込みのChoiceLoader
"""
import logging
import os
import re
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---- Workers互換 import (CPythonでは未導入でも動くようガード) ----
try:
    from workers import wsgi as _wsgi  # type: ignore
except Exception:  # noqa: BLE001
    _wsgi = None

try:
    from pyodide.ffi import run_sync as _run_sync  # type: ignore
except Exception:  # noqa: BLE001
    _run_sync = None

try:
    from js import Object as _JsObject  # type: ignore
except Exception:  # noqa: BLE001
    _JsObject = None

try:
    from pyodide.ffi import to_js as _to_js  # type: ignore
except Exception:  # noqa: BLE001
    _to_js = None


def _run_await(coro):
    """Workers(Pyodide)のrun_sync。CPython直実行時はasyncio.runにフォールバック。"""
    if _run_sync is not None:
        return _run_sync(coro)
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("run_sync unavailable inside running loop (CPython)")


_BASE = os.path.dirname(__file__)
_TEMPLATES_DIR = os.path.abspath(os.path.join(_BASE, "..", "templates"))
_STATIC_DIR = os.path.abspath(os.path.join(_BASE, "..", "public", "static"))

app = Flask(__name__, template_folder=_TEMPLATES_DIR, static_folder=_STATIC_DIR)

# Workers本番は templates/ が同梱されないため DictLoader 埋め込みをフォールバックに。
# templates/ が正本。変更後は scripts/sync_worker_templates.py で src/_templates.py を再生成。
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

_EMBEDDED_TEMPLATES = {
'index.html': '<!DOCTYPE html>\n<html lang="ja">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>{{ presentation.title }} | LT Share</title>\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n</head>\n<body>\n<header id="site-header"><div class="header-inner"><span class="logo">LT Share</span><nav class="header-nav"><a href="{{ url_for(\'list_page\') }}">List</a></nav></div></header>\n{% if presentation.has_link %}\n<div id="object-wrapper">\n  <div class="slide-link-card">\n    <div class="slide-link-icon">🔗</div>\n    <p class="slide-link-text">この発表の資料は外部リンクで公開されています</p>\n    <a class="slide-link-url" href="{{ presentation.slide_url }}" target="_blank" rel="noopener">{{ presentation.slide_url }}</a>\n  </div>\n</div>\n{% elif presentation.has_pdf %}\n<div id="object-wrapper">\n  <object id="slide" data="{{ presentation.pdf_url }}"></object>\n</div>\n{% else %}\n<div id="object-wrapper">\n  <div class="no-slide">\n    <div class="no-slide-icon">📄</div>\n    <p>資料なし</p>\n  </div>\n</div>\n{% endif %}\n<div id="title"><h1 id="title-name">{{ presentation.title }}</h1><div id="title-tag">{% for tag in presentation.tags %}<span>#{{ tag }}</span>{% else %}<span class="no-tag">タグ未設定</span>{% endfor %}</div></div>\n<div id="other"><div id="person"><span id="name">{{ presentation.presenter_name }}</span><span>{{ presentation.grade_display }}</span></div><div id="date"><span>{{ presentation.session.name }}</span><span>{{ presentation.session.event_date_display }}</span></div></div>\n<hr>\n<div id="detail">\n  <h3>Comment</h3>\n  {% if presentation.comment %}\n  <p id="comment">{{ presentation.comment }}</p>\n  {% else %}\n  <p id="comment">コメントなし</p>\n  {% endif %}\n  {% if presentation.related_links %}\n  <h3>Related Links</h3>\n  <ul id="related-links">\n    {% for url in presentation.related_links %}\n    <li><a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></li>\n    {% endfor %}\n  </ul>\n  {% endif %}\n</div>\n<footer>\n{% if prev %}\n  <a href="{{ url_for(\'presentation_detail\', round_num=prev.session_round|int, order_num=prev.presentation_order|int) }}" class="nav-button prev"><div class="button-container btn-prev"><div class="btn-desc">← 前の記事:</div><div>{{ prev.title }}</div></div></a>\n{% endif %}\n{% if next %}\n  <a href="{{ url_for(\'presentation_detail\', round_num=next.session_round|int, order_num=next.presentation_order|int) }}" class="nav-button next"><div class="button-container btn-next"><div class="btn-desc">→ 次の記事:</div><div>{{ next.title }}</div></div></a>\n{% endif %}\n</footer>\n</body>\n</html>\n',
'list.html': '<!DOCTYPE html>\n<html lang="ja">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>List | LT Share</title>\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n</head>\n<body>\n<header id="site-header"><div class="header-inner"><span class="logo">LT Share</span><nav class="header-nav"><a href="{{ url_for(\'list_page\') }}">List</a></nav></div></header>\n<main class="list-container">\n  <h1>発表一覧</h1>\n  <form method="get" action="{{ url_for(\'list_page\') }}" id="session-filter" class="list-filter"> \n    <div class="filter-row search-row">\n      <label for="q">タイトル・ジャンル:</label>\n      <input type="search" name="q" id="q" value="{{ search_query|default(\'\') }}" placeholder="タイトル・ジャンルで検索">\n      <button type="submit" class="btn-search">検索</button>\n      {% if has_filter %}<a href="{{ url_for(\'list_page\') }}" class="filter-clear">クリア</a>{% endif %}\n    </div>\n    <div class="filter-row">\n      <label for="session_id">開催回:</label>\n      <select name="session_id" id="session_id" onchange="this.form.submit()">\n        <option value="">すべて</option>\n        {% for s in sessions %}\n        <option value="{{ s.id }}" {% if current_session and current_session.id == s.id %}selected{% endif %}>{{ s.name }}</option>\n        {% endfor %}\n      </select>\n      <label class="check-label"><input type="checkbox" name="has_slide" value="1" {% if has_slide_only %}checked{% endif %} onchange="this.form.submit()"> 資料ありのみ</label>\n      <span class="list-count">{{ presentations|length }}件</span>\n    </div>\n  </form>\n  <div class="table-wrapper">\n    <table class="list-table">\n      <thead>\n        <tr>\n          <th class="col-name">名前</th>\n          <th class="col-title">タイトル</th>\n          <th class="col-genre">ジャンル</th>\n          <th class="col-session">発表回</th>\n          <th class="col-slide">資料</th>\n        </tr>\n      </thead>\n      <tbody>\n        {% for p in presentations %}\n        <tr>\n          <td class="col-name">{{ p.presenter_name }}</td>\n          <td class="col-title"><a href="{{ p.detail_url }}">{{ p.title }}</a></td>\n          <td class="col-genre">\n            {% if p.tags %}\n              {% for tag in p.tags %}<span class="tag">#{{ tag }}</span>{% endfor %}\n            {% else %}\n              <span class="no-tag">-</span>\n            {% endif %}\n          </td>\n          <td class="col-session">{{ p.session.name }}</td>\n          <td class="col-slide">\n            {% if p.has_link %}\n              <a class="slide-badge link" href="{{ p.slide_url }}" target="_blank" rel="noopener">🔗 リンク</a>\n            {% elif p.has_pdf %}\n              <a class="slide-badge pdf" href="{{ p.pdf_url }}" target="_blank" rel="noopener">📄 PDF</a>\n            {% else %}\n              <span class="slide-badge none">資料なし</span>\n            {% endif %}\n          </td>\n        </tr>\n        {% else %}\n        <tr class="empty-row">\n          <td colspan="5">条件に一致する発表がありません。</td>\n        </tr>\n        {% endfor %}\n      </tbody>\n    </table>\n  </div>\n</main>\n</body>\n</html>\n',
'admin.html': '<!DOCTYPE html>\n<html lang="ja">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>管理画面 | LT Share</title>\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'admin.css\') }}">\n</head>\n<body>\n<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="{{ url_for(\'list_page\') }}">List</a><a href="{{ url_for(\'index\') }}">Top</a>{% if password_set %}<a href="{{ url_for(\'admin_logout\') }}">Logout</a>{% endif %}</nav></div></header>\n\n<main class="admin-container">\n  <div class="admin-title-row">\n    <h1>DB管理</h1>\n    {% if not password_set %}\n    <p class="flash warning">⚠ ADMIN_USERNAME / ADMIN_PASSWORD 未設定のため保護なしで公開されています。本番では必ず .env に設定してください。</p>\n    {% endif %}\n  </div>\n\n  {% with messages = get_flashed_messages(with_categories=true) %}\n    {% for category, message in messages %}\n    <p class="flash {{ category }}">{{ message }}</p>\n    {% endfor %}\n  {% endwith %}\n\n  <div class="stats">\n    <div class="stat">発表 <strong>{{ presentations|length }}</strong> 件</div>\n    <div class="stat">開催回 <strong>{{ sessions|length }}</strong> 件</div>\n  </div>\n\n  <nav class="admin-tabs">\n    <a href="#presentations">発表の追加</a>\n    <a href="#sessions">開催回 ({{ sessions|length }})</a>\n    <a href="{{ url_for(\'admin_presentations\') }}">発表一覧・編集 ({{ presentations|length }}) →</a>\n  </nav>\n\n  <!-- ===== 発表 ===== -->\n  <section id="presentations" class="admin-section">\n    <h2>発表の追加</h2>\n    <form method="post" action="{{ url_for(\'admin_create_presentation\') }}" class="card-form grid">\n      <label>タイトル *<input name="title" required maxlength="200" placeholder="例: Docker入門"></label>\n      <label>発表者<input name="presenter_name" maxlength="100" placeholder="例: I.B."></label>\n      <label>学年\n        <select name="grade">\n          <option value="">(未設定)</option>\n          <option value="1">1</option>\n          <option value="2">2</option>\n          <option value="3">3</option>\n          <option value="4">4</option>\n        </select>\n      </label>\n      <label>開催回 *\n        <select name="session_id" required>\n          <option value="">選択してください</option>\n          {% for s in sessions %}\n          <option value="{{ s.id }}">{{ s.name }} ({{ s.event_date_display }})</option>\n          {% endfor %}\n        </select>\n      </label>\n      <label>発表順 *<input name="presentation_order" type="number" min="1" step="1" required placeholder="例: 3"></label>\n      <label class="full check-label"><input type="checkbox" name="no_pdf" value="1"> PDFなし (資料なし・または外部リンクのみで公開)</label>\n      <label class="full">資料URL (PDFがない場合の共有リンク・任意)<input name="slide_url" type="url" placeholder="例: https://docs.google.com/presentation/d/..." maxlength="500"></label>\n      <div class="full related-links-field">\n        <label for="related-link-input">関連リンク (任意・最大3件)</label>\n        <input id="related-link-input" type="text" inputmode="url" placeholder="例: https://github.com/... を入力してEnter" maxlength="500" autocomplete="off">\n        <input type="hidden" name="related_url1" id="related_url1">\n        <input type="hidden" name="related_url2" id="related_url2">\n        <input type="hidden" name="related_url3" id="related_url3">\n        <div id="related-link-chips" class="link-chips" aria-live="polite"></div>\n        <p class="hint form-error" id="related-link-error" hidden></p>\n      </div>\n      <label class="full">コメント<textarea name="comment" rows="2" placeholder="発表の概要・補足"></textarea></label>\n      <p class="full hint">PDFキー（例: 第1回＋3 → Slides/1/3.pdf）は開催回と発表順から自動生成されます。「PDFなし」にするとキーは空で保存され、詳細ページに「資料なし」と表示されます。</p>\n      <label class="full">タグ (カンマ/スペース区切り)<input name="tags" placeholder="例: Linux, Docker"></label>\n      <div class="full"><button type="submit" class="btn primary">発表を追加</button></div>\n    </form>\n\n  </section>\n\n  <!-- ===== 開催回 ===== -->\n  <section id="sessions" class="admin-section">\n    <h2>開催回</h2>\n    <p class="hint">一番上の行から新規追加、各行で編集・削除できます。</p>\n    <table class="admin-table">\n      <thead><tr><th>ID</th><th>開催回名</th><th>開催日</th><th>発表数</th><th>操作</th></tr></thead>\n      <tbody>\n      <tr class="new-row">\n        <form method="post" action="{{ url_for(\'admin_create_session\') }}">\n          <td>新規</td>\n          <td><input name="name" required placeholder="例: 第3回"></td>\n          <td><input type="date" name="event_date"></td>\n          <td>—</td>\n          <td><button type="submit" class="btn small primary">追加</button></td>\n        </form>\n      </tr>\n      {% for s in sessions %}\n      <tr>\n        <form method="post" action="{{ url_for(\'admin_update_session\', session_id=s.id) }}">\n          <td>{{ s.id }}</td>\n          <td><input name="name" value="{{ s.name }}" required></td>\n          <td><input type="date" name="event_date" value="{{ s.event_date }}"></td>\n          <td>{{ s.presentation_count }}</td>\n          <td class="btn-row">\n            <button type="submit" class="btn small primary">更新</button>\n            <button type="submit" class="btn small danger" formaction="{{ url_for(\'admin_delete_session\', session_id=s.id) }}" onclick="return confirm(\'開催回「{{ s.name }}」を削除しますか？\');">削除</button>\n          </td>\n        </form>\n      </tr>\n      {% endfor %}\n      </tbody>\n    </table>\n  </section>\n\n</main>\n<script>\n(function() {\n  var MAX = 3;\n  var chipsEl = document.getElementById(\'related-link-chips\');\n  var inputEl = document.getElementById(\'related-link-input\');\n  var errEl = document.getElementById(\'related-link-error\');\n  var hiddenEls = [\n    document.getElementById(\'related_url1\'),\n    document.getElementById(\'related_url2\'),\n    document.getElementById(\'related_url3\')\n  ];\n  if (!chipsEl || !inputEl) return;\n  function showError(msg) {\n    if (!errEl) return;\n    if (!msg) { errEl.hidden = true; errEl.textContent = \'\'; }\n    else { errEl.hidden = false; errEl.textContent = msg; }\n  }\n  function getLinks() {\n    var out = [];\n    hiddenEls.forEach(function(h) { if (h && h.value.trim() !== \'\') out.push(h.value.trim()); });\n    return out;\n  }\n  function setLinks(links) {\n    for (var i = 0; i < hiddenEls.length; i++) {\n      if (hiddenEls[i]) hiddenEls[i].value = links[i] || \'\';\n    }\n  }\n  function render() {\n    var links = getLinks();\n    chipsEl.innerHTML = \'\';\n    links.forEach(function(url, idx) {\n      var chip = document.createElement(\'span\');\n      chip.className = \'link-chip\';\n      var txt = document.createElement(\'span\');\n      txt.className = \'link-chip-text\';\n      txt.textContent = url;\n      txt.title = url;\n      var btn = document.createElement(\'button\');\n      btn.type = \'button\';\n      btn.className = \'link-chip-remove\';\n      btn.setAttribute(\'aria-label\', \'削除: \' + url);\n      btn.textContent = \'×\';\n      btn.addEventListener(\'click\', function() {\n        var cur = getLinks();\n        cur.splice(idx, 1);\n        setLinks(cur);\n        render();\n        showError(\'\');\n        inputEl.focus();\n      });\n      chip.appendChild(txt);\n      chip.appendChild(btn);\n      chipsEl.appendChild(chip);\n    });\n  }\n  function tryAdd(raw) {\n    var url = (raw || \'\').trim();\n    if (!url) return false;\n    if (!/^https?:\\/\\/.+/i.test(url)) { showError(\'関連リンクは http(s):// から始めてください\'); return false; }\n    var cur = getLinks();\n    if (cur.indexOf(url) !== -1) { showError(\'同じURLは既に追加されています\'); return false; }\n    if (cur.length >= MAX) { showError(\'関連リンクは最大\' + MAX + \'件までです\'); return false; }\n    cur.push(url);\n    setLinks(cur);\n    render();\n    showError(\'\');\n    return true;\n  }\n  inputEl.addEventListener(\'keydown\', function(e) {\n    if (e.key === \'Enter\') {\n      e.preventDefault();\n      if (tryAdd(inputEl.value)) inputEl.value = \'\';\n    }\n  });\n  var form = inputEl.closest(\'form\');\n  if (form) {\n    form.addEventListener(\'submit\', function(e) {\n      var pending = inputEl.value.trim();\n      if (!pending) return;\n      var cur = getLinks();\n      if (!/^https?:\\/\\/.+/i.test(pending)) {\n        e.preventDefault();\n        showError(\'関連リンクは http(s):// から始めてください\');\n        inputEl.focus();\n        return;\n      }\n      if (cur.indexOf(pending) !== -1) {\n        e.preventDefault();\n        showError(\'同じURLは既に追加されています\');\n        inputEl.focus();\n        return;\n      }\n      if (cur.length >= MAX) {\n        e.preventDefault();\n        showError(\'関連リンクは最大\' + MAX + \'件までです\');\n        inputEl.focus();\n        return;\n      }\n      cur.push(pending);\n      setLinks(cur);\n      inputEl.value = \'\';\n    });\n  }\n  render();\n})();\n</script>\n</body>\n</html>\n',
'admin_login.html': '<!DOCTYPE html>\n<html lang="ja">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>管理者ログイン | LT Share</title>\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'admin.css\') }}">\n</head>\n<body>\n<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="{{ url_for(\'list_page\') }}">List</a></nav></div></header>\n<main class="admin-container narrow">\n  <h1>管理者ログイン</h1>\n  {% if error %}<p class="flash error">{{ error }}</p>{% endif %}\n  {% with messages = get_flashed_messages(with_categories=true) %}\n    {% for category, message in messages %}\n    <p class="flash {{ category }}">{{ message }}</p>\n    {% endfor %}\n  {% endwith %}\n  <form method="post" action="{{ url_for(\'admin_login\') }}" class="card-form">\n    <input type="hidden" name="next" value="{{ next }}">\n    <label>ユーザ名\n      <input type="text" name="username" value="{{ username|default(\'\') }}" autofocus required autocomplete="username">\n    </label>\n    <label>パスワード\n      <input type="password" name="password" required autocomplete="current-password">\n    </label>\n    <button type="submit" class="btn primary">ログイン</button>\n  </form>\n  <p class="hint">.env の <code>ADMIN_USERNAME</code> と <code>ADMIN_PASSWORD</code> に設定した値を入力してください。</p>\n</main>\n</body>\n</html>\n',
'admin_presentations.html': '<!DOCTYPE html>\n<html lang="ja">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>発表一覧・編集 | LT Share Admin</title>\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'admin.css\') }}">\n</head>\n<body>\n<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="{{ url_for(\'admin_dashboard\') }}">管理トップ</a><a href="{{ url_for(\'list_page\') }}">List</a>{% if password_set %}<a href="{{ url_for(\'admin_logout\') }}">Logout</a>{% endif %}</nav></div></header>\n\n<main class="admin-container">\n  <div class="list-head-row">\n    <h1>発表一覧・編集</h1>\n    <a class="btn primary small" href="{{ url_for(\'admin_dashboard\') }}#presentations">＋ 新規登録</a>\n  </div>\n\n  {% with messages = get_flashed_messages(with_categories=true) %}\n    {% for category, message in messages %}\n    <p class="flash {{ category }}">{{ message }}</p>\n    {% endfor %}\n  {% endwith %}\n\n  <table class="admin-table">\n    <thead><tr><th>発表回</th><th>発表順</th><th>タイトル</th><th>資料</th></tr></thead>\n    <tbody>\n    {% for p in presentations %}\n    <tr>\n      <td>{{ p.session.name }}</td>\n      <td>{{ p.presentation_order }}</td>\n      <td><a href="{{ url_for(\'admin_edit_presentation\', presentation_id=p.id) }}">{{ p.title }}</a></td>\n      <td>{% if p.has_link %}🔗 <a href="{{ p.slide_url }}" target="_blank" rel="noopener">リンク</a>{% elif p.has_pdf %}📄 PDF{% else %}<span class="no-tag">資料なし</span>{% endif %}</td>\n    </tr>\n    {% else %}\n    <tr><td colspan="4">発表データがありません。</td></tr>\n    {% endfor %}\n    </tbody>\n  </table>\n</main>\n</body>\n</html>\n',
'admin_presentation_edit.html': '<!DOCTYPE html>\n<html lang="ja">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>発表の編集 #{{ presentation.id }} | LT Share Admin</title>\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n  <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'admin.css\') }}">\n</head>\n<body>\n<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="{{ url_for(\'admin_presentations\') }}">発表一覧</a><a href="{{ url_for(\'admin_dashboard\') }}">管理トップ</a>{% if password_set %}<a href="{{ url_for(\'admin_logout\') }}">Logout</a>{% endif %}</nav></div></header>\n\n<main class="admin-container">\n  <h1>発表の編集 #{{ presentation.id }}</h1>\n\n  {% with messages = get_flashed_messages(with_categories=true) %}\n    {% for category, message in messages %}\n    <p class="flash {{ category }}">{{ message }}</p>\n    {% endfor %}\n  {% endwith %}\n\n  <form method="post" action="{{ url_for(\'admin_update_presentation\', presentation_id=presentation.id) }}" class="card-form grid">\n    <label>タイトル *<input name="title" value="{{ presentation.title }}" required maxlength="200"></label>\n    <label>発表者<input name="presenter_name" value="{{ presentation.presenter_name }}" maxlength="100"></label>\n    <label>学年\n      <select name="grade">\n        <option value="" {% if not presentation.grade %}selected{% endif %}>(未設定)</option>\n        <option value="1" {% if presentation.grade == "1" %}selected{% endif %}>1</option>\n        <option value="2" {% if presentation.grade == "2" %}selected{% endif %}>2</option>\n        <option value="3" {% if presentation.grade == "3" %}selected{% endif %}>3</option>\n        <option value="4" {% if presentation.grade == "4" %}selected{% endif %}>4</option>\n      </select>\n    </label>\n    <label>開催回 *\n      <select name="session_id" required>\n        {% for s in sessions %}\n        <option value="{{ s.id }}" {% if presentation.session_id == s.id %}selected{% endif %}>{{ s.name }} ({{ s.event_date_display }})</option>\n        {% endfor %}\n      </select>\n    </label>\n    <label class="full">発表順 *<input name="presentation_order" type="number" min="1" step="1" required value="{{ presentation.presentation_order }}" placeholder="例: 3"></label>\n    <label class="full check-label"><input type="checkbox" name="no_pdf" value="1" {% if not presentation.has_pdf %}checked{% endif %}> PDFなし (資料なし・または外部リンクのみで公開)</label>\n    <label class="full">資料URL (PDFがない場合の共有リンク・任意)<input name="slide_url" type="url" value="{{ presentation.slide_url }}" placeholder="例: https://docs.google.com/presentation/d/..." maxlength="500"></label>\n    <div class="full related-links-field">\n      <label for="related-link-input">関連リンク (任意・最大3件)</label>\n      <input id="related-link-input" type="text" inputmode="url" placeholder="例: https://github.com/... を入力してEnter" maxlength="500" autocomplete="off">\n      <input type="hidden" name="related_url1" id="related_url1" value="{{ presentation.related_url1 }}">\n      <input type="hidden" name="related_url2" id="related_url2" value="{{ presentation.related_url2 }}">\n      <input type="hidden" name="related_url3" id="related_url3" value="{{ presentation.related_url3 }}">\n      <div id="related-link-chips" class="link-chips" aria-live="polite"></div>\n      <p class="hint form-error" id="related-link-error" hidden></p>\n    </div>\n    <label class="full">コメント<textarea name="comment" rows="3">{{ presentation.comment }}</textarea></label>\n    <label class="full">タグ (カンマ/スペース区切り)<input name="tags" value="{{ presentation.tags|join(\', \') }}" placeholder="例: Linux, Docker"></label>\n    <p class="full hint">PDFキーは開催回と発表順から自動生成されます。現在の状態: {% if presentation.has_link %}<strong>外部リンク</strong> (外部リンク優先で表示されます){% elif presentation.has_pdf %}<strong>PDFあり</strong> (<code>{{ presentation.pdf_key }}</code>){% else %}<strong>資料なし</strong>{% endif %}</p>\n    <div class="full btn-row">\n      <button type="submit" class="btn primary">更新</button>\n      <a class="btn" href="{{ presentation.detail_url }}" target="_blank" rel="noopener">表示確認</a>\n      <a class="btn" href="{{ url_for(\'admin_presentations\') }}">一覧に戻る</a>\n      <button type="submit" class="btn danger" formaction="{{ url_for(\'admin_delete_presentation\', presentation_id=presentation.id) }}" onclick="return confirm(\'発表 #{{ presentation.id }}「{{ presentation.title }}」を削除しますか？\');">削除</button>\n    </div>\n  </form>\n</main>\n<script>\n(function() {\n  var MAX = 3;\n  var chipsEl = document.getElementById(\'related-link-chips\');\n  var inputEl = document.getElementById(\'related-link-input\');\n  var errEl = document.getElementById(\'related-link-error\');\n  var hiddenEls = [\n    document.getElementById(\'related_url1\'),\n    document.getElementById(\'related_url2\'),\n    document.getElementById(\'related_url3\')\n  ];\n  if (!chipsEl || !inputEl) return;\n  function showError(msg) {\n    if (!errEl) return;\n    if (!msg) { errEl.hidden = true; errEl.textContent = \'\'; }\n    else { errEl.hidden = false; errEl.textContent = msg; }\n  }\n  function getLinks() {\n    var out = [];\n    hiddenEls.forEach(function(h) { if (h && h.value.trim() !== \'\') out.push(h.value.trim()); });\n    return out;\n  }\n  function setLinks(links) {\n    for (var i = 0; i < hiddenEls.length; i++) {\n      if (hiddenEls[i]) hiddenEls[i].value = links[i] || \'\';\n    }\n  }\n  function render() {\n    var links = getLinks();\n    chipsEl.innerHTML = \'\';\n    links.forEach(function(url, idx) {\n      var chip = document.createElement(\'span\');\n      chip.className = \'link-chip\';\n      var txt = document.createElement(\'span\');\n      txt.className = \'link-chip-text\';\n      txt.textContent = url;\n      txt.title = url;\n      var btn = document.createElement(\'button\');\n      btn.type = \'button\';\n      btn.className = \'link-chip-remove\';\n      btn.setAttribute(\'aria-label\', \'削除: \' + url);\n      btn.textContent = \'×\';\n      btn.addEventListener(\'click\', function() {\n        var cur = getLinks();\n        cur.splice(idx, 1);\n        setLinks(cur);\n        render();\n        showError(\'\');\n        inputEl.focus();\n      });\n      chip.appendChild(txt);\n      chip.appendChild(btn);\n      chipsEl.appendChild(chip);\n    });\n  }\n  function tryAdd(raw) {\n    var url = (raw || \'\').trim();\n    if (!url) return false;\n    if (!/^https?:\\/\\/.+/i.test(url)) { showError(\'関連リンクは http(s):// から始めてください\'); return false; }\n    var cur = getLinks();\n    if (cur.indexOf(url) !== -1) { showError(\'同じURLは既に追加されています\'); return false; }\n    if (cur.length >= MAX) { showError(\'関連リンクは最大\' + MAX + \'件までです\'); return false; }\n    cur.push(url);\n    setLinks(cur);\n    render();\n    showError(\'\');\n    return true;\n  }\n  inputEl.addEventListener(\'keydown\', function(e) {\n    if (e.key === \'Enter\') {\n      e.preventDefault();\n      if (tryAdd(inputEl.value)) inputEl.value = \'\';\n    }\n  });\n  var form = inputEl.closest(\'form\');\n  if (form) {\n    form.addEventListener(\'submit\', function(e) {\n      var pending = inputEl.value.trim();\n      if (!pending) return;\n      var cur = getLinks();\n      if (!/^https?:\\/\\/.+/i.test(pending)) {\n        e.preventDefault();\n        showError(\'関連リンクは http(s):// から始めてください\');\n        inputEl.focus();\n        return;\n      }\n      if (cur.indexOf(pending) !== -1) {\n        e.preventDefault();\n        showError(\'同じURLは既に追加されています\');\n        inputEl.focus();\n        return;\n      }\n      if (cur.length >= MAX) {\n        e.preventDefault();\n        showError(\'関連リンクは最大\' + MAX + \'件までです\');\n        inputEl.focus();\n        return;\n      }\n      cur.push(pending);\n      setLinks(cur);\n      inputEl.value = \'\';\n    });\n  }\n  render();\n})();\n</script>\n</body>\n</html>\n',
}
try:
    app.jinja_loader = ChoiceLoader(
        [FileSystemLoader(_TEMPLATES_DIR), DictLoader(_EMBEDDED_TEMPLATES)]
    )
except Exception:  # noqa: BLE001
    pass
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------- Admin 認証設定 ----------
# 本番は wrangler secret put ADMIN_USERNAME / ADMIN_PASSWORD / SECRET_KEY
# ローカルは .dev.vars (workers.env) または .env (os.getenv)


# ---------- Workers env helpers ----------
# .dev.vars / secret は request.environ["workers.env"] に入る。
# os.getenv は旧Flask(.env)互換のフォールバック。


def _workers_env():
    try:
        from flask import has_request_context

        if has_request_context():
            e = request.environ.get("workers.env")
            if e is not None:
                return e
    except Exception:  # noqa: BLE001
        pass
    try:
        from workers import env as _top_env  # type: ignore

        return _top_env
    except Exception:  # noqa: BLE001
        return None


def get_env(name, default=""):
    e = _workers_env()
    if e is not None:
        try:
            v = getattr(e, name, None)
            if v is not None:
                return str(v)
        except Exception:  # noqa: BLE001
            pass
        try:
            # JsProxy dict風アクセスのフォールバック
            v = e[name]  # type: ignore
            if v is not None:
                return str(v)
        except Exception:  # noqa: BLE001
            pass
    v = os.getenv(name, default)
    return v if v is not None else default


def get_admin_password():
    return get_env("ADMIN_PASSWORD", "")


def get_admin_username():
    return get_env("ADMIN_USERNAME", "").strip()


def is_auth_enabled():
    """管理画面の保護が有効か。USERNAME・PASSWORDのどちらも空の時だけ無効 (開発用)。"""
    return bool(get_admin_password() or get_admin_username())


def get_secret_key():
    return get_env("SECRET_KEY", "") or get_env("ADMIN_PASSWORD", "") or "dev-secret-change-me"


def get_r2_public_base():
    return get_env("R2_PUBLIC_BASE_URL", "").rstrip("/")


app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")


@app.before_request
def _sync_secret_from_env():
    # Workers secret はリクエスト毎にしか取れないためここで反映
    try:
        sk = get_secret_key()
        if sk:
            app.secret_key = sk
    except Exception:  # noqa: BLE001
        pass


def is_admin():
    """管理者としてログイン済みか。USERNAME・PASSWORDのどちらも未設定時は常にTrue (開発用)。"""
    if not is_auth_enabled():
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
    """書き込み系API用。未ログイン時はJSON 401を返す (認証有効時のみ)。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_auth_enabled() and not session.get("admin"):
            return jsonify({"error": "admin login required"}), 401
        return view(*args, **kwargs)
    return wrapped


def _get_db_binding():
    e = _workers_env()
    if e is None:
        return None
    try:
        db = getattr(e, "DB", None)
        return db
    except Exception:  # noqa: BLE001
        return None


def _get_r2_binding():
    e = _workers_env()
    if e is None:
        return None
    try:
        return getattr(e, "SLIDES", None)
    except Exception:  # noqa: BLE001
        return None


def _row_to_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        if hasattr(row, "to_py"):
            d = row.to_py()
            if isinstance(d, dict):
                return {str(k): v for k, v in d.items()}
    except Exception:  # noqa: BLE001
        pass
    if _JsObject is not None:
        try:
            keys = list(_JsObject.keys(row))
            return {str(k): getattr(row, k) for k in keys}
        except Exception:  # noqa: BLE001
            pass
    try:
        return dict(row)
    except Exception:  # noqa: BLE001
        pass
    try:
        out = {}
        for k in dir(row):
            if k.startswith("_"):
                continue
            try:
                out[k] = getattr(row, k)
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return {}


def db_query(sql, params=None):
    """D1バインディング経由。pywrangler dev --local でも同じコードで動く。"""
    db = _get_db_binding()
    if db is None:
        raise RuntimeError(
            "D1 binding `DB` が未設定です。pywrangler dev (D1 local) または "
            "wrangler.toml の [[d1_databases]] を確認してください。"
        )
    params = params or []
    stmt = db.prepare(sql)
    if params:
        stmt = stmt.bind(*params)
    res = _run_await(stmt.all())
    try:
        raw = getattr(res, "results", []) or []
    except Exception:  # noqa: BLE001
        raw = []
    out = []
    try:
        n = int(getattr(raw, "length", len(raw)))
    except Exception:  # noqa: BLE001
        n = 0
    # JsProxy配列とPythonリストの両対応
    try:
        iterator = list(raw) if not hasattr(raw, "length") else [raw[i] for i in range(n)]
    except Exception:  # noqa: BLE001
        iterator = []
    for r in iterator:
        d = _row_to_dict(r)
        if d:
            out.append(d)
    return out


def db_query_first(sql, params=None):
    db = _get_db_binding()
    if db is None:
        raise RuntimeError("D1 binding `DB` が未設定です")
    params = params or []
    stmt = db.prepare(sql)
    if params:
        stmt = stmt.bind(*params)
    row = _run_await(stmt.first())
    if row is None:
        return None
    return _row_to_dict(row)


def db_execute(sql, params=None):
    """INSERT/UPDATE/DELETE用。"""
    db = _get_db_binding()
    if db is None:
        raise RuntimeError("D1 binding `DB` が未設定です")
    params = params or []
    stmt = db.prepare(sql)
    if params:
        stmt = stmt.bind(*params)
    _run_await(stmt.run())
    return


# ---------- R2 設定 ----------
# public運用: R2_PUBLIC_BASE_URL 直結。なければ /r2/<key> でバインディング配信。


def r2_mode():
    """public / r2-binding のいずれか。署名発行・local(data/)は廃止。"""
    if get_r2_public_base():
        return "public"
    if _get_r2_binding() is not None:
        return "r2-binding"
    return "none"


def pdf_url_for(pdf_key):
    if not pdf_key:
        return ""
    key = pdf_key.lstrip("/")
    base = get_r2_public_base()
    if base:
        return f"{base}/{key}"
    if _get_r2_binding() is not None:
        return f"/r2/{key}"
    return f"/r2/{key}"


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
    """登録用: '', '1'〜'4'、旧形式 'N-year' を受け付け、数字 or '' を返す。受付不可は None。"""
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
        return f"{g}-year"
    return ""


def normalize_slide_url(raw):
    """外部共有リンク用: 空文字OK。http(s)://始まりのみ受け付け、それ以外は None(エラー)。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if text == "":
        return ""
    if re.match(r"^https?://", text, re.IGNORECASE):
        return text
    return None


def material_of(pdf_key, slide_url):
    """資料種別を返す: 'link' / 'pdf' / 'none'。外部リンクがあれば優先。"""
    if (slide_url or "").strip():
        return "link"
    if (pdf_key or "").strip():
        return "pdf"
    return "none"


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


def _bump_revision(presentation_id):
    """revision を+1する。未マイグレーションDBでは何もしない (互換用)。"""
    try:
        db_execute("UPDATE presentations SET revision = revision + 1 WHERE id = ?", [presentation_id])
    except Exception as e:  # noqa: BLE001
        if "revision" not in str(e).lower():
            raise


def presentation_order_of(row, pdf_key=""):
    """発表順を返す。DBカラム presentation_order を正とし、0・欠損時は pdf_key から補完。"""
    raw = row.get("presentation_order")
    try:
        if raw is not None and str(raw).strip() != "" and int(raw) >= 1:
            return str(int(raw))
    except (TypeError, ValueError):
        pass
    return pdf_order_from_key(pdf_key or row.get("pdf_key") or "")


def row_to_presentation(row):
    """DB行 -> テンプレート用dict。回数・日付は sessions JOIN 結果から取得。"""
    event_date = ((row.get("s_event_date") or "").strip())
    pdf_key = (row.get("pdf_key") or "").strip().lstrip("/")
    slide_url = (row.get("slide_url") or "").strip()
    try:
        pdf_url = pdf_url_for(pdf_key)
    except Exception:
        pdf_url = f"/data/{pdf_key}" if pdf_key else ""
    _grade = normalize_grade_input(row.get("grade"))
    grade = _grade if _grade is not None else ""
    session_round = session_round_number({"name": row.get("s_name"), "id": row.get("s_id")})
    # 発表順はDBカラムを正とする。未マイグレーションDB・旧行(0)は pdf_key から逆算で補完
    presentation_order = presentation_order_of(row, pdf_key)
    # 関連リンク (未マイグレーションDBではカラム自体がないため .get で安全に取得)
    related_url1 = ((row.get("related_url1") or "").strip())
    related_url2 = ((row.get("related_url2") or "").strip())
    related_url3 = ((row.get("related_url3") or "").strip())
    related_links = [u for u in (related_url1, related_url2, related_url3) if u]
    detail_url = (
        f"/{session_round}/{presentation_order}"
        if presentation_order
        else (f"/?id={row.get('id')}" if row.get("id") is not None else "")
    )
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
        "session_round": session_round,
        "event_date": event_date,
        "event_date_display": event_date.replace("-", ".") if event_date else "",
        "comment": row.get("comment") or "",
        "pdf_key": pdf_key,
        "slide_url": slide_url,
        "has_pdf": bool(pdf_key),
        "has_link": bool(slide_url),
        "material": material_of(pdf_key, slide_url),
        "related_url1": related_url1,
        "related_url2": related_url2,
        "related_url3": related_url3,
        "related_links": related_links,
        "presentation_order": presentation_order,
        "revision": row.get("revision") if row.get("revision") is not None else 0,
        "detail_url": detail_url,
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


def get_presentation_by_round_order(round_num, order_num):
    """発表回番号・発表順番号から発表1件を取得。URL /{round}/{order} 用。"""
    try:
        round_num, order_num = int(round_num), int(order_num)
    except (TypeError, ValueError):
        return None
    if order_num < 1:
        return None
    try:
        sess_rows = db_query("SELECT * FROM sessions")
    except Exception:
        raise
    target_ids = []
    for s in sess_rows:
        try:
            if int(session_round_number(s)) == round_num:
                target_ids.append(s["id"])
        except (TypeError, ValueError):
            continue
    if not target_ids:
        return None
    for sid in target_ids:
        rows = db_query(f"{_PRES_SELECT} WHERE p.session_id = ?", [sid])
        for p in _attach_tags([row_to_presentation(r) for r in rows]):
            if p.get("presentation_order") == str(order_num):
                return p
    return None


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
    """発表のタグ紐づけを置き換える。存在しないタグ名は自動作成。

    消失対策: 旧実装の DELETE-first (全削除→再登録) では、途中で失敗すると
    タグが全滅した。追加分を先に登録し、外れた分だけ後で消すことで、
    失敗時は「残る」側に倒れるようにしている。
    """
    names = [n.strip() for n in (tag_names or []) if (n or "").strip()]
    # 重複除去 (順序保持)
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    wanted_ids = set()
    for name in uniq:
        db_execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", [name])
        rows = db_query("SELECT id FROM tags WHERE name = ?", [name])
        if rows:
            wanted_ids.add(rows[0]["id"])
    current_ids = {
        r["tag_id"]
        for r in db_query(
            "SELECT tag_id FROM presentation_tags WHERE presentation_id = ?",
            [presentation_id],
        )
    }
    for tid in wanted_ids - current_ids:
        db_execute(
            "INSERT OR IGNORE INTO presentation_tags (presentation_id, tag_id) VALUES (?, ?)",
            [presentation_id, tid],
        )
    for tid in current_ids - wanted_ids:
        db_execute(
            "DELETE FROM presentation_tags WHERE presentation_id = ? AND tag_id = ?",
            [presentation_id, tid],
        )


def split_title_keywords(raw):
    """タイトル検索用: 空白区切りでキーワード分割 (AND検索)。"""
    if not raw:
        return []
    text = str(raw).replace("　", " ")
    return [w.strip() for w in text.split() if w.strip()]


def escape_like(s):
    """LIKEパターン用エスケープ (\\, %, _)。"""
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_all_presentations(session_id=None, title_query=None, genre_query=None, keyword_query=None, has_slide_only=False):
    """開催回・タイトル・ジャンル(タグ)で絞り込み取得。

    - title_query: 文字列またはキーワードリスト。タイトルに全キーワードを含む(AND・部分一致)
    - genre_query: 文字列またはタグ名リスト。指定タグを全て含む発表に絞る(AND・部分一致)
    - keyword_query: 文字列またはキーワードリスト。タイトルorジャンルのどちらかに全キーワードを含む(AND・部分一致)
    """
    if isinstance(title_query, str):
        title_keywords = split_title_keywords(title_query)
    else:
        title_keywords = [t for t in (title_query or []) if str(t).strip()]
    if isinstance(genre_query, str):
        genre_keywords = parse_tag_input(genre_query)
    else:
        genre_keywords = [g for g in (genre_query or []) if str(g).strip()]
    if isinstance(keyword_query, str):
        keyword_keywords = parse_tag_input(keyword_query)
    else:
        keyword_keywords = [k for k in (keyword_query or []) if str(k).strip()]
    where, params = [], []
    if session_id is not None:
        where.append("p.session_id = ?")
        params.append(session_id)
    if has_slide_only:
        # PDFまたは外部リンクのどちらかがあるもののみ (資料なしを除外)
        # slide_urlカラム未マイグレーションのDBでも動くよう、存在確認は呼び出し側で行う
        where.append("(COALESCE(p.pdf_key, '') != '' OR COALESCE(p.slide_url, '') != '')")
    for kw in title_keywords:
        where.append("p.title LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(kw)}%")
    for kw in genre_keywords:
        where.append(
            "EXISTS (SELECT 1 FROM presentation_tags pt "
            "JOIN tags t ON t.id = pt.tag_id "
            "WHERE pt.presentation_id = p.id AND t.name LIKE ? ESCAPE '\\')"
        )
        params.append(f"%{escape_like(kw)}%")
    for kw in keyword_keywords:
        like = f"%{escape_like(kw)}%"
        where.append(
            "(p.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM presentation_tags pt "
            "JOIN tags t ON t.id = pt.tag_id "
            "WHERE pt.presentation_id = p.id AND t.name LIKE ? ESCAPE '\\'))"
        )
        params.extend([like, like])
    sql = _PRES_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.event_date DESC, p.id DESC"
    try:
        rows = db_query(sql, params)
    except Exception as e:  # noqa: BLE001
        # slide_url未マイグレーションのDB互換 (D1の400時も含む): 条件を外してPython側で絞る
        if has_slide_only:
            log.warning("has_slide_only条件を外して再試行します (slide_url未移行の可能性): %s", e)
            where2 = [w for w in where if "slide_url" not in w and "COALESCE" not in w]
            # has_slide_only由来の条件だけ外す: 最後のCOALESCE条件を除去
            # (COALESCE条件が1件だけのはずなので、pdf_keyのみ条件に緩和できない場合は全件→Python絞り)
            sql2 = _PRES_SELECT
            if where2:
                sql2 += " WHERE " + " AND ".join(where2)
            sql2 += " ORDER BY s.event_date DESC, p.id DESC"
            # paramsは slide_url条件に紐づくparamがないためそのまま使える
            rows = db_query(sql2, params)
            items = _attach_tags([row_to_presentation(r) for r in rows])
            return [p for p in items if p.get("has_pdf") or p.get("has_link")]
        raise
    return _attach_tags([row_to_presentation(r) for r in rows])


# ---------- Routes ----------


@app.route("/")
def index():
    """ルートは一覧ページを表示。旧形式 ?id= 指定時は正規URLへリダイレクト。"""
    pid = request.args.get("id", type=int)
    if pid is None:
        # 一覧表示 (list_page と同じ絞り込みに対応)
        return list_page()
    try:
        presentation = get_presentation(pid)
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    if presentation is None:
        abort(404, description="発表データが見つかりません")
    # 旧URLから正規URL (/{発表回}/{発表順}) へ誘導。PDFなし等で正規URLがない場合はそのまま表示
    detail_url = presentation.get("detail_url") or ""
    if detail_url.startswith("/") and not detail_url.startswith("/?"):
        return redirect(detail_url, code=302)
    return render_template("index.html", presentation=presentation)


@app.route("/<int:round_num>/<int:order_num>")
def presentation_detail(round_num, order_num):
    """個別発表ページ。URL形式: /{発表回番号}/{発表順番号} (例: /1/3)。"""
    try:
        presentation = get_presentation_by_round_order(round_num, order_num)
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    if presentation is None:
        abort(404, description="発表データが見つかりません")
    # 前後の発表を取得 (同一回の前後順)
    prev_presentation = None
    next_presentation = None
    if order_num > 1:
        prev_presentation = get_presentation_by_round_order(round_num, order_num - 1)
    # 次の発表は order+1 を試す (存在しない場合は None)
    next_presentation = get_presentation_by_round_order(round_num, order_num + 1)
    return render_template(
        "index.html",
        presentation=presentation,
        prev=prev_presentation,
        next=next_presentation,
    )


@app.route("/list")
def list_page():
    """?session_id= で開催回、?q= でタイトル・ジャンル(タグ)、?has_slide=1 で資料ありのみ絞り込み可。"""
    session_id = request.args.get("session_id", type=int)
    search_query = (request.args.get("q") or "").strip()
    has_slide_only = request.args.get("has_slide") == "1"
    # 旧パラメータ (?title= / ?genre=) との後方互換: q が空なら旧値を引き継ぐ
    if not search_query:
        legacy = " ".join(
            [
                (request.args.get("title") or "").strip(),
                (request.args.get("genre") or "").strip(),
            ]
        ).strip()
        search_query = legacy
    try:
        presentations = get_all_presentations(
            session_id=session_id, keyword_query=search_query, has_slide_only=has_slide_only
        )
        sessions = get_sessions()
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    current_session = None
    if session_id is not None:
        current_session = next((s for s in sessions if s["id"] == session_id), None)
    has_filter = bool(current_session or search_query or has_slide_only)
    return render_template(
        "list.html",
        presentations=presentations,
        sessions=sessions,
        current_session=current_session,
        search_query=search_query,
        has_filter=has_filter,
        has_slide_only=has_slide_only,
    )


@app.route("/slides/<int:presentation_id>")
def slide_redirect(presentation_id):
    """資料へのリダイレクト (共有リンク用)。外部リンク優先、なければPDF、どちらもなければ404。"""
    try:
        p = get_presentation(presentation_id)
    except Exception as e:  # noqa: BLE001
        log.exception("D1取得エラー")
        abort(502, description=f"データ取得に失敗しました: {e}")
    if p is None:
        abort(404, description="スライドが見つかりません")
    if p.get("slide_url"):
        return redirect(p["slide_url"], code=302)
    if p.get("pdf_url"):
        return redirect(p["pdf_url"], code=302)
    abort(404, description="資料なし")


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
    title_q = (request.args.get("title") or "").strip()
    genre_q = (request.args.get("genre") or "").strip()
    keyword_q = (request.args.get("q") or "").strip()
    has_slide_only = request.args.get("has_slide") == "1"
    try:
        return jsonify(
            get_all_presentations(
                session_id=session_id,
                title_query=title_q,
                genre_query=genre_q,
                keyword_query=keyword_q,
                has_slide_only=has_slide_only,
            )
        )
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
    R2バインディング (SLIDES) を使用。public運用時は公開URLへリダイレクトで配信。
    """
    r2 = _get_r2_binding()
    if r2 is None:
        return jsonify({"error": "R2 binding not configured"}), 503
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
        data = f.read()
        put_opts = None
        if _to_js is not None:
            try:
                from js import Object as _Obj  # type: ignore

                put_opts = _to_js(
                    {"httpMetadata": {"contentType": "application/pdf"}},
                    dict_converter=_Obj.fromEntries,
                )
            except Exception:  # noqa: BLE001
                put_opts = None
        if put_opts is not None:
            _run_await(r2.put(key, data, put_opts))
        else:
            try:
                _run_await(r2.put(key, data))
            except TypeError:
                # 一部ランタイムは第3引数dict可
                _run_await(r2.put(key, data, {"httpMetadata": {"contentType": "application/pdf"}}))
        db_execute("UPDATE presentations SET pdf_key = ? WHERE id = ?", [key, presentation_id])
    except Exception as e:  # noqa: BLE001
        log.exception("R2アップロードエラー")
        return jsonify({"error": str(e)}), 502
    return jsonify({"id": presentation_id, "pdf_key": key, "pdf_url": pdf_url_for(key)})


@app.route("/api/presentations/<int:presentation_id>/pdf", methods=["DELETE"])
@admin_api_required
def api_delete_pdf(presentation_id):
    """PDF紐づけを外す (pdf_key を空に = 資料なし)。R2オブジェクト自体は残る。"""
    try:
        if get_presentation(presentation_id) is None:
            return jsonify({"error": "not found"}), 404
        db_execute("UPDATE presentations SET pdf_key = '' WHERE id = ?", [presentation_id])
    except Exception as e:  # noqa: BLE001
        log.exception("PDF解除エラー")
        return jsonify({"error": str(e)}), 502
    return jsonify(get_presentation(presentation_id))


# ---------- Admin (管理画面) ----------


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """管理者ログイン。ADMIN_USERNAME・ADMIN_PASSWORDのどちらも未設定時はそのままダッシュボードへ。"""
    if not is_auth_enabled():
        return redirect(url_for("admin_dashboard"))
    if session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    error = None
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next") or url_for("admin_dashboard")
        if username == get_admin_username() and password == get_admin_password():
            session["admin"] = True
            session["admin_user"] = username
            return redirect(next_url)
        error = "ユーザ名またはパスワードが違います"
    return render_template("admin_login.html", error=error, next=request.args.get("next", "/admin/"), username=username)


@app.route("/admin/logout", methods=["GET", "POST"])
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_user", None)
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
        password_set=bool(is_auth_enabled()),
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
        password_set=bool(is_auth_enabled()),
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
        password_set=bool(is_auth_enabled()),
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
    slide_url = normalize_slide_url(request.form.get("slide_url", ""))
    related_url1 = normalize_slide_url(request.form.get("related_url1", ""))
    related_url2 = normalize_slide_url(request.form.get("related_url2", ""))
    related_url3 = normalize_slide_url(request.form.get("related_url3", ""))
    no_pdf = request.form.get("no_pdf") == "1"
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
    if slide_url is None:
        flash("資料URLは http(s):// から始めてください (空欄可)", "error")
        return redirect(url_for("admin_dashboard") + "#presentations")
    if related_url1 is None or related_url2 is None or related_url3 is None:
        flash("関連リンクは http(s):// から始めてください (空欄可)", "error")
        return redirect(url_for("admin_dashboard") + "#presentations")
    try:
        sess_rows = db_query("SELECT * FROM sessions WHERE id = ?", [session_id])
        if not sess_rows:
            flash("指定の開催回が存在しません", "error")
            return redirect(url_for("admin_dashboard") + "#presentations")
        # PDFキーは開催回＋発表順から自動生成 (例: 第1回＋3 → Slides/1/3.pdf)
        # 「PDFなし」にチェックがあれば空文字で保存。発表順は専用カラムに保持するので消えない
        pdf_key = "" if no_pdf else build_pdf_key(sess_rows[0], int(order_raw))
        order_num = int(order_raw)
        try:
            db_execute(
                "INSERT INTO presentations (title, presenter_name, grade, session_id, comment, pdf_key, slide_url, presentation_order,"
                " related_url1, related_url2, related_url3)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [title, presenter_name, grade, session_id, comment, pdf_key, slide_url, order_num,
                 related_url1, related_url2, related_url3],
            )
        except Exception as e_inner:  # noqa: BLE001
            # 未マイグレーションのDB互換: 新カラムなしで再試行
            msg = str(e_inner).lower()
            if "related_url" in msg or "slide_url" in msg or "presentation_order" in msg:
                try:
                    db_execute(
                        "INSERT INTO presentations (title, presenter_name, grade, session_id, comment, pdf_key, slide_url, presentation_order)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [title, presenter_name, grade, session_id, comment, pdf_key, slide_url, order_num],
                    )
                except Exception as e_legacy:  # noqa: BLE001
                    msg2 = str(e_legacy).lower()
                    if "slide_url" in msg2 or "presentation_order" in msg2:
                        try:
                            db_execute(
                                "INSERT INTO presentations (title, presenter_name, grade, session_id, comment, pdf_key, slide_url)"
                                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                                [title, presenter_name, grade, session_id, comment, pdf_key, slide_url],
                            )
                        except Exception as e_inner2:  # noqa: BLE001
                            if "slide_url" in str(e_inner2).lower():
                                db_execute(
                                    "INSERT INTO presentations (title, presenter_name, grade, session_id, comment, pdf_key)"
                                    " VALUES (?, ?, ?, ?, ?, ?)",
                                    [title, presenter_name, grade, session_id, comment, pdf_key],
                                )
                            else:
                                raise
                    else:
                        raise
            else:
                raise
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
    slide_url = normalize_slide_url(request.form.get("slide_url", ""))
    related_url1 = normalize_slide_url(request.form.get("related_url1", ""))
    related_url2 = normalize_slide_url(request.form.get("related_url2", ""))
    related_url3 = normalize_slide_url(request.form.get("related_url3", ""))
    no_pdf = request.form.get("no_pdf") == "1"
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
    if slide_url is None:
        flash("資料URLは http(s):// から始めてください (空欄可)", "error")
        return redirect(edit_url)
    if related_url1 is None or related_url2 is None or related_url3 is None:
        flash("関連リンクは http(s):// から始めてください (空欄可)", "error")
        return redirect(edit_url)
    try:
        sess_rows = db_query("SELECT * FROM sessions WHERE id = ?", [session_id])
        if not sess_rows:
            flash("指定の開催回が存在しません", "error")
            return redirect(edit_url)
        # PDFキーは開催回＋発表順から自動生成。「PDFなし」チェック時は空文字保存
        # 発表順は専用カラムに保持するので、PDFなしでも順番は消えない
        pdf_key = "" if no_pdf else build_pdf_key(sess_rows[0], int(order_raw))
        order_num = int(order_raw)
        try:
            db_execute(
                "UPDATE presentations SET title=?, presenter_name=?, grade=?, session_id=?, comment=?, pdf_key=?, slide_url=?, presentation_order=?,"
                " related_url1=?, related_url2=?, related_url3=?"
                " WHERE id=?",
                [title, presenter_name, grade, session_id, comment, pdf_key, slide_url, order_num,
                 related_url1, related_url2, related_url3, presentation_id],
            )
        except Exception as e_inner:  # noqa: BLE001
            msg = str(e_inner).lower()
            if "related_url" in msg or "slide_url" in msg or "presentation_order" in msg:
                try:
                    db_execute(
                        "UPDATE presentations SET title=?, presenter_name=?, grade=?, session_id=?, comment=?, pdf_key=?, slide_url=?, presentation_order=?"
                        " WHERE id=?",
                        [title, presenter_name, grade, session_id, comment, pdf_key, slide_url, order_num, presentation_id],
                    )
                except Exception as e_legacy:  # noqa: BLE001
                    msg2 = str(e_legacy).lower()
                    if "slide_url" in msg2 or "presentation_order" in msg2:
                        try:
                            db_execute(
                                "UPDATE presentations SET title=?, presenter_name=?, grade=?, session_id=?, comment=?, pdf_key=?, slide_url=?"
                                " WHERE id=?",
                                [title, presenter_name, grade, session_id, comment, pdf_key, slide_url, presentation_id],
                            )
                        except Exception as e_inner2:  # noqa: BLE001
                            if "slide_url" in str(e_inner2).lower():
                                db_execute(
                                    "UPDATE presentations SET title=?, presenter_name=?, grade=?, session_id=?, comment=?, pdf_key=?"
                                    " WHERE id=?",
                                    [title, presenter_name, grade, session_id, comment, pdf_key, presentation_id],
                                )
                            else:
                                raise
                    else:
                        raise
            else:
                raise
        set_presentation_tags(presentation_id, tag_names)
        flash(f"発表 #{presentation_id} を更新しました", "success")
        return redirect(url_for("admin_presentations"))
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
    db = _get_db_binding()
    d1_ok = False
    if db is not None:
        try:
            _run_await(db.prepare("SELECT 1").run())
            d1_ok = True
        except Exception:  # noqa: BLE001
            d1_ok = False
    return jsonify(
        {
            "d1_enabled": d1_ok,
            "r2_mode": r2_mode(),
            "r2_bucket": "lt-pdf",
            "r2_public_base_url": get_r2_public_base(),
            "runtime": "python-workers",
            "embedded_templates": len(_EMBEDDED_TEMPLATES) if "_EMBEDDED_TEMPLATES" in globals() else -1,
            "jinja_loader": type(app.jinja_loader).__name__,
        }
    )


@app.route("/r2/<path:key>")
def r2_file(key):
    """R2バインディング直配信。public運用時は公開URLへリダイレクト優先。"""
    clean = (key or "").lstrip("/")
    if not clean or ".." in clean:
        abort(404)
    base = get_r2_public_base()
    if base:
        return redirect(f"{base}/{clean}", code=302)
    r2 = _get_r2_binding()
    if r2 is None:
        abort(503, description="R2 binding `SLIDES` 未設定")
    try:
        obj = _run_await(r2.get(clean))
    except Exception as e:  # noqa: BLE001
        log.exception("R2取得エラー")
        abort(502, description=str(e))
    if obj is None:
        abort(404)
    try:
        ctype = "application/pdf"
        try:
            md = getattr(obj, "httpMetadata", None)
            if md is not None:
                ctype = getattr(md, "contentType", None) or ctype
        except Exception:  # noqa: BLE001
            pass
        body = None
        for meth in ("bytes", "arrayBuffer"):
            try:
                fn = getattr(obj, meth, None)
                if fn is not None:
                    body = _run_await(fn())
                    break
            except Exception:  # noqa: BLE001
                continue
        if body is None:
            try:
                body = getattr(obj, "body", None)
            except Exception:  # noqa: BLE001
                body = None
        if body is None:
            abort(404)
        # JsProxy bytes -> Python bytes 変換
        try:
            if not isinstance(body, (bytes, bytearray)):
                if hasattr(body, "to_py"):
                    body = body.to_py()
        except Exception:  # noqa: BLE001
            pass
        return Response(bytes(body), content_type=ctype)
    except Exception as e:  # noqa: BLE001
        log.exception("R2配信エラー")
        abort(502, description=str(e))


@app.route("/data/<path:filename>")
def data_file(filename):
    """旧Flask互換: /data/<key> はR2へフォールバック (Workersにローカルfsなし)。"""
    base = get_r2_public_base()
    key = (filename or "").lstrip("/")
    if base and key:
        return redirect(f"{base}/{key}", code=302)
    return r2_file(key)


try:
    if _wsgi is not None:
        Default = _wsgi.entrypoint(app)
    else:
        Default = None
except Exception:  # noqa: BLE001
    Default = None


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
