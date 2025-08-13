# parse_media.py
import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import List, Dict
import pysubs2
from tqdm import tqdm

def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()

def ms_to_timestr(ms: int) -> str:
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def read_text_guess(path: Path) -> str:
    # 简单编码猜测（LRC 常见）
    for enc in ("utf-8", "cp932", "cp936"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")

def parse_lrc(path: Path) -> List[Dict]:
    txt = read_text_guess(path)
    lines = txt.splitlines()
    entries: List[Dict] = []
    time_tag = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
    for line in lines:
        tags = list(time_tag.finditer(line))
        if not tags:
            continue
        text = time_tag.sub("", line).strip()
        text = nfkc(text)
        if not text:
            continue
        for m in tags:
            mm = int(m.group(1)); ss = int(m.group(2))
            frac = m.group(3)
            if frac is None:
                ms = 0
            else:
                frac = (frac + "00")[:3]  # 归一为毫秒
                ms = int(frac)
            start_ms = (mm * 60 + ss) * 1000 + ms
            entries.append({
                "id": f"{path}|{start_ms}",
                "media_type": "song",
                "title": path.stem,
                "episode_or_track": "",
                "media_path": "",
                "text": text,
                "start_ms": start_ms,
                "end_ms": start_ms + 3000,  # 没有结束时间就默认 3 秒
                "source_path": str(path),
                "context_prev": "",
                "context_next": "",
            })
    entries.sort(key=lambda x: x["start_ms"])
    for i, e in enumerate(entries):
        e["context_prev"] = entries[i-1]["text"] if i > 0 else ""
        e["context_next"] = entries[i+1]["text"] if i < len(entries)-1 else ""
    return entries

def parse_subtitle(path: Path) -> List[Dict]:
    subs = pysubs2.load(str(path))  # 自动处理编码/格式（SRT/ASS）
    entries: List[Dict] = []
    for ev in subs.events:
        text = getattr(ev, "plaintext", None)
        if text is None:
            raw = ev.text or ""
            text = re.sub(r"\{[^}]*\}", "", raw)
        text = nfkc(text)
        if not text:
            continue
        start_ms = int(ev.start)
        end_ms = int(ev.end)
        entries.append({
            "id": f"{path}|{start_ms}",
            "media_type": "anime",
            "title": path.stem,
            "episode_or_track": "",
            "media_path": "",
            "text": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "source_path": str(path),
            "context_prev": "",
            "context_next": "",
        })
    for i, e in enumerate(entries):
        e["context_prev"] = entries[i-1]["text"] if i > 0 else ""
        e["context_next"] = entries[i+1]["text"] if i < len(entries)-1 else ""
    return entries

def scan_folder(root: Path) -> List[Dict]:
    exts = {".srt", ".ass", ".lrc"}
    all_entries: List[Dict] = []
    files = [p for p in root.rglob("*") if p.suffix.lower() in exts]
    print(f"Found {len(files)} subtitle/lyric files.")
    for f in tqdm(files, desc="Parsing"):
        try:
            if f.suffix.lower() == ".lrc":
                all_entries.extend(parse_lrc(f))
            else:
                all_entries.extend(parse_subtitle(f))
        except Exception as e:
            print(f"[WARN] Failed to parse {f}: {e}")
    return all_entries

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=str, help="字幕/歌词根目录")
    ap.add_argument("--out", type=str, help="导出 JSONL 文件路径（可选）")
    ap.add_argument("--preview", type=int, default=12, help="预览显示前N条")
    args = ap.parse_args()

    root = Path(args.folder).resolve()
    if not root.exists():
        print(f"Path not found: {root}")
        return

    entries = scan_folder(root)
    print(f"Parsed entries: {len(entries)}")

    # 预览前N条
    for e in entries[:args.preview]:
        print(f"[{e['media_type']}] {e['title']} {ms_to_timestr(e['start_ms'])}-{ms_to_timestr(e['end_ms'])} | {e['text']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as w:
            for e in entries:
                w.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"JSONL written: {out_path}")

if __name__ == "__main__":
    main()
