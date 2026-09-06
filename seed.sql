-- Seed data
-- wrangler d1 execute lt-share-db --file=./seed.sql
-- wrangler d1 execute lt-share-db --remote --file=./seed.sql
-- ※ presentations へのINSERTは冪等 (既に1件以上あれば何もしない)

-- 開催回マスタ
INSERT OR IGNORE INTO sessions (name, event_date) VALUES ('第1回', '2026-09-04');

-- 発表 (sessions から回数を紐づけ。日付は sessions 側で管理)
INSERT INTO presentations (title, presenter_name, grade, session_id, comment, pdf_key)
SELECT 'Title', 'I.B.', '3', id, 'hoge', 'Slides/1/3.pdf' FROM sessions WHERE name = '第1回'
AND NOT EXISTS (SELECT 1 FROM presentations);

INSERT OR IGNORE INTO tags (name) VALUES ('Linux'), ('Github'), ('PR');

INSERT OR IGNORE INTO presentation_tags (presentation_id, tag_id)
SELECT (SELECT id FROM presentations ORDER BY id LIMIT 1), id
FROM tags WHERE name IN ('Linux', 'Github', 'PR');
