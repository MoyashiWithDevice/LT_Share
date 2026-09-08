-- Cloudflare D1 schema for LT Share
-- 使い方:
--   wrangler d1 execute lt-share-db --file=./schema.sql
--   または wrangler d1 execute lt-share-db --remote --file=./schema.sql
--
-- 設計:
--   sessions: LT会の開催回と日付を管理 (例: 第1回 / 2026-09-04)
--   presentations: 各発表。session_id で sessions に紐づく。日付は sessions からJOIN取得。
--   tags / presentation_tags: ジャンルタグ (多対多)

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE, -- 例: 第1回
  event_date TEXT NOT NULL DEFAULT '', -- YYYY-MM-DD 形式
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS presentations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  presenter_name TEXT NOT NULL DEFAULT '',
  grade TEXT NOT NULL DEFAULT '',
  session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
  comment TEXT NOT NULL DEFAULT '',
  pdf_key TEXT NOT NULL DEFAULT '', -- Cloudflare R2 のオブジェクトキー。命名規則: Slides/{開催回数字}/{発表順}.pdf (例: Slides/1/1.pdf)。空文字 = PDFなし
  slide_url TEXT NOT NULL DEFAULT '', -- PDFがない場合の外部共有リンク (Google Slides / Canva / Speaker Deck 等)。空文字 = リンクなし
  related_url1 TEXT NOT NULL DEFAULT '', -- 関連リンク1 (参考URL等)。空文字 = なし
  related_url2 TEXT NOT NULL DEFAULT '', -- 関連リンク2。空文字 = なし
  related_url3 TEXT NOT NULL DEFAULT '', -- 関連リンク3。空文字 = なし
  presentation_order INTEGER NOT NULL DEFAULT 0, -- 発表順 (正規URL /{回}/{順} 用。pdf_key とは独立して保持し、PDFなしでも順番が消えないようにする)
  revision INTEGER NOT NULL DEFAULT 0, -- 楽観ロック用カウンタ。更新のたびに+1。古い編集画面からの上書き(発表者名・タグの消失)を検出する
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS presentation_tags (
  presentation_id INTEGER NOT NULL,
  tag_id INTEGER NOT NULL,
  PRIMARY KEY (presentation_id, tag_id),
  FOREIGN KEY (presentation_id) REFERENCES presentations(id) ON DELETE CASCADE,
  FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_presentations_session ON presentations(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(event_date);
CREATE INDEX IF NOT EXISTS idx_presentation_tags_pid ON presentation_tags(presentation_id);
CREATE INDEX IF NOT EXISTS idx_presentation_tags_tid ON presentation_tags(tag_id);
