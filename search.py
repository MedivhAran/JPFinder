# search.py
import argparse
import sqlite3
from pathlib import Path
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

def ms_to_timestr(ms: int) -> str:
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

_sudachi = dictionary.Dictionary().create()
_mode = sudachi_tokenizer.Tokenizer.SplitMode.C

def tokenize_surface(q: str):
    for m in _sudachi.tokenize(q, _mode):
        w = m.surface().strip()
        if w:
            yield w

def tokenize_reading(q: str):
    for m in _sudachi.tokenize(q, _mode):
        r = (m.reading_form() or m.surface()).strip()
        if r:
            yield r

def build_match_query(query: str):
    s_tokens = [t.replace('"', '').replace("'", "") for t in tokenize_surface(query)]
    r_tokens = [t.replace('"', '').replace("'", "") for t in tokenize_reading(query)]

    parts = []
    if s_tokens:
        # 每个词都限定到 text_tok 列，并用 AND 连接
        parts.append(" AND ".join(f'text_tok:{t}' for t in s_tokens))
    if r_tokens:
        # 每个词都限定到 reading_tok 列，并用 AND 连接
        parts.append(" AND ".join(f'reading_tok:{t}' for t in r_tokens))

    if not parts:
        return None
    # 两组之间用 OR 连接，形成一个统一的 MATCH 字符串
    return " OR ".join(f'({p})' for p in parts if p)

def search(db_path: Path, query: str, topn: int = 20, debug: bool = False):
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    match_expr = build_match_query(query)
    if not match_expr:
        print("Empty query after tokenization.")
        return

    if debug:
        print("MATCH:", match_expr)

    sql = """
    SELECT e.title, e.media_type, e.start_ms, e.end_ms, e.text, e.source_path
    FROM fts
    JOIN entries e ON e.rowid = fts.rowid
    WHERE fts MATCH ?
    ORDER BY bm25(fts)
    LIMIT ?
    """
    params = [match_expr, topn]

    try:
        rows = cur.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # 兼容没有 bm25 函数的环境
        if "no such function: bm25" in str(e).lower():
            sql = """
            SELECT e.title, e.media_type, e.start_ms, e.end_ms, e.text, e.source_path
            FROM fts
            JOIN entries e ON e.rowid = fts.rowid
            WHERE fts MATCH ?
            LIMIT ?
            """
            rows = cur.execute(sql, params).fetchall()
        else:
            raise

    for i, (title, mtype, s, e, text, src) in enumerate(rows, 1):
        print(f"{i:2d}. [{mtype}] {title} {ms_to_timestr(s)}-{ms_to_timestr(e)} | {text}")

    conn.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", type=str, help="要搜索的日语词/短语")
    ap.add_argument("--db", default="data/index.db", help="索引数据库路径")
    ap.add_argument("--top", type=int, default=20, help="返回前N条")
    ap.add_argument("--debug", action="store_true", help="打印 MATCH 表达式")
    args = ap.parse_args()

    search(Path(args.db), args.query, args.top, args.debug)

if __name__ == "__main__":
    main()
