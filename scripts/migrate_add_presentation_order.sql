-- 発表順の独立保持マイグレーション (PDFなしでも発表順が消えないようにする)
-- ローカル: python scripts/init_local_db.py を再実行するか、下記をdev.dbに適用
--   python3 -c "import sqlite3; con=sqlite3.connect('dev.db'); con.executescript(open('scripts/migrate_add_presentation_order.sql').read()); con.commit()"
-- D1 (本番/開発): wrangler d1 execute lt-share-db --file=./scripts/migrate_add_presentation_order.sql
--                 wrangler d1 execute lt-share-db --remote --file=./scripts/migrate_add_presentation_order.sql
-- ※ 既存行の値はアプリ側のバックフィル (pdf_key から逆算) で補完すること。
--    下のUPDATEは命名規則 Slides/{回}/{順}.pdf に従っている行のみを対象とする。

ALTER TABLE presentations ADD COLUMN presentation_order INTEGER NOT NULL DEFAULT 0;
