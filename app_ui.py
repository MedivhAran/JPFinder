# app_gui.py
import sys
import os
import json
import sqlite3
import shutil
import subprocess
import hashlib
import html
import re
from pathlib import Path
from typing import Optional, List, Tuple

from PySide6 import QtWidgets, QtGui, QtCore
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

# ----------------- 将数据目录放到本地用户目录 -----------------
def get_base_dir() -> Path:
    # Windows: %LOCALAPPDATA%\JPFinder；其他平台退回到用户目录
    if os.name == "nt":
        root = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        root = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    base = Path(root) / "JPFinder"
    (base / "data").mkdir(parents=True, exist_ok=True)
    return base

BASE = get_base_dir()
DB_PATH = BASE / "data" / "index.db"
CONFIG_PATH = BASE / "data" / "config.json"
CACHE_DIR = BASE / "data" / "cache"

def migrate_legacy_paths():
    """从旧目录（exe 同级或当前工作目录的 data）迁移 index.db / config.json"""
    candidates = []
    try:
        exe_dir = Path(sys.argv[0]).resolve().parent
        candidates.append(exe_dir / "data")
    except Exception:
        pass
    candidates.append(Path.cwd() / "data")

    dst_data = DB_PATH.parent
    dst_data.mkdir(parents=True, exist_ok=True)

    for cand in candidates:
        try:
            if not cand.exists():
                continue
            src_db = cand / "index.db"
            src_cfg = cand / "config.json"
            if src_db.exists() and not DB_PATH.exists():
                shutil.copy2(src_db, DB_PATH)
            if src_cfg.exists() and not CONFIG_PATH.exists():
                shutil.copy2(src_cfg, CONFIG_PATH)
        except Exception:
            pass

migrate_legacy_paths()

# ----------------- 基础工具 -----------------
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

QUOTE_RE = re.compile(r'"([^"]+)"|“([^”]+)”|『([^』]+)』|「([^」]+)」')

def parse_query(query: str) -> Tuple[str, List[str], set, set]:
    phrases: List[str] = []
    for m in QUOTE_RE.finditer(query):
        for g in m.groups():
            if g:
                phrases.append(g.strip())
                break
    surf_set, read_set = set(), set()
    for m in _sudachi.tokenize(query, _mode):
        s = (m.surface() or "").strip()
        r = (m.reading_form() or m.surface() or "").strip()
        if s:
            surf_set.add(s)
        if r:
            read_set.add(r)
    return query, phrases, surf_set, read_set

def ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS media_links(
        stem TEXT PRIMARY KEY,
        media_path TEXT
    );
    """)
    conn.commit()

def get_db_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    ensure_schema(conn)
    return conn

def query_hits(conn: sqlite3.Connection, query: str, phrases: List[str], topn: int = 50):
    match_expr = build_match_query(query)
    if not match_expr:
        return []
    cur = conn.cursor()
    sql = """
    SELECT e.title, e.media_type, e.start_ms, e.end_ms, e.text, e.source_path
    FROM fts
    JOIN entries e ON e.rowid = fts.rowid
    WHERE fts MATCH ?
    ORDER BY bm25(fts)
    LIMIT ?
    """
    params = [match_expr, topn * 5]
    try:
        rows = cur.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        # 若用户尚未建立索引（没有 fts 表），返回空并在状态栏提示
        if "no such table: fts" in msg:
            return []
        if "no such function: bm25" in msg:
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

    if phrases:
        def has_all(txt: str) -> bool:
            t = txt or ""
            return all(p in t for p in phrases)
        rows = [r for r in rows if has_all(r[4])]
        rows = rows[:topn]
    else:
        rows = rows[:topn]
    return rows

def find_media_candidates(source_path: Optional[Path], media_root: Optional[Path]) -> List[Path]:
    candidates: List[Path] = []
    stem: Optional[str] = None
    if source_path:
        stem = source_path.stem
        folder = source_path.parent
        for ext in [".mkv", ".mp4", ".ts", ".m4v", ".avi", ".mov", ".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg"]:
            p = folder / f"{stem}{ext}"
            if p.exists():
                candidates.append(p)
        if not candidates:
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in {".mkv",".mp4",".ts",".m4v",".avi",".mov",".mp3",".flac",".m4a",".aac",".wav",".ogg"}:
                    candidates.append(p)
    if media_root:
        if stem:
            for ext in [".mkv", ".mp4", ".ts", ".m4v", ".avi", ".mov", ".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg"]:
                for p in media_root.rglob(f"{stem}{ext}"):
                    candidates.append(p)
        elif not candidates:
            for p in media_root.glob("*"):
                if p.is_file() and p.suffix.lower() in {".mkv",".mp4",".ts",".m4v",".avi",".mov",".mp3",".flac",".m4a",".aac",".wav",".ogg"}:
                    candidates.append(p)
    uniq, seen = [], set()
    for p in candidates:
        if p not in seen:
            uniq.append(p); seen.add(p)
    return uniq

def load_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"media_root": "", "ffplay_path": "", "ffmpeg_path": ""}

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def get_bound_media(conn: sqlite3.Connection, source_path: Optional[Path]) -> Optional[Path]:
    if not source_path:
        return None
    stem = source_path.stem
    row = conn.execute("SELECT media_path FROM media_links WHERE stem = ?", (stem,)).fetchone()
    if row:
        p = Path(row[0])
        if p.exists():
            return p
    return None

def bind_media(conn: sqlite3.Connection, source_path: Path, media_path: Path):
    stem = source_path.stem
    conn.execute("INSERT OR REPLACE INTO media_links(stem, media_path) VALUES (?,?)", (stem, str(media_path)))
    conn.commit()

def resolve_ffplay(cfg: dict, parent: QtWidgets.QWidget) -> Optional[str]:
    p = cfg.get("ffplay_path")
    if p and Path(p).exists():
        return p
    from shutil import which
    auto = which("ffplay")
    if auto and Path(auto).exists():
        cfg["ffplay_path"] = auto
        save_config(cfg)
        return auto
    path, _ = QtWidgets.QFileDialog.getOpenFileName(parent, "请选择 ffplay.exe", "", "ffplay.exe (ffplay.exe);;所有文件 (*.*)")
    if path:
        cfg["ffplay_path"] = path
        save_config(cfg)
        return path
    return None

def resolve_ffmpeg(cfg: dict, parent: QtWidgets.QWidget) -> Optional[str]:
    p = cfg.get("ffmpeg_path")
    if p and Path(p).exists():
        return p
    from shutil import which
    auto = which("ffmpeg")
    if auto and Path(auto).exists():
        cfg["ffmpeg_path"] = auto
        save_config(cfg)
        return auto
    ffplay = cfg.get("ffplay_path")
    if ffplay:
        cand = Path(ffplay).parent / "ffmpeg.exe"
        if cand.exists():
            cfg["ffmpeg_path"] = str(cand)
            save_config(cfg)
            return str(cand)
    path, _ = QtWidgets.QFileDialog.getOpenFileName(parent, "请选择 ffmpeg.exe", "", "ffmpeg.exe (ffmpeg.exe);;所有文件 (*.*)")
    if path:
        cfg["ffmpeg_path"] = path
        save_config(cfg)
        return path
    return None

def make_snippet(ffmpeg_exe: str, media: Path, start_ms: int, end_ms: int, pad_ms=400) -> Optional[Path]:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        key = f"{media}|{start_ms}|{end_ms}|{pad_ms}"
        h = hashlib.md5(key.encode("utf-8")).hexdigest()  # nosec
        out = CACHE_DIR / f"{h}.mp3"
        if out.exists() and out.stat().st_size > 1024:
            return out
        ss = max(0, start_ms - pad_ms)
        dur = max(1, (end_ms - start_ms) + 2 * pad_ms)
        args = [
            ffmpeg_exe, "-ss", f"{ss/1000:.3f}", "-i", str(media),
            "-t", f"{dur/1000:.3f}", "-vn", "-ac", "2", "-ar", "48000",
            "-b:a", "160k", "-y", str(out),
        ]
        subprocess.run(args, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return out if out.exists() else None
    except Exception:
        return None

# ----------------- 高亮构建（可调样式） -----------------
HIGHLIGHT_TOKEN_STYLE  = "background-color:#FFE69A; color:#202020; border-radius:4px; padding:0 3px;"
HIGHLIGHT_PHRASE_STYLE = "background-color:#BAF7C7; color:#103E1E; border-radius:4px; padding:0 3px;"

def subtract_intervals(a: Tuple[int, int], bs: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    s, e = a
    segments = [(s, e)]
    for bs_s, bs_e in bs:
        new_segments = []
        for x, y in segments:
            if bs_e <= x or bs_s >= y:
                new_segments.append((x, y))
            else:
                if x < bs_s:
                    new_segments.append((x, bs_s))
                if bs_e < y:
                    new_segments.append((bs_e, y))
        segments = new_segments
        if not segments:
            break
    return [(x, y) for x, y in segments if y > x]

def find_phrase_ranges(text: str, phrases: List[str]) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    for p in phrases:
        if not p:
            continue
        start = 0
        while True:
            i = text.find(p, start)
            if i == -1:
                break
            ranges.append((i, i + len(p)))
            start = i + len(p)
    ranges.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in ranges:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    return merged

def build_highlight_html(text: str, phrases: List[str], surf_qset: set, read_qset: set) -> str:
    if not text:
        return ""
    phrase_ranges = find_phrase_ranges(text, phrases)

    token_ranges: List[Tuple[int, int]] = []
    for m in _sudachi.tokenize(text, _mode):
        surf = m.surface() or ""
        read = m.reading_form() or surf
        if (surf in surf_qset) or (read in read_qset):
            pieces = subtract_intervals((m.begin(), m.end()), phrase_ranges)
            token_ranges.extend(pieces)

    labeled = [(s, e, "phrase") for s, e in phrase_ranges] + [(s, e, "token") for s, e in token_ranges]
    labeled.sort(key=lambda x: (x[0], x[2] != "phrase"))

    out: List[Tuple[int, int, str]] = []
    for s, e, t in labeled:
        if t == "token" and any(not (e <= ps or s >= pe) for ps, pe, pt in out if pt == "phrase"):
            continue
        if out and out[-1][2] == t and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e), t)
        else:
            out.append((s, e, t))

    cur = 0
    parts: List[str] = []
    for s, e, t in out:
        if cur < s:
            parts.append(html.escape(text[cur:s]))
        seg = html.escape(text[s:e])
        style = HIGHLIGHT_PHRASE_STYLE if t == "phrase" else HIGHLIGHT_TOKEN_STYLE
        parts.append(f'<span style="{style}">{seg}</span>')
        cur = e
    if cur < len(text):
        parts.append(html.escape(text[cur:]))
    return "".join(parts)

# ----------------- 扁平风样式（收敛 hover 误触） -----------------
def apply_flat_style(app: QtWidgets.QApplication, window: QtWidgets.QMainWindow | None = None, theme: str = "dark"):
    if theme == "dark":
        palette = dict(
            bg="#1f2125", panel="#26292e", card="#2c2f36",
            text="#EDEDED", sub="#B9C0CC",
            border="#343841", grid="#3a3f48",
            accent="#4C8BF5", accent_hover="#4C8BF5",  # hover 同色
            accent_active="#3773E6",
            placeholder="#9aa3af",
            sel_bg="#3b60a8", sel_fg="#ffffff",
            scroll_track="#2b2e34", scroll_handle="#434955"
        )
    else:
        palette = dict(
            bg="#f5f6f8", panel="#ffffff", card="#ffffff",
            text="#1f2328", sub="#606C80",
            border="#e6e8eb", grid="#e9ebee",
            accent="#3577F1", accent_hover="#3577F1",
            accent_active="#2C66D6",
            placeholder="#9aa3af",
            sel_bg="#DCE6FF", sel_fg="#0F172A",
            scroll_track="#f0f2f5", scroll_handle="#cdd3dd"
        )

    qss = f"""
    QMainWindow, QWidget {{
        background: {palette['bg']};
        color: {palette['text']};
        font: 13px "Segoe UI";
    }}
    QFrame#AppBar {{
        background: {palette['panel']};
        border-bottom: 1px solid {palette['border']};
    }}
    QFrame#Card {{
        background: {palette['card']};
        border: 1px solid {palette['border']};
        border-radius: 10px;
    }}
    QLineEdit#SearchEdit {{
        background: {palette['card']};
        border: 1px solid {palette['border']};
        border-radius: 10px;
        padding: 8px 12px;
        color: {palette['text']};
        selection-background-color: {palette['sel_bg']};
        selection-color: {palette['sel_fg']};
    }}
    QLineEdit#SearchEdit::placeholder {{ color: {palette['placeholder']}; }}
    QLineEdit#SearchEdit:hover {{ border-color: {palette['grid']}; }}
    QLineEdit#SearchEdit:focus {{ border-color: {palette['accent']}; }}

    QPushButton#PrimaryButton {{
        background: {palette['accent']};
        border: none;
        color: white;
        padding: 8px 14px;
        border-radius: 8px;
        font-weight: 600;
    }}
    QPushButton#PrimaryButton:hover  {{ background: {palette['accent_hover']}; }}
    QPushButton#PrimaryButton:pressed{{ background: {palette['accent_active']}; }}
    QPushButton#PrimaryButton:focus   {{ outline: none; }}

    QPushButton#GhostButton {{
        background: transparent;
        color: {palette['text']};
        border: 1px solid {palette['grid']};
        padding: 7px 12px;
        border-radius: 8px;
    }}
    QPushButton#GhostButton:hover {{
        background: {palette['panel']};
        border-color: {palette['grid']};
        color: {palette['text']};
    }}
    QPushButton#GhostButton:pressed {{
        background: {palette['panel']};
        border-color: {palette['grid']};
    }}
    QPushButton#GhostButton:focus {{ outline: none; }}

    QCheckBox#FlatCheck {{ spacing: 8px; color: {palette['text']}; }}
    QCheckBox#FlatCheck::indicator {{
        width: 18px; height: 18px;
        border-radius: 4px;
        border: 1px solid {palette['grid']};
        background: {palette['panel']};
    }}
    QCheckBox#FlatCheck::indicator:hover {{
        border-color: {palette['grid']};
        background: {palette['panel']};
    }}
    QCheckBox#FlatCheck::indicator:checked {{
        background: {palette['accent']};
        border-color: {palette['accent']};
        image: url();
    }}

    QHeaderView::section {{
        background: {palette['panel']};
        color: {palette['sub']};
        padding: 6px 8px;
        border: none;
        border-bottom: 1px solid {palette['border']};
    }}
    QTableWidget {{
        background: {palette['card']};
        border: 1px solid {palette['border']};
        border-radius: 10px;
        gridline-color: {palette['grid']};
        selection-background-color: {palette['sel_bg']};
        selection-color: {palette['sel_fg']};
        outline: none;
    }}
    QTableWidget::item:hover {{
        background: transparent;
        color: {palette['text']};
    }}
    QTableWidget::item:selected {{
        background: {palette['sel_bg']};
        color: {palette['sel_fg']};
    }}
    QTableWidget::item:selected:hover {{
        background: {palette['sel_bg']};
        color: {palette['sel_fg']};
    }}

    QScrollBar:vertical {{
        background: {palette['scroll_track']};
        width: 10px; margin: 2px;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical {{
        background: {palette['scroll_handle']};
        min-height: 30px; border-radius: 5px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {palette['grid']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: transparent; }}

    QScrollBar:horizontal {{
        background: {palette['scroll_track']};
        height: 10px; margin: 2px; border-radius: 5px;
    }}
    QScrollBar::handle:horizontal {{
        background: {palette['scroll_handle']};
        min-width: 30px; border-radius: 5px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {palette['grid']}; }}

    QSlider::groove:horizontal {{ height: 6px; background: {palette['grid']}; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: {palette['accent']}; width: 12px; height:12px;
        margin: -4px 0; border-radius: 6px;
    }}
    QSlider::sub-page:horizontal {{ background: {palette['accent']}; border-radius: 3px; }}

    QStatusBar {{
        background: {palette['panel']};
        border-top: 1px solid {palette['border']};
    }}
    """
    app.setStyleSheet(qss)

# ----------------- 简易音频播放器 -----------------
class AudioPlayer(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.8)

        self.btn_play = QtWidgets.QPushButton("▶")
        self.btn_stop = QtWidgets.QPushButton("■")
        for b in (self.btn_play, self.btn_stop):
            b.setObjectName("GhostButton")

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.lbl_time = QtWidgets.QLabel("00:00.000 / 00:00.000")

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(self.btn_play)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.lbl_time)

        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_stop.clicked.connect(self.stop)
        self.slider.valueChanged.connect(self.on_slider)
        self.slider.setRange(0, 0)

        self.player.positionChanged.connect(self.on_pos)
        self.player.durationChanged.connect(self.on_dur)
        self.player.playbackStateChanged.connect(self.on_state)

    def play_file(self, path: Path):
        url = QtCore.QUrl.fromLocalFile(str(path))
        self.player.setSource(url)
        self.player.play()

    def toggle_play(self):
        st = self.player.playbackState()
        if st == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()

    def on_pos(self, pos: int):
        if self.slider.maximum() > 0:
            self.slider.blockSignals(True)
            self.slider.setValue(pos)
            self.slider.blockSignals(False)
        self._update_time_label()

    def on_dur(self, dur: int):
        self.slider.setRange(0, max(0, dur))
        self._update_time_label()

    def on_slider(self, val: int):
        self.player.setPosition(val)

    def on_state(self, _):
        self.btn_play.setText("⏸" if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _update_time_label(self):
        cur = ms_to_timestr(self.player.position())
        dur = ms_to_timestr(self.player.duration())
        self.lbl_time.setText(f"{cur} / {dur}")

# ----------------- 原文列 HTML 渲染委托 -----------------
class RichTextDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() != 3:
            return super().paint(painter, option, index)
        painter.save()
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        doc = QtGui.QTextDocument()
        doc.setDefaultFont(option.font)
        html_text = index.data(QtCore.Qt.ItemDataRole.UserRole) or index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
        doc.setHtml(html_text)
        doc.setTextWidth(option.rect.width())
        painter.translate(option.rect.topLeft())
        ctx = QtGui.QAbstractTextDocumentLayout.PaintContext()
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def sizeHint(self, option, index):
        if index.column() != 3:
            return super().sizeHint(option, index)
        doc = QtGui.QTextDocument()
        doc.setDefaultFont(option.font)
        html_text = index.data(QtCore.Qt.ItemDataRole.UserRole) or index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
        doc.setHtml(html_text)
        doc.setTextWidth(option.rect.width())
        h = doc.size().height()
        return QtCore.QSize(int(doc.idealWidth()), int(h))

# ----------------- 主窗口 -----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JP Finder - 扁平风")
        self.resize(1180, 760)

        self.cfg = load_config()
        self.conn = get_db_conn()
        self.last_rows: list = []
        self.q_surf_set: set = set()
        self.q_read_set: set = set()
        self.q_phrases: List[str] = []

        # 顶部 AppBar
        self.appbar = QtWidgets.QFrame()
        self.appbar.setObjectName("AppBar")
        bar_layout = QtWidgets.QHBoxLayout(self.appbar)
        bar_layout.setContentsMargins(16, 10, 16, 10)
        bar_layout.setSpacing(10)

        self.ed_query = QtWidgets.QLineEdit()
        self.ed_query.setObjectName("SearchEdit")
        self.ed_query.setPlaceholderText("搜索日语词/短语（短语用引号，例如 \"君の名は\"）")

        self.btn_search = QtWidgets.QPushButton("搜索")
        self.btn_search.setObjectName("PrimaryButton")

        self.btn_media_root = QtWidgets.QPushButton("媒体目录…")
        self.btn_media_root.setObjectName("GhostButton")

        self.chk_internal = QtWidgets.QCheckBox("使用内置播放器")
        self.chk_internal.setObjectName("FlatCheck")
        self.chk_internal.setChecked(True)

        self.chk_show_video = QtWidgets.QCheckBox("显示视频画面（仅外部）")
        self.chk_show_video.setObjectName("FlatCheck")

        bar_layout.addWidget(self.ed_query, 1)
        bar_layout.addWidget(self.btn_search)
        bar_layout.addSpacing(8)
        bar_layout.addWidget(self.btn_media_root)
        bar_layout.addStretch(1)
        bar_layout.addWidget(self.chk_internal)
        bar_layout.addWidget(self.chk_show_video)

        # 结果卡片
        self.result_card = QtWidgets.QFrame()
        self.result_card.setObjectName("Card")
        res_layout = QtWidgets.QVBoxLayout(self.result_card)
        res_layout.setContentsMargins(12, 12, 12, 12)
        res_layout.setSpacing(8)

        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["作品", "类型", "时间", "原文（命中高亮）", "源字幕路径"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setItemDelegateForColumn(3, RichTextDelegate(self.table))

        res_layout.addWidget(self.table)

        # 播放器卡片
        self.audio_card = QtWidgets.QFrame()
        self.audio_card.setObjectName("Card")
        aud_layout = QtWidgets.QVBoxLayout(self.audio_card)
        aud_layout.setContentsMargins(12, 12, 12, 12)
        aud_layout.setSpacing(8)

        self.audio = AudioPlayer(self)
        self.btn_play = QtWidgets.QPushButton("播放选中")
        self.btn_play.setObjectName("PrimaryButton")

        aud_layout.addWidget(self.audio)
        aud_layout.addWidget(self.btn_play, alignment=QtCore.Qt.AlignmentFlag.AlignLeft)

        # 阴影（可注释）
        for card in (self.result_card, self.audio_card):
            effect = QtWidgets.QGraphicsDropShadowEffect(card)
            effect.setBlurRadius(18)
            effect.setOffset(0, 6)
            effect.setColor(QtGui.QColor(0, 0, 0, 90))
            card.setGraphicsEffect(effect)

        # 主布局
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)
        root.addWidget(self.appbar)
        root.addWidget(self.result_card, 1)
        root.addWidget(self.audio_card)
        self.setCentralWidget(central)

        # 事件
        self.btn_search.clicked.connect(self.do_search)
        self.ed_query.returnPressed.connect(self.do_search)
        self.table.doubleClicked.connect(self.play_selected)
        self.btn_play.clicked.connect(self.play_selected)
        self.btn_media_root.clicked.connect(self.pick_media_root)

        # 快捷键
        self.sc_focus = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+L"), self)
        self.sc_focus.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
        self.sc_focus.activated.connect(self.ed_query.setFocus)

        # 状态栏
        self.status = self.statusBar()
        ffplay_show = Path(self.cfg.get("ffplay_path","")).name or "未设置"
        ffmpeg_show = Path(self.cfg.get("ffmpeg_path","")).name or "未设置"
        self.status.showMessage(
            f"数据库: {DB_PATH} | 媒体目录: {self.cfg.get('media_root','')} | ffplay: {ffplay_show} | ffmpeg: {ffmpeg_show}"
        )

        # 若未建立索引，友好提示
        try:
            self.conn.execute("SELECT 1 FROM fts LIMIT 1")
        except sqlite3.OperationalError:
            QtWidgets.QMessageBox.information(
                self, "提示",
                f"未检测到索引数据库的 FTS 表。\n请先运行 build_index.py，将 --db 指向：\n{DB_PATH}"
            )

    def pick_media_root(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择媒体根目录")
        if d:
            self.cfg["media_root"] = d
            save_config(self.cfg)
            self.status.showMessage(f"数据库: {DB_PATH} | 媒体目录: {self.cfg.get('media_root','')}")

    def do_search(self):
        q = self.ed_query.text().strip()
        if not q:
            return
        _, phrases, surf_set, read_set = parse_query(q)
        self.q_phrases = phrases
        self.q_surf_set, self.q_read_set = surf_set, read_set

        rows = query_hits(self.conn, q, phrases, topn=200)
        self.last_rows = rows
        self.fill_table(rows)
        note = f"命中 {len(rows)} 条"
        if phrases:
            note += f"（短语：{'，'.join(phrases)}）"
        self.status.showMessage(note)

    def fill_table(self, rows):
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for i, (title, mtype, s, e, text, src) in enumerate(rows):
            time_str = f"{ms_to_timestr(s)} - {ms_to_timestr(e)}"
            it0 = QtWidgets.QTableWidgetItem(str(title)); it0.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            it1 = QtWidgets.QTableWidgetItem(str(mtype)); it1.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            it2 = QtWidgets.QTableWidgetItem(time_str);  it2.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            it3 = QtWidgets.QTableWidgetItem(str(text))
            html_text = build_highlight_html(str(text), self.q_phrases, self.q_surf_set, self.q_read_set)
            it3.setData(QtCore.Qt.ItemDataRole.UserRole, html_text)

            it4 = QtWidgets.QTableWidgetItem(str(src))

            self.table.setItem(i, 0, it0)
            self.table.setItem(i, 1, it1)
            self.table.setItem(i, 2, it2)
            self.table.setItem(i, 3, it3)
            self.table.setItem(i, 4, it4)

    def _item_text(self, row: int, col: int) -> Optional[str]:
        it = self.table.item(row, col)
        return it.text() if it is not None else None

    def get_selected_row(self):
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return None
        idx = sel[0].row()
        if 0 <= idx < len(self.last_rows):
            return self.last_rows[idx]
        # 兜底
        title = self._item_text(idx, 0)
        text = self._item_text(idx, 3)
        if not title or not text:
            return None
        cur = self.conn.cursor()
        row = cur.execute("""
            SELECT title, media_type, start_ms, end_ms, text, source_path
            FROM entries
            WHERE title=? AND text=?
            ORDER BY start_ms
            LIMIT 1
        """, (title, text)).fetchone()
        return row

    def play_selected(self):
        row = self.get_selected_row()
        if not row:
            return
        title, mtype, start_ms, end_ms, text, src = row
        source_path = Path(src) if src and str(src).strip() else None

        media = get_bound_media(self.conn, source_path)
        if media is None:
            media_root = Path(self.cfg["media_root"]).resolve() if self.cfg.get("media_root") else None
            cands = find_media_candidates(source_path, media_root)
            if not cands:
                QtWidgets.QMessageBox.information(self, "提示", "未找到媒体文件。\n请在右上角设置媒体目录，或把视频与字幕放在同一文件夹。")
                return
            items = [str(p) for p in cands]
            item, ok = QtWidgets.QInputDialog.getItem(self, "选择媒体文件", "候选：", items, 0, False)
            if not ok:
                return
            media = Path(item)
            if source_path:
                bind_media(self.conn, source_path, media)

        if self.chk_internal.isChecked():
            ffmpeg_exe = resolve_ffmpeg(self.cfg, self)
            if not ffmpeg_exe:
                QtWidgets.QMessageBox.information(self, "提示", "未设置 ffmpeg.exe，无法裁切片段。")
                return
            out = make_snippet(ffmpeg_exe, media, start_ms, end_ms, pad_ms=400)
            if not out:
                QtWidgets.QMessageBox.warning(self, "失败", "裁切片段失败，请检查 ffmpeg 是否可用。")
                return
            self.audio.play_file(out)
        else:
            ffplay_exe = resolve_ffplay(self.cfg, self)
            if not ffplay_exe:
                QtWidgets.QMessageBox.information(self, "提示", "未设置 ffplay.exe，无法播放。")
                return
            show_video = self.chk_show_video.isChecked()
            ss = max(0, start_ms - 400)
            dur = max(1, (end_ms - start_ms) + 800)
            args = [ffplay_exe, "-ss", f"{ss/1000:.3f}", "-t", f"{dur/1000:.3f}", "-i", str(media), "-autoexit", "-loglevel", "error"]
            if not show_video:
                args += ["-vn", "-nodisp"]
            subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)

def main():
    app = QtWidgets.QApplication(sys.argv)
    apply_flat_style(app, theme="light")  # "dark" / "light"
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
