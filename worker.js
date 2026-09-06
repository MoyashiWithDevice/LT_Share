/* LT Share - Cloudflare Workers ネイティブ版
 *
 * Flask(app.py) の移植:
 *  - メタデータは D1 バインディング (env.DB) から取得
 *  - PDF実体は R2 バインディング (env.SLIDES) から配信。R2_PUBLIC_BASE_URL があれば公開URLへリダイレクト
 *  - 静的CSSは [assets] (./public) から配信
 *  - 管理画面認証は Cookie (lt_admin) + ADMIN_PASSWORD シークレット
 */

const PRES_SELECT =
  "SELECT p.*, s.id AS s_id, s.name AS s_name, s.event_date AS s_event_date " +
  "FROM presentations p LEFT JOIN sessions s ON s.id = p.session_id";

const PDF_KEY_PREFIX = "Slides";
const GRADE_CHOICES = new Set(["1", "2", "3", "4"]);
const ADMIN_COOKIE = "lt_admin";

/* ---------- utils ---------- */

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function normalizeGradeInput(raw) {
  if (raw === null || raw === undefined) return "";
  const text = String(raw).trim();
  if (text === "") return "";
  if (GRADE_CHOICES.has(text)) return text;
  const m = text.match(/^[Gg][Rr][Aa][Dd][Ee]\s*([1-4])$/);
  if (m) return m[1];
  return null;
}

function gradeDisplay(grade) {
  const g = normalizeGradeInput(grade);
  return g ? `Grade ${g}` : "";
}

function sessionRoundNumber(sess) {
  const m = String(sess?.name ?? "").match(/(\d+)/);
  if (m) return m[1];
  return String(sess?.id ?? "");
}

function buildPdfKey(sess, order) {
  return `${PDF_KEY_PREFIX}/${sessionRoundNumber(sess)}/${parseInt(order, 10)}.pdf`;
}

function pdfOrderFromKey(pdfKey) {
  if (!pdfKey) return "";
  const name = String(pdfKey).trim().replace(/^\/+/, "").split("/").pop();
  const base = name.toLowerCase().endsWith(".pdf") ? name.slice(0, -4) : name;
  if (/^\d+$/.test(base) && parseInt(base, 10) >= 1) return String(parseInt(base, 10));
  return "";
}

function pdfUrlFor(env, pdfKey) {
  if (!pdfKey) return "";
  const key = String(pdfKey).replace(/^\/+/, "");
  const base = (env.R2_PUBLIC_BASE_URL || "").toString().replace(/\/+$/, "");
  if (base) return `${base}/${key}`;
  return `/r2/${key}`;
}

function parseTagInput(raw) {
  if (!raw) return [];
  const text = String(raw).replace(/、/g, ",").replace(/#/g, ",").replace(/　/g, " ");
  const names = [];
  for (const chunk of text.replace(/,/g, " ").split(/\s+/)) {
    const name = chunk.trim();
    if (name && !names.includes(name)) names.push(name);
  }
  return names;
}

/* ---------- cookies / auth ---------- */

function getCookies(request) {
  const out = {};
  const header = request.headers.get("cookie") || "";
  for (const part of header.split(";")) {
    const i = part.indexOf("=");
    if (i < 0) continue;
    const k = part.slice(0, i).trim();
    const v = part.slice(i + 1).trim();
    if (k) out[k] = decodeURIComponent(v);
  }
  return out;
}

async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function expectedAdminToken(env) {
  if (!env.ADMIN_PASSWORD) return "";
  return sha256hex(`lt-share-admin:${env.ADMIN_PASSWORD}`);
}

async function isAdmin(request, env) {
  if (!env.ADMIN_PASSWORD) return true; // 開発用: パスワード未設定時は保護なし
  const cookies = getCookies(request);
  if (!cookies[ADMIN_COOKIE]) return false;
  const expected = await expectedAdminToken(env);
  return cookies[ADMIN_COOKIE] === expected;
}

function adminCookieHeader(token, maxAgeSec) {
  const parts = [
    `${ADMIN_COOKIE}=${encodeURIComponent(token)}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
  ];
  if (maxAgeSec <= 0) parts.push("Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT");
  else parts.push(`Max-Age=${maxAgeSec}`);
  // 本番HTTPSでは Secure を付ける (http://localhost では付けない)
  // Secure 属性がhttpではCookieが保存されないため、条件付きにはしない。
  // Cloudflare経由は常にhttpsなので付与してよい。
  parts.push("Secure");
  return parts.join("; ");
}

function redirect(url, status = 302, extraHeaders = {}) {
  return new Response(null, {
    status,
    headers: { location: url, ...extraHeaders },
  });
}

function redirectWithFlash(url, message, kind = "info") {
  const u = new URL(url, "http://x");
  u.searchParams.set("flash", message);
  u.searchParams.set("kind", kind);
  // 相対URLに戻す
  const rel = u.pathname + (u.search ? u.search : "") + (u.hash ? u.hash : "");
  return redirect(rel);
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

function flashHtml(url) {
  const msg = url.searchParams.get("flash");
  if (!msg) return "";
  const kind = url.searchParams.get("kind") || "info";
  return `<p class="flash ${esc(kind)}">${esc(msg)}</p>`;
}

/* ---------- D1 access ---------- */

function mustDB(env) {
  if (!env.DB) throw new Error("D1 binding `DB` が未設定です (wrangler.toml を確認)");
  return env.DB;
}

function rowToSession(row) {
  const eventDate = String(row.event_date || "").trim();
  return {
    id: row.id,
    name: row.name || "",
    event_date: eventDate,
    event_date_display: eventDate ? eventDate.replace(/-/g, ".") : "",
    presentation_count: row.presentation_count ?? 0,
  };
}

function rowToPresentation(env, row) {
  const eventDate = String(row.s_event_date || "").trim();
  const pdfKey = String(row.pdf_key || "").trim().replace(/^\/+/, "");
  const g = normalizeGradeInput(row.grade);
  const sessionRound = sessionRoundNumber({ name: row.s_name, id: row.s_id });
  const presentationOrder = pdfOrderFromKey(pdfKey);
  const detailUrl = presentationOrder
    ? `/${sessionRound}/${presentationOrder}`
    : row.id != null
      ? `/?id=${row.id}`
      : "";
  return {
    id: row.id,
    title: row.title || "",
    presenter_name: row.presenter_name || "",
    grade: g === null ? "" : g,
    grade_display: gradeDisplay(row.grade),
    session_id: row.s_id,
    session: {
      id: row.s_id,
      name: row.s_name || "",
      event_date: eventDate,
      event_date_display: eventDate ? eventDate.replace(/-/g, ".") : "",
    },
    session_name: row.s_name || "",
    session_round: sessionRound,
    event_date: eventDate,
    event_date_display: eventDate ? eventDate.replace(/-/g, ".") : "",
    comment: row.comment || "",
    pdf_key: pdfKey,
    presentation_order: presentationOrder,
    detail_url: detailUrl,
    pdf_url: pdfUrlFor(env, pdfKey),
    pdf_file: pdfKey,
    tags: [],
  };
}

async function fetchTagsMap(env, ids) {
  if (!ids.length) return {};
  const placeholders = ids.map(() => "?").join(",");
  const res = await mustDB(env)
    .prepare(
      `SELECT pt.presentation_id AS pid, t.name AS name FROM presentation_tags pt ` +
        `JOIN tags t ON t.id = pt.tag_id WHERE pt.presentation_id IN (${placeholders}) ORDER BY t.id`
    )
    .bind(...ids)
    .all();
  const map = {};
  for (const id of ids) map[id] = [];
  for (const r of res.results || []) {
    if (!map[r.pid]) map[r.pid] = [];
    map[r.pid].push(r.name);
  }
  return map;
}

async function attachTags(env, items) {
  const map = await fetchTagsMap(
    env,
    items.map((p) => p.id)
  );
  for (const p of items) p.tags = map[p.id] || [];
  return items;
}

async function getSessions(env) {
  const res = await mustDB(env)
    .prepare(
      `SELECT s.*, COUNT(p.id) AS presentation_count FROM sessions s ` +
        `LEFT JOIN presentations p ON p.session_id = s.id ` +
        `GROUP BY s.id ORDER BY s.event_date DESC, s.id DESC`
    )
    .all();
  return (res.results || []).map(rowToSession);
}

function splitTitleKeywords(raw) {
  if (!raw) return [];
  return String(raw)
    .replace(/　/g, " ")
    .split(/\s+/)
    .map((w) => w.trim())
    .filter(Boolean);
}

function escapeLike(s) {
  return String(s).replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_");
}

async function getAllPresentations(env, sessionId = null, titleQuery = "", genreQuery = "", keywordQuery = "") {
  const titleKeywords =
    typeof titleQuery === "string" ? splitTitleKeywords(titleQuery) : (titleQuery || []).filter(Boolean);
  const genreKeywords =
    typeof genreQuery === "string" ? parseTagInput(genreQuery) : (genreQuery || []).filter(Boolean);
  const keywordKeywords =
    typeof keywordQuery === "string" ? parseTagInput(keywordQuery) : (keywordQuery || []).filter(Boolean);
  const where = [];
  const params = [];
  if (sessionId !== null && sessionId !== undefined) {
    where.push("p.session_id = ?");
    params.push(sessionId);
  }
  for (const kw of titleKeywords) {
    where.push("p.title LIKE ? ESCAPE '\\'");
    params.push(`%${escapeLike(kw)}%`);
  }
  for (const kw of genreKeywords) {
    where.push(
      "EXISTS (SELECT 1 FROM presentation_tags pt " +
        "JOIN tags t ON t.id = pt.tag_id " +
        "WHERE pt.presentation_id = p.id AND t.name LIKE ? ESCAPE '\\')"
    );
    params.push(`%${escapeLike(kw)}%`);
  }
  for (const kw of keywordKeywords) {
    const like = `%${escapeLike(kw)}%`;
    where.push(
      "(p.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM presentation_tags pt " +
        "JOIN tags t ON t.id = pt.tag_id " +
        "WHERE pt.presentation_id = p.id AND t.name LIKE ? ESCAPE '\\'))"
    );
    params.push(like, like);
  }
  let sql = PRES_SELECT;
  if (where.length) sql += " WHERE " + where.join(" AND ");
  sql += " ORDER BY s.event_date DESC, p.id DESC";
  const res = await mustDB(env).prepare(sql).bind(...params).all();
  const items = (res.results || []).map((r) => rowToPresentation(env, r));
  return attachTags(env, items);
}

async function getPresentation(env, id) {
  const row = await mustDB(env)
    .prepare(`${PRES_SELECT} WHERE p.id = ?`)
    .bind(id)
    .first();
  if (!row) return null;
  const items = await attachTags(env, [rowToPresentation(env, row)]);
  return items[0];
}

async function getLatestPresentation(env) {
  const row = await mustDB(env)
    .prepare(`${PRES_SELECT} ORDER BY p.id DESC LIMIT 1`)
    .first();
  if (!row) return null;
  const items = await attachTags(env, [rowToPresentation(env, row)]);
  return items[0];
}

async function getPresentationByRoundOrder(env, roundNum, orderNum) {
  const round = parseInt(roundNum, 10);
  const order = parseInt(orderNum, 10);
  if (!Number.isFinite(round) || !Number.isFinite(order) || order < 1) return null;
  const sessRes = await mustDB(env).prepare("SELECT * FROM sessions").all();
  const targetIds = [];
  for (const s of sessRes.results || []) {
    try {
      if (parseInt(sessionRoundNumber(s), 10) === round) targetIds.push(s.id);
    } catch {
      continue;
    }
  }
  if (!targetIds.length) return null;
  for (const sid of targetIds) {
    const res = await mustDB(env)
      .prepare(`${PRES_SELECT} WHERE p.session_id = ?`)
      .bind(sid)
      .all();
    const items = await attachTags(
      env,
      (res.results || []).map((r) => rowToPresentation(env, r))
    );
    const hit = items.find((p) => p.presentation_order === String(order));
    if (hit) return hit;
  }
  return null;
}

async function getSession(env, sessionId) {
  const row = await mustDB(env)
    .prepare("SELECT * FROM sessions WHERE id = ?")
    .bind(sessionId)
    .first();
  if (!row) return null;
  const s = rowToSession(row);
  s.presentations = await getAllPresentations(env, sessionId);
  return s;
}

async function setPresentationTags(env, presentationId, tagNames) {
  const seen = new Set();
  const uniq = [];
  for (const n of tagNames || []) {
    const t = String(n || "").trim();
    if (t && !seen.has(t)) {
      seen.add(t);
      uniq.push(t);
    }
  }
  const db = mustDB(env);
  await db
    .prepare("DELETE FROM presentation_tags WHERE presentation_id = ?")
    .bind(presentationId)
    .run();
  for (const name of uniq) {
    await db.prepare("INSERT OR IGNORE INTO tags (name) VALUES (?)").bind(name).run();
    const tag = await db.prepare("SELECT id FROM tags WHERE name = ?").bind(name).first();
    if (tag) {
      await db
        .prepare("INSERT OR IGNORE INTO presentation_tags (presentation_id, tag_id) VALUES (?, ?)")
        .bind(presentationId, tag.id)
        .run();
    }
  }
}

/* ---------- HTML templates ---------- */

function headerHtml(active) {
  return `<header id="site-header"><div class="header-inner"><span class="logo">LT Share</span><nav class="header-nav"><a href="/list">List</a></nav></div></header>`;
}

function adminHeaderHtml(passwordSet) {
  return `<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="/list">List</a><a href="/">Top</a>${passwordSet ? `<a href="/admin/logout">Logout</a>` : ``}</nav></div></header>`;
}

function pageIndex(p) {
  const tags =
    p.tags.length > 0
      ? p.tags.map((t) => `<span>#${esc(t)}</span>`).join("")
      : `<span class="no-tag">タグ未設定</span>`;
  return `<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${esc(p.title)} | LT Share</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
${headerHtml()}
<div id="object-wrapper">
  <object id="slide" data="${esc(p.pdf_url)}"></object>
</div>
<div id="title"><h1 id="title-name">${esc(p.title)}</h1><div id="title-tag">${tags}</div></div>
<div id="other"><div id="person"><span id="name">${esc(p.presenter_name)}</span><span>${esc(p.grade_display)}</span></div><div id="date"><span>${esc(p.session.name)}</span><span>${esc(p.session.event_date_display)}</span></div></div>
<hr>
<div id="detail">
  <h3>Comment</h3>
  <p id="comment">${esc(p.comment)}</p>
</div>
</body>
</html>`;
}

function pageList(presentations, sessions, currentSession, searchQuery = "", hasFilter = false) {
  const opts = sessions
    .map(
      (s) =>
        `<option value="${s.id}"${currentSession && currentSession.id === s.id ? " selected" : ""}>${esc(s.name)}</option>`
    )
    .join("\n");
  const rows =
    presentations.length > 0
      ? presentations
          .map(
            (p) => `<tr>
          <td class="col-name">${esc(p.presenter_name)}</td>
          <td class="col-title"><a href="${esc(p.detail_url)}">${esc(p.title)}</a></td>
          <td class="col-genre">${
            p.tags.length > 0
              ? p.tags.map((t) => `<span class="tag">#${esc(t)}</span>`).join("")
              : `<span class="no-tag">-</span>`
          }</td>
          <td class="col-session">${esc(p.session.name)}</td>
        </tr>`
          )
          .join("\n")
      : `<tr class="empty-row"><td colspan="4">条件に一致する発表がありません。</td></tr>`;
  return `<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>List | LT Share</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
${headerHtml()}
<main class="list-container">
  <h1>発表一覧</h1>
  <form method="get" action="/list" id="session-filter" class="list-filter">
    <div class="filter-row">
      <label for="session_id">開催回:</label>
      <select name="session_id" id="session_id" onchange="this.form.submit()">
        <option value="">すべて</option>
        ${opts}
      </select>
      <span class="list-count">${presentations.length}件</span>
    </div>
    <div class="filter-row search-row">
      <label for="q">タイトル・ジャンル:</label>
      <input type="search" name="q" id="q" value="${esc(searchQuery)}" placeholder="タイトル・ジャンルで検索">
      <button type="submit" class="btn-search">検索</button>
      ${hasFilter ? `<a href="/list" class="filter-clear">クリア</a>` : ``}
    </div>
  </form>
  <div class="table-wrapper">
    <table class="list-table">
      <thead>
        <tr>
          <th class="col-name">名前</th>
          <th class="col-title">タイトル</th>
          <th class="col-genre">ジャンル</th>
          <th class="col-session">発表回</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  </div>
</main>
</body>
</html>`;
}

function pageAdminLogin(url, error) {
  const next = url.searchParams.get("next") || "/admin/";
  return `<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>管理者ログイン | LT Share</title>
  <link rel="stylesheet" href="/static/style.css">
  <link rel="stylesheet" href="/static/admin.css">
</head>
<body>
<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="/list">List</a></nav></div></header>
<main class="admin-container narrow">
  <h1>管理者ログイン</h1>
  ${error ? `<p class="flash error">${esc(error)}</p>` : ``}
  ${flashHtml(url)}
  <form method="post" action="/admin/login" class="card-form">
    <input type="hidden" name="next" value="${esc(next)}">
    <label>パスワード
      <input type="password" name="password" autofocus required autocomplete="current-password">
    </label>
    <button type="submit" class="btn primary">ログイン</button>
  </form>
  <p class="hint">wrangler secret の <code>ADMIN_PASSWORD</code> に設定した値を入力してください。</p>
</main>
</body>
</html>`;
}

function pageAdminDashboard(url, presentations, sessions, passwordSet) {
  const sessOpts = sessions
    .map((s) => `<option value="${s.id}">${esc(s.name)} (${esc(s.event_date_display)})</option>`)
    .join("\n");
  const sessRows = sessions
    .map(
      (s) => `<tr>
        <form method="post" action="/admin/sessions/${s.id}/update">
          <td>${s.id}</td>
          <td><input name="name" value="${esc(s.name)}" required></td>
          <td><input type="date" name="event_date" value="${esc(s.event_date)}"></td>
          <td>${s.presentation_count}</td>
          <td class="btn-row">
            <button type="submit" class="btn small primary">更新</button>
            <button type="submit" class="btn small danger" formaction="/admin/sessions/${s.id}/delete" onclick="return confirm('開催回「${esc(s.name)}」を削除しますか？');">削除</button>
          </td>
        </form>
      </tr>`
    )
    .join("\n");
  return `<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>管理画面 | LT Share</title>
  <link rel="stylesheet" href="/static/style.css">
  <link rel="stylesheet" href="/static/admin.css">
</head>
<body>
${adminHeaderHtml(passwordSet)}
<main class="admin-container">
  <div class="admin-title-row">
    <h1>DB管理</h1>
    ${!passwordSet ? `<p class="flash warning">⚠ ADMIN_PASSWORD 未設定のため保護なしで公開されています。本番では必ず wrangler secret put ADMIN_PASSWORD を設定してください。</p>` : ``}
  </div>
  ${flashHtml(url)}
  <div class="stats">
    <div class="stat">発表 <strong>${presentations.length}</strong> 件</div>
    <div class="stat">開催回 <strong>${sessions.length}</strong> 件</div>
  </div>
  <nav class="admin-tabs">
    <a href="#presentations">発表の追加</a>
    <a href="#sessions">開催回 (${sessions.length})</a>
    <a href="/admin/presentations">発表一覧・編集 (${presentations.length}) →</a>
  </nav>
  <section id="presentations" class="admin-section">
    <h2>発表の追加</h2>
    <form method="post" action="/admin/presentations/create" class="card-form grid">
      <label>タイトル *<input name="title" required maxlength="200" placeholder="例: Docker入門"></label>
      <label>発表者<input name="presenter_name" maxlength="100" placeholder="例: I.B."></label>
      <label>学年
        <select name="grade">
          <option value="">(未設定)</option>
          <option value="1">1</option>
          <option value="2">2</option>
          <option value="3">3</option>
          <option value="4">4</option>
        </select>
      </label>
      <label>開催回 *
        <select name="session_id" required>
          <option value="">選択してください</option>
          ${sessOpts}
        </select>
      </label>
      <label>発表順 *<input name="presentation_order" type="number" min="1" step="1" required placeholder="例: 3"></label>
      <label class="full">コメント<textarea name="comment" rows="2" placeholder="発表の概要・補足"></textarea></label>
      <p class="full hint">PDFキー（例: 第1回＋3 → Slides/1/3.pdf）は開催回と発表順から自動生成されます。</p>
      <label class="full">タグ (カンマ/スペース区切り)<input name="tags" placeholder="例: Linux, Docker"></label>
      <div class="full"><button type="submit" class="btn primary">発表を追加</button></div>
    </form>
  </section>
  <section id="sessions" class="admin-section">
    <h2>開催回</h2>
    <p class="hint">一番上の行から新規追加、各行で編集・削除できます。</p>
    <table class="admin-table">
      <thead><tr><th>ID</th><th>開催回名</th><th>開催日</th><th>発表数</th><th>操作</th></tr></thead>
      <tbody>
      <tr class="new-row">
        <form method="post" action="/admin/sessions/create">
          <td>新規</td>
          <td><input name="name" required placeholder="例: 第3回"></td>
          <td><input type="date" name="event_date"></td>
          <td>—</td>
          <td><button type="submit" class="btn small primary">追加</button></td>
        </form>
      </tr>
      ${sessRows}
      </tbody>
    </table>
  </section>
</main>
</body>
</html>`;
}

function pageAdminPresentations(url, presentations, passwordSet) {
  const rows =
    presentations.length > 0
      ? presentations
          .map(
            (p) => `<tr>
      <td>${esc(p.session.name)}</td>
      <td>${esc(p.presentation_order)}</td>
      <td><a href="/admin/presentations/${p.id}/edit">${esc(p.title)}</a></td>
    </tr>`
          )
          .join("\n")
      : `<tr><td colspan="3">発表データがありません。</td></tr>`;
  return `<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>発表一覧・編集 | LT Share Admin</title>
  <link rel="stylesheet" href="/static/style.css">
  <link rel="stylesheet" href="/static/admin.css">
</head>
<body>
<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="/admin/">管理トップ</a><a href="/list">List</a>${passwordSet ? `<a href="/admin/logout">Logout</a>` : ``}</nav></div></header>
<main class="admin-container">
  <h1>発表一覧・編集</h1>
  <p class="hint">タイトルを選択すると編集ページに移動します。新規追加は<a href="/admin/#presentations">管理トップのフォーム</a>から行えます。</p>
  ${flashHtml(url)}
  <table class="admin-table">
    <thead><tr><th>発表回</th><th>発表順</th><th>タイトル</th></tr></thead>
    <tbody>
    ${rows}
    </tbody>
  </table>
</main>
</body>
</html>`;
}

function pageAdminEdit(url, p, sessions, passwordSet) {
  const gradeSel = (v) =>
    `<option value="${v}"${p.grade === v ? " selected" : ""}>${v || "(未設定)"}</option>`;
  const sessOpts = sessions
    .map(
      (s) =>
        `<option value="${s.id}"${p.session_id === s.id ? " selected" : ""}>${esc(s.name)} (${esc(s.event_date_display)})</option>`
    )
    .join("\n");
  return `<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>発表の編集 #${p.id} | LT Share Admin</title>
  <link rel="stylesheet" href="/static/style.css">
  <link rel="stylesheet" href="/static/admin.css">
</head>
<body>
<header id="site-header"><div class="header-inner"><span class="logo">LT Share <span class="admin-badge">Admin</span></span><nav class="header-nav"><a href="/admin/presentations">発表一覧</a><a href="/admin/">管理トップ</a>${passwordSet ? `<a href="/admin/logout">Logout</a>` : ``}</nav></div></header>
<main class="admin-container">
  <h1>発表の編集 #${p.id}</h1>
  ${flashHtml(url)}
  <form method="post" action="/admin/presentations/${p.id}/update" class="card-form grid">
    <label>タイトル *<input name="title" value="${esc(p.title)}" required maxlength="200"></label>
    <label>発表者<input name="presenter_name" value="${esc(p.presenter_name)}" maxlength="100"></label>
    <label>学年
      <select name="grade">
        ${gradeSel("")}
        ${gradeSel("1")}
        ${gradeSel("2")}
        ${gradeSel("3")}
        ${gradeSel("4")}
      </select>
    </label>
    <label>開催回 *
      <select name="session_id" required>
        ${sessOpts}
      </select>
    </label>
    <label class="full">発表順 *<input name="presentation_order" type="number" min="1" step="1" required value="${esc(p.presentation_order)}" placeholder="例: 3"></label>
    <label class="full">コメント<textarea name="comment" rows="3">${esc(p.comment)}</textarea></label>
    <label class="full">タグ (カンマ/スペース区切り)<input name="tags" value="${esc(p.tags.join(", "))}" placeholder="例: Linux, Docker"></label>
    <p class="full hint">PDFキーは開催回と発表順から自動生成されます。現在のキー: <code>${esc(p.pdf_key)}</code></p>
    <p class="full hint">PDFの差し替えは <code>POST /api/presentations/${p.id}/pdf</code> (form-data <code>file</code>) で行えます。</p>
    <div class="full btn-row">
      <button type="submit" class="btn primary">更新</button>
      <a class="btn" href="${esc(p.detail_url)}" target="_blank" rel="noopener">表示確認</a>
      <a class="btn" href="/admin/presentations">一覧に戻る</a>
      <button type="submit" class="btn danger" formaction="/admin/presentations/${p.id}/delete" onclick="return confirm('発表 #${p.id}「${esc(p.title)}」を削除しますか？');">削除</button>
    </div>
  </form>
</main>
</body>
</html>`;
}

/* ---------- route handlers ---------- */

async function handleIndex(request, env, url) {
  const idParam = url.searchParams.get("id");
  const pid = idParam ? parseInt(idParam, 10) : null;
  if (!Number.isFinite(pid)) {
    // ルートは一覧ページを表示
    return handleList(request, env, url);
  }
  // 旧形式 ?id= は正規URL (/{発表回}/{発表順}) へ誘導
  try {
    const p = await getPresentation(env, pid);
    if (!p) return html("<h1>発表データが見つかりません</h1>", 404);
    if (p.detail_url && p.detail_url.startsWith("/") && !p.detail_url.startsWith("/?")) {
      return redirect(p.detail_url, 302);
    }
    return html(pageIndex(p));
  } catch (e) {
    console.error(e);
    return html(`<h1>データ取得に失敗しました</h1><p>${esc(String(e))}</p>`, 502);
  }
}

async function handlePresentationDetail(request, env, roundNum, orderNum) {
  try {
    const p = await getPresentationByRoundOrder(env, roundNum, orderNum);
    if (!p) return html("<h1>発表データが見つかりません</h1>", 404);
    return html(pageIndex(p));
  } catch (e) {
    console.error(e);
    return html(`<h1>データ取得に失敗しました</h1><p>${esc(String(e))}</p>`, 502);
  }
}

async function handleList(request, env, url) {
  const sidParam = url.searchParams.get("session_id");
  const sessionId = sidParam ? parseInt(sidParam, 10) : null;
  let searchQuery = (url.searchParams.get("q") || "").trim();
  if (!searchQuery) {
    searchQuery = [url.searchParams.get("title") || "", url.searchParams.get("genre") || ""]
      .map((s) => s.trim())
      .filter(Boolean)
      .join(" ");
  }
  try {
    const presentations = await getAllPresentations(
      env,
      Number.isFinite(sessionId) ? sessionId : null,
      "",
      "",
      searchQuery
    );
    const sessions = await getSessions(env);
    const current =
      Number.isFinite(sessionId) ? sessions.find((s) => s.id === sessionId) || null : null;
    const hasFilter = Boolean(current || searchQuery);
    return html(pageList(presentations, sessions, current, searchQuery, hasFilter));
  } catch (e) {
    console.error(e);
    return html(`<h1>データ取得に失敗しました</h1><p>${esc(String(e))}</p>`, 502);
  }
}

async function handleSlideRedirect(request, env, id) {
  try {
    const p = await getPresentation(env, id);
    if (!p || !p.pdf_key) return new Response("スライドが見つかりません", { status: 404 });
    return redirect(pdfUrlFor(env, p.pdf_key), 302);
  } catch (e) {
    console.error(e);
    return new Response(`データ取得に失敗しました: ${e}`, { status: 502 });
  }
}

async function handleR2Serve(request, env, key) {
  if (!env.SLIDES) return new Response("R2 binding `SLIDES` 未設定", { status: 503 });
  const clean = decodeURIComponent(key).replace(/^\/+/, "");
  if (!clean || clean.includes("..")) return new Response("not found", { status: 404 });
  const obj = await env.SLIDES.get(clean);
  if (!obj) return new Response("not found", { status: 404 });
  const headers = new Headers();
  headers.set("content-type", obj.httpMetadata?.contentType || "application/pdf");
  headers.set("cache-control", "public, max-age=31536000, immutable");
  if (obj.httpEtag) headers.set("etag", obj.httpEtag);
  if (obj.size) headers.set("content-length", String(obj.size));
  // Range対応はそのまま透過させる
  return new Response(obj.body, { headers });
}

async function handleHealth(env) {
  let d1 = false;
  try {
    if (env.DB) {
      await env.DB.prepare("SELECT 1").first();
      d1 = true;
    }
  } catch {
    d1 = false;
  }
  return json({
    d1_enabled: d1,
    r2_mode: env.R2_PUBLIC_BASE_URL ? "public" : env.SLIDES ? "r2-binding" : "none",
    r2_bucket: "lt-pdf",
    r2_public_base_url: env.R2_PUBLIC_BASE_URL || "",
  });
}

/* ---------- main fetch ---------- */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    // 静的アセットはプラットフォームに委譲 (binding ASSETS があれば)
    if (path.startsWith("/static/") && env.ASSETS) {
      try {
        return await env.ASSETS.fetch(request);
      } catch {
        return new Response("not found", { status: 404 });
      }
    }
    if (path === "/favicon.ico") return new Response(null, { status: 204 });

    // ---- 公開ページ ----
    if (path === "/" && method === "GET") return handleIndex(request, env, url);
    if (path === "/list" && method === "GET") return handleList(request, env, url);
    if (path === "/health" && method === "GET") return handleHealth(env);

    let m;
    if ((m = path.match(/^\/slides\/(\d+)$/)) && method === "GET") {
      return handleSlideRedirect(request, env, parseInt(m[1], 10));
    }
    // 個別発表ページ: /{発表回番号}/{発表順番号} (例: /1/3)
    if ((m = path.match(/^\/(\d+)\/(\d+)\/?$/)) && method === "GET") {
      return handlePresentationDetail(request, env, parseInt(m[1], 10), parseInt(m[2], 10));
    }
    if (path.startsWith("/r2/") && method === "GET") {
      return handleR2Serve(request, env, path.slice(4));
    }
    // 旧Flask互換: /data/<key> はR2へフォールバック
    if (path.startsWith("/data/") && method === "GET") {
      const key = path.slice(6);
      if (env.R2_PUBLIC_BASE_URL) {
        return redirect(`${env.R2_PUBLIC_BASE_URL.replace(/\/+$/, "")}/${key}`, 302);
      }
      return handleR2Serve(request, env, key);
    }

    // ---- JSON API (読み取りは公開) ----
    if (path === "/api/sessions" && method === "GET") {
      try {
        return json(await getSessions(env));
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }
    if ((m = path.match(/^\/api\/sessions\/(\d+)$/)) && method === "GET") {
      try {
        const s = await getSession(env, parseInt(m[1], 10));
        if (!s) return json({ error: "not found" }, 404);
        return json(s);
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }
    if (path === "/api/sessions" && method === "POST") {
      if (env.ADMIN_PASSWORD && !(await isAdmin(request, env)))
        return json({ error: "admin login required" }, 401);
      let data = {};
      try {
        data = await request.json();
      } catch {
        data = {};
      }
      const name = String(data.name || "").trim();
      const eventDate = String(data.event_date || "").trim();
      if (!name) return json({ error: "name is required" }, 400);
      try {
        const existing = await mustDB(env)
          .prepare("SELECT * FROM sessions WHERE name = ?")
          .bind(name)
          .first();
        if (existing) {
          const s = rowToSession(existing);
          s.created = false;
          return json(s, 200);
        }
        await mustDB(env)
          .prepare("INSERT INTO sessions (name, event_date) VALUES (?, ?)")
          .bind(name, eventDate)
          .run();
        const row = await mustDB(env)
          .prepare("SELECT * FROM sessions WHERE name = ?")
          .bind(name)
          .first();
        const s = rowToSession(row);
        s.created = true;
        return json(s, 201);
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }
    if (path === "/api/presentations" && method === "GET") {
      const sidParam = url.searchParams.get("session_id");
      const sid = sidParam ? parseInt(sidParam, 10) : null;
      const titleQ = (url.searchParams.get("title") || "").trim();
      const genreQ = (url.searchParams.get("genre") || "").trim();
      const keywordQ = (url.searchParams.get("q") || "").trim();
      try {
        return json(
          await getAllPresentations(env, Number.isFinite(sid) ? sid : null, titleQ, genreQ, keywordQ)
        );
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }
    if ((m = path.match(/^\/api\/presentations\/(\d+)$/)) && method === "GET") {
      try {
        const p = await getPresentation(env, parseInt(m[1], 10));
        if (!p) return json({ error: "not found" }, 404);
        return json(p);
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }
    if ((m = path.match(/^\/api\/presentations\/(\d+)\/session$/)) && method === "PUT") {
      if (env.ADMIN_PASSWORD && !(await isAdmin(request, env)))
        return json({ error: "admin login required" }, 401);
      const pid = parseInt(m[1], 10);
      let data = {};
      try {
        data = await request.json();
      } catch {
        data = {};
      }
      if (data.session_id === undefined || data.session_id === null)
        return json({ error: "session_id is required" }, 400);
      try {
        if (!(await getPresentation(env, pid)))
          return json({ error: "presentation not found" }, 404);
        const target = await mustDB(env)
          .prepare("SELECT * FROM sessions WHERE id = ?")
          .bind(data.session_id)
          .first();
        if (!target) return json({ error: "session not found" }, 404);
        await mustDB(env)
          .prepare("UPDATE presentations SET session_id = ? WHERE id = ?")
          .bind(data.session_id, pid)
          .run();
        return json(await getPresentation(env, pid));
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }
    if ((m = path.match(/^\/api\/presentations\/(\d+)\/pdf$/)) && method === "POST") {
      if (!(await isAdmin(request, env))) return json({ error: "admin login required" }, 401);
      if (!env.SLIDES) return json({ error: "R2 binding not configured" }, 503);
      const pid = parseInt(m[1], 10);
      let form;
      try {
        form = await request.formData();
      } catch {
        return json({ error: "multipart form-data required" }, 400);
      }
      const file = form.get("file");
      if (!file || typeof file === "string" || !file.size)
        return json({ error: "file is required" }, 400);
      try {
        const current = await getPresentation(env, pid);
        if (!current) return json({ error: "not found" }, 404);
        const key = String(form.get("key") || current.pdf_key || `${pid}.pdf`).replace(
          /^\/+/,
          ""
        );
        await env.SLIDES.put(key, await file.arrayBuffer(), {
          httpMetadata: { contentType: "application/pdf" },
        });
        await mustDB(env)
          .prepare("UPDATE presentations SET pdf_key = ? WHERE id = ?")
          .bind(key, pid)
          .run();
        return json({ id: pid, pdf_key: key, pdf_url: pdfUrlFor(env, key) });
      } catch (e) {
        console.error(e);
        return json({ error: String(e) }, 502);
      }
    }

    // ---- 管理画面 ----
    if (path === "/admin/login" && method === "GET") {
      if (!env.ADMIN_PASSWORD) return redirect("/admin/");
      if (await isAdmin(request, env)) return redirect("/admin/");
      return html(pageAdminLogin(url, null));
    }
    if (path === "/admin/login" && method === "POST") {
      if (!env.ADMIN_PASSWORD) return redirect("/admin/");
      const form = await request.formData();
      const password = String(form.get("password") || "");
      const nextUrl = String(form.get("next") || "/admin/");
      const safeNext = nextUrl.startsWith("/") ? nextUrl : "/admin/";
      if (password === env.ADMIN_PASSWORD) {
        const token = await expectedAdminToken(env);
        return redirect(safeNext, 302, {
          "set-cookie": adminCookieHeader(token, 60 * 60 * 24 * 7),
        });
      }
      return html(pageAdminLogin(url, "パスワードが違います"), 401);
    }
    if ((path === "/admin/logout") && (method === "GET" || method === "POST")) {
      const back = redirectWithFlash("/admin/login", "ログアウトしました", "info");
      const headers = new Headers(back.headers);
      headers.set("set-cookie", adminCookieHeader("", 0));
      return new Response(null, { status: back.status, headers });
    }
    if ((path === "/admin" || path === "/admin/") && method === "GET") {
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent("/admin/")}`);
      try {
        const presentations = await getAllPresentations(env);
        const sessions = await getSessions(env);
        return html(pageAdminDashboard(url, presentations, sessions, Boolean(env.ADMIN_PASSWORD)));
      } catch (e) {
        console.error(e);
        return html(`<h1>データ取得に失敗しました</h1><p>${esc(String(e))}</p>`, 502);
      }
    }
    if ((path === "/admin/presentations" || path === "/admin/presentations/") && method === "GET") {
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent("/admin/presentations")}`);
      try {
        const presentations = await getAllPresentations(env);
        const sessions = await getSessions(env);
        const rank = new Map(sessions.map((s, i) => [s.id, i]));
        presentations.sort((a, b) => {
          const ra = rank.has(a.session_id) ? rank.get(a.session_id) : rank.size;
          const rb = rank.has(b.session_id) ? rank.get(b.session_id) : rank.size;
          if (ra !== rb) return ra - rb;
          const oa = /^\d+$/.test(a.presentation_order || "") ? parseInt(a.presentation_order, 10) : 1e9;
          const ob = /^\d+$/.test(b.presentation_order || "") ? parseInt(b.presentation_order, 10) : 1e9;
          if (oa !== ob) return oa - ob;
          return a.id - b.id;
        });
        return html(pageAdminPresentations(url, presentations, Boolean(env.ADMIN_PASSWORD)));
      } catch (e) {
        console.error(e);
        return html(`<h1>データ取得に失敗しました</h1><p>${esc(String(e))}</p>`, 502);
      }
    }
    if ((m = path.match(/^\/admin\/presentations\/(\d+)\/edit$/)) && method === "GET") {
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent(path)}`);
      const pid = parseInt(m[1], 10);
      try {
        const p = await getPresentation(env, pid);
        if (!p) return html("<h1>発表データが見つかりません</h1>", 404);
        const sessions = await getSessions(env);
        return html(pageAdminEdit(url, p, sessions, Boolean(env.ADMIN_PASSWORD)));
      } catch (e) {
        console.error(e);
        return html(`<h1>データ取得に失敗しました</h1><p>${esc(String(e))}</p>`, 502);
      }
    }
    if (path === "/admin/presentations/create" && method === "POST") {
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent("/admin/")}`);
      const form = await request.formData();
      const title = String(form.get("title") || "").trim();
      const presenterName = String(form.get("presenter_name") || "").trim();
      const grade = normalizeGradeInput(form.get("grade"));
      const sessionId = form.get("session_id") ? parseInt(form.get("session_id"), 10) : null;
      const orderRaw = String(form.get("presentation_order") || "").trim();
      const comment = String(form.get("comment") || "").trim();
      const tagNames = parseTagInput(form.get("tags"));
      const fail = (msg) => redirectWithFlash("/admin/#presentations", msg, "error");
      if (!title) return fail("タイトルは必須です");
      if (grade === null) return fail("学年は1〜4の数字で指定してください");
      if (!Number.isFinite(sessionId)) return fail("開催回を選択してください（PDFキー生成に必要です）");
      if (!/^\d+$/.test(orderRaw) || parseInt(orderRaw, 10) < 1)
        return fail("発表順は1以上の数字で指定してください");
      try {
        const sess = await mustDB(env)
          .prepare("SELECT * FROM sessions WHERE id = ?")
          .bind(sessionId)
          .first();
        if (!sess) return fail("指定の開催回が存在しません");
        const pdfKey = buildPdfKey(sess, parseInt(orderRaw, 10));
        await mustDB(env)
          .prepare(
            "INSERT INTO presentations (title, presenter_name, grade, session_id, comment, pdf_key) VALUES (?, ?, ?, ?, ?, ?)"
          )
          .bind(title, presenterName, grade, sessionId, comment, pdfKey)
          .run();
        const newest = await mustDB(env)
          .prepare("SELECT id FROM presentations ORDER BY id DESC LIMIT 1")
          .first();
        if (newest) await setPresentationTags(env, newest.id, tagNames);
        return redirectWithFlash("/admin/#presentations", `発表「${title}」を追加しました`, "success");
      } catch (e) {
        console.error(e);
        return fail(`追加に失敗しました: ${e}`);
      }
    }
    if ((m = path.match(/^\/admin\/presentations\/(\d+)\/update$/)) && method === "POST") {
      const pid = parseInt(m[1], 10);
      const editUrl = `/admin/presentations/${pid}/edit`;
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent(editUrl)}`);
      const form = await request.formData();
      const title = String(form.get("title") || "").trim();
      const presenterName = String(form.get("presenter_name") || "").trim();
      const grade = normalizeGradeInput(form.get("grade"));
      const rawSid = String(form.get("session_id") || "").trim();
      const sessionId = rawSid ? parseInt(rawSid, 10) : null;
      const comment = String(form.get("comment") || "").trim();
      const orderRaw = String(form.get("presentation_order") || "").trim();
      const tagNames = parseTagInput(form.get("tags"));
      const fail = (msg) => redirectWithFlash(editUrl, msg, "error");
      if (!title) return fail("タイトルは必須です");
      if (grade === null) return fail("学年は1〜4の数字で指定してください");
      if (!/^\d+$/.test(orderRaw) || parseInt(orderRaw, 10) < 1)
        return fail("発表順は1以上の数字で指定してください");
      if (!Number.isFinite(sessionId)) return fail("開催回を選択してください");
      try {
        const sess = await mustDB(env)
          .prepare("SELECT * FROM sessions WHERE id = ?")
          .bind(sessionId)
          .first();
        if (!sess) return fail("指定の開催回が存在しません");
        const pdfKey = buildPdfKey(sess, parseInt(orderRaw, 10));
        await mustDB(env)
          .prepare(
            "UPDATE presentations SET title=?, presenter_name=?, grade=?, session_id=?, comment=?, pdf_key=? WHERE id=?"
          )
          .bind(title, presenterName, grade, sessionId, comment, pdfKey, pid)
          .run();
        await setPresentationTags(env, pid, tagNames);
        return redirectWithFlash(editUrl, `発表 #${pid} を更新しました`, "success");
      } catch (e) {
        console.error(e);
        return fail(`更新に失敗しました: ${e}`);
      }
    }
    if ((m = path.match(/^\/admin\/presentations\/(\d+)\/delete$/)) && method === "POST") {
      const pid = parseInt(m[1], 10);
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent("/admin/presentations")}`);
      try {
        await mustDB(env)
          .prepare("DELETE FROM presentation_tags WHERE presentation_id = ?")
          .bind(pid)
          .run();
        await mustDB(env).prepare("DELETE FROM presentations WHERE id = ?").bind(pid).run();
        return redirectWithFlash("/admin/presentations", `発表 #${pid} を削除しました`, "success");
      } catch (e) {
        console.error(e);
        return redirectWithFlash("/admin/presentations", `削除に失敗しました: ${e}`, "error");
      }
    }
    if (path === "/admin/sessions/create" && method === "POST") {
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent("/admin/")}`);
      const form = await request.formData();
      const name = String(form.get("name") || "").trim();
      const eventDate = String(form.get("event_date") || "").trim();
      if (!name) return redirectWithFlash("/admin/#sessions", "開催回名は必須です", "error");
      try {
        await mustDB(env)
          .prepare("INSERT INTO sessions (name, event_date) VALUES (?, ?)")
          .bind(name, eventDate)
          .run();
        return redirectWithFlash("/admin/#sessions", `開催回「${name}」を追加しました`, "success");
      } catch (e) {
        console.error(e);
        return redirectWithFlash(
          "/admin/#sessions",
          `追加に失敗しました (同名の開催回がある可能性があります): ${e}`,
          "error"
        );
      }
    }
    if ((m = path.match(/^\/admin\/sessions\/(\d+)\/update$/)) && method === "POST") {
      const sid = parseInt(m[1], 10);
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent("/admin/")}`);
      const form = await request.formData();
      const name = String(form.get("name") || "").trim();
      const eventDate = String(form.get("event_date") || "").trim();
      if (!name) return redirectWithFlash("/admin/#sessions", "開催回名は必須です", "error");
      try {
        await mustDB(env)
          .prepare("UPDATE sessions SET name=?, event_date=? WHERE id=?")
          .bind(name, eventDate, sid)
          .run();
        return redirectWithFlash("/admin/#sessions", `開催回 #${sid} を更新しました`, "success");
      } catch (e) {
        console.error(e);
        return redirectWithFlash("/admin/#sessions", `更新に失敗しました: ${e}`, "error");
      }
    }
    if ((m = path.match(/^\/admin\/sessions\/(\d+)\/delete$/)) && method === "POST") {
      const sid = parseInt(m[1], 10);
      if (!(await isAdmin(request, env)))
        return redirect(`/admin/login?next=${encodeURIComponent("/admin/")}`);
      try {
        const linked = await mustDB(env)
          .prepare("SELECT COUNT(*) AS c FROM presentations WHERE session_id = ?")
          .bind(sid)
          .first();
        if (linked && linked.c) {
          return redirectWithFlash(
            "/admin/#sessions",
            `開催回 #${sid} は発表が紐づいているため削除できません (発表を先に移動・削除してください)`,
            "error"
          );
        }
        await mustDB(env).prepare("DELETE FROM sessions WHERE id = ?").bind(sid).run();
        return redirectWithFlash("/admin/#sessions", `開催回 #${sid} を削除しました`, "success");
      } catch (e) {
        console.error(e);
        return redirectWithFlash("/admin/#sessions", `削除に失敗しました: ${e}`, "error");
      }
    }

    return new Response("not found", { status: 404 });
  },
};
