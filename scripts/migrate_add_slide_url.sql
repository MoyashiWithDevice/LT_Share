-- 資料なし / 外部共有リンク対応マイグレーション
-- ローカル: python3 -c "import sqlite3; con=sqlite3.connect('dev.db'); con.executescript(open('scripts/migrate_add_slide_url.sql').read()); con.commit()"
-- D1 (本番/開発): wrangler d1 execute lt-share-db --file=./scripts/migrate_add_slide_url.sql
--                 wrangler d1 execute lt-share-db --remote --file=./scripts/migrate_add_slide_url.sql

-- presentations.slide_url: PDFがない場合の外部共有リンク (空文字 = リンクなし)
-- 既にカラムがある環境ではエラーになるため、 wrangler 実行時は失敗しても無視してOK。
ALTER TABLE presentations ADD COLUMN slide_url TEXT NOT NULL DEFAULT '';
