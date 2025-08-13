# build_index.py
import argparse
import json
import sqlite3
import unicodedata
import re
from pathlib import Path
from typing import Iterable, Tuple
from tqdm import tqdm

# Sudachi
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()

# 去掉双向控制等不可见控制字符（你的样例里“‪”就是这类字符）
BIDI_CTRL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
def clean_controls(text: str) -> str:
    return BIDI_CTRL_RE.sub("", text)

def jp_clean(text: str) -> str:
    return clean_controls(nfkc(text))

# Sudachi 初始化（C 模式倾向于短词，适合检索）
_sudachi = dictionary.Dictionary().create()
_mode = sudachi_tokenizer.Tokenizer.SplitMode.C

def tokenize_surface(text: str) -> Iterable[str]:
    for m in _sudachi.tokenize(text, _mode):
        surf = m.surface()
        s = surf.strip()
        if s:
            yield s

def tokenize_reading_kana(text: str) -> Iterable[str]:
    for m in _sudachi.tokenize(text, _mode):
        r = m.reading_form()  # 通常是カタカナ
        if not r or r == "*":
            r = m.surface()
        r = r.strip()
        if r:
            yield r

def ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    # 主表存元数据与原文
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=OFF;

    CREATE TABLE IF NOT EXISTS entries(
        id TEXT UNIQUE,
        media_type TEXT,
        title TEXT,
        episode_or_track TEXT,
        media_path TEXT,
        source_path TEXT,
        start_ms INTEGER,
        end_ms INTEGER,
        text TEXT,
        context_prev TEXT,
        context_next TEXT
    );

    -- FTS5 外部内容表，仅索引分词后的两个字段
    CREATE VIRTUAL TABLE IF NOT EXISTS fts
    USING fts5(
        text_tok,           -- 分词后的原文（空格分隔）
        reading_tok,        -- 分词后的读音（カタカナ）
        content='entries',
        content_rowid='rowid'
    );
    """)
    conn.commit()

def insert_entry(cur: sqlite3.Cursor, row: dict):
    # 插入主表
    cur.execute("""
        INSERT OR IGNORE INTO entries
        (id, media_type, title, episode_or_track, media_path, source_path, start_ms, end_ms, text, context_prev, context_next)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        row["id"], row["media_type"], row.get("title",""), row.get("episode_or_track",""),
        row.get("media_path",""), row.get("source_path",""),
        int(row["start_ms"]), int(row["end_ms"]),
        row["text"], row.get("context_prev",""), row.get("context_next","")
    ))
    # 取刚插入行的 rowid
    cur.execute("SELECT rowid FROM entries WHERE id = ?", (row["id"],))
    rid = cur.fetchone()[0]
    # 计算分词
    text = jp_clean(row["text"])
    text_tok = " ".join(tokenize_surface(text))
    reading_tok = " ".join(tokenize_reading_kana(text))
    # 插入 FTS
    cur.execute("INSERT INTO fts(rowid, text_tok, reading_tok) VALUES (?,?,?)",
                (rid, text_tok, reading_tok))

def build(db_path: Path, jsonl_path: Path):
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    cur = conn.cursor()

    total = 0
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in tqdm(f, desc="Indexing"):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            # 清洗 text（去控制字符）
            obj["text"] = jp_clean(obj.get("text",""))
            obj["context_prev"] = jp_clean(obj.get("context_prev",""))
            obj["context_next"] = jp_clean(obj.get("context_next",""))
            if not obj["text"]:
                continue
            insert_entry(cur, obj)
            total += 1
            if total % 1000 == 0:
                conn.commit()
    conn.commit()
    # 为常用过滤加索引（可选）
    cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_title ON entries(title)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_media_type ON entries(media_type)")
    conn.commit()
    conn.close()
    print(f"Indexed rows: {total}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True, help="parse_media.py 生成的 JSONL 路径")
    ap.add_argument("--db", default="data/index.db", help="输出 SQLite 数据库文件")
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        print(f"JSONL not found: {jsonl_path}")
        return

    build(db_path, jsonl_path)

if __name__ == "__main__":
    main()
