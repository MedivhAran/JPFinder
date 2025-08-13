# play_snippet.py
import argparse
import sqlite3
import subprocess
import shutil
from pathlib import Path
from typing import List, Optional
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

VIDEO_EXTS = [".mkv", ".mp4", ".ts", ".m4v", ".avi", ".mov"]
AUDIO_EXTS = [".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg"]
MEDIA_EXTS = VIDEO_EXTS + AUDIO_EXTS

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
        parts.append(" AND ".join(f'text_tok:{t}' for t in s_tokens))
    if r_tokens:
        parts.append(" AND ".join(f'reading_tok:{t}' for t in r_tokens))
    if not parts:
        return None
    return " OR ".join(f'({p})' for p in parts if p)

def query_db(db_path: Path, query: str, topn: int):
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    match_expr = build_match_query(query)
    if not match_expr:
        print("Empty query after tokenization."); return []
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
    conn.close()
    return rows

def find_media_candidates(source_path: Optional[Path], media_root: Optional[Path]) -> List[Path]:
    """
    根据字幕/歌词文件路径（可能为 None）和可选的媒体根目录，寻找可能的媒体文件。
    1) 若有 source_path：优先同目录同stem，再退而求其次同目录所有媒体
    2) 若提供 media_root：按同stem递归找；若连 stem 都没有，则枚举 media_root 下的媒体文件（谨慎，可能很多）
    """
    candidates: List[Path] = []

    stem: Optional[str] = None
    if source_path is not None:
        stem = source_path.stem
        folder = source_path.parent
        # 同目录同stem
        for ext in MEDIA_EXTS:
            p = folder / f"{stem}{ext}"
            if p.exists():
                candidates.append(p)
        # 同目录的其他媒体文件
        if not candidates:
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
                    candidates.append(p)

    # 在 media_root 下查找（优先同stem）
    if media_root:
        if stem:
            for ext in MEDIA_EXTS:
                for p in media_root.rglob(f"{stem}{ext}"):
                    candidates.append(p)
        elif not candidates:
            # 没有 stem 信息时，尽量少扫：只列出顶层媒体文件
            for p in media_root.glob("*"):
                if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
                    candidates.append(p)

    # 去重，保持顺序
    seen = set()
    uniq = []
    for p in candidates:
        if p not in seen:
            uniq.append(p); seen.add(p)
    return uniq

def play_with_ffplay(media: Path, start_ms: int, end_ms: int, audio_only=True, pad_ms=400):
    if shutil.which("ffplay") is None:
        raise RuntimeError("未找到 ffplay，请确保已安装 FFmpeg 并将其 bin 目录添加到 PATH。")

    ss = max(0, start_ms - pad_ms)
    dur = max(1, (end_ms - start_ms) + 2 * pad_ms)

    args = ["ffplay", "-ss", f"{ss/1000:.3f}", "-t", f"{dur/1000:.3f}", "-i", str(media), "-autoexit", "-loglevel", "error"]
    if audio_only:
        args += ["-vn", "-nodisp"]
    print(f"播放: {media} @ {ms_to_timestr(ss)} ~ +{dur/1000:.2f}s")
    subprocess.run(args)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/index.db")
    ap.add_argument("--query", required=True)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--choose", type=int, help="直接选择第N条结果（1-based）")
    ap.add_argument("--media-root", type=str, help="可选：提供一个媒体根目录，找不到同名文件时到这里递归查找")
    ap.add_argument("--show-video", action="store_true", help="显示视频画面（否则仅音频）")
    args = ap.parse_args()

    rows = query_db(Path(args.db), args.query, args.top)
    if not rows:
        print("没有命中结果"); return

    for i, (title, mtype, s, e, text, src) in enumerate(rows, 1):
        print(f"{i:2d}. [{mtype}] {title} {ms_to_timestr(s)}-{ms_to_timestr(e)} | {text}")

    idx = args.choose
    if not idx:
        try:
            idx = int(input("请选择要播放的序号: ").strip())
        except Exception:
            print("未选择，退出。"); return
    if idx < 1 or idx > len(rows):
        print("序号超出范围"); return

    title, mtype, s, e, text, src = rows[idx-1]
    src_path: Optional[Path] = Path(src) if src and str(src).strip() else None
    media_root = Path(args.media_root).resolve() if args.media_root else None

    cands = find_media_candidates(src_path, media_root)
    if not cands:
        print("未找到媒体文件。建议：\n- 确保字幕与视频在同一文件夹，且文件名前缀一致\n- 或使用 --media-root 指定媒体所在根目录"); return

    media = cands[0]
    if len(cands) > 1:
        print("找到多个媒体文件：")
        for i, p in enumerate(cands, 1):
            print(f"{i}. {p}")
        try:
            pick = int(input("请选择媒体文件序号: ").strip())
            if 1 <= pick <= len(cands):
                media = cands[pick-1]
        except Exception:
            pass

    try:
        play_with_ffplay(media, s, e, audio_only=not args.show_video)
    except RuntimeError as ex:
        print(str(ex))

if __name__ == "__main__":
    main()
