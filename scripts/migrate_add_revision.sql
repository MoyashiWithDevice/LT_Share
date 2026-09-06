-- 楽観ロック用カウンタ追加マイグレーション
-- 古い編集画面からの上書き (発表者名・タグの消失) を検出するために使用。
-- ローカル: python3 -c "import sqlite3; con=sqlite3.connect('dev.db'); con.executescript(open('scripts/migrate_add_revision.sql').read()); con.commit()"
-- D1 (本番/開発): wrangler d1 execute lt-share-db --file=./scripts/migrate_add_revision.sql
--                 wrangler d1 execute lt-share-db --remote --file=./scripts/migrate_add_revision.sql

ALTER TABLE presentations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
