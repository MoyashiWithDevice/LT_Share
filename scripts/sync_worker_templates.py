"""templates/*.html を src/worker.py に埋め込む (Python Workers対策)。

背景: Python Workers本番は templates/ が同梱されないため、
src/worker.py 内の _EMBEDDED_TEMPLATES (DictLoader) がフォールバックになる。
templates/ が正本。変更後は必ず本スクリプトで再生成すること。

使い方:
    uv run python scripts/sync_worker_templates.py
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "templates"
WORKER = ROOT / "src" / "worker.py"

NAMES = [
    "index.html",
    "list.html",
    "admin.html",
    "admin_login.html",
    "admin_presentations.html",
    "admin_presentation_edit.html",
]


def main():
    d = {}
    for name in NAMES:
        d[name] = (TPL_DIR / name).read_text(encoding="utf-8")
    literal = "{\n" + "\n".join(f"{n!r}: {v!r}," for n, v in d.items()) + "\n}"
    src = WORKER.read_text(encoding="utf-8")
    start_marker = "_EMBEDDED_TEMPLATES = {"
    idx = src.find(start_marker)
    assert idx != -1, "src/worker.py に _EMBEDDED_TEMPLATES が見つかりません"
    # 対応する閉じ括弧を探す: literal生成時と同じ形式なので、"\n}\ntry:" を探す
    end_marker = "\n}\ntry:\n    app.jinja_loader"
    end_idx = src.find(end_marker, idx)
    assert end_idx != -1, "埋め込みブロック終端が見つかりません"
    new_block = "_EMBEDDED_TEMPLATES = " + literal + "\ntry:\n    app.jinja_loader"
    src = src[:idx] + new_block + src[end_idx + len("\n}\ntry:\n    app.jinja_loader"):]
    WORKER.write_text(src, encoding="utf-8")
    print(f"inlined {len(literal)} chars into {WORKER}")


if __name__ == "__main__":
    main()
