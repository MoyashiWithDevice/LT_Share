"""ローカル開発用 SQLite 初期化。schema.sql + seed.sql を dev.db に適用する。"""
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(BASE, "schema.sql")
SEED = os.path.join(BASE, "seed.sql")
DB_PATH = os.getenv("LOCAL_DB_PATH", os.path.join(BASE, "dev.db"))


def main():
    with open(SCHEMA, encoding="utf-8") as f:
        schema_sql = f.read()
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(schema_sql)
        if os.path.exists(SEED):
            count = con.execute("SELECT COUNT(*) FROM presentations").fetchone()[0]
            if count == 0:
                with open(SEED, encoding="utf-8") as f:
                    con.executescript(f.read())
                print(f"seed applied -> {DB_PATH}")
            else:
                print(f"skip seed (already {count} rows) -> {DB_PATH}")
        else:
            print(f"schema applied -> {DB_PATH}")
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    main()
