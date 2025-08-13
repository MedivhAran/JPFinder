# indexer_gui.py
# 图形化索引构建器：选择字幕/歌词目录 -> 构建 SQLite FTS5 索引到 %LOCALAPPDATA%\JPFinder\data\index.db
import os
import sys
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import List, Dict, Optional

from PySide6 import QtWidgets, QtGui, QtCore
import pysubs2
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

# ------------------ 数据目录（与主程序一致） ------------------
def get_base_dir() -> Path:
    if os.name == "nt":
        root = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        root = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    base = Path(root) / "JPFinder"
    (base / "data").mkdir(parents=True, exist_ok=True)
    return base

BASE = get_base_dir()
DEFAULT_DB = BASE / "data" / "index.db"

# ------------------ 文本与解析工具 ------------------
_sudachi = dictionary.Dictionary().create()
_mode = sudachi_tokenizer.Tokenizer.SplitMode.C

BIDI_CTRL_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")  # 清理不可见控制符

def nfkc(x: str) -> str:
    return unicodedata.normalize("NFKC", x or "").strip()

def clean_controls(x: str) -> str:
    return BIDI_CTRL_RE.sub("", x or "")

def jp_clean(x: str) -> str:
    return clean_controls(nfkc(x))

def tokenize_surface(text: str):
    for m in _sudachi.tokenize(text, _mode):
        s = (m.surface() or "").strip()
        if s:
            yield s

def tokenize_reading_kana(text: str):
    for m in _sudachi.tokenize(text, _mode):
        r = (m.reading_form() or m.surface() or "").strip()
        if r:
            yield r

def read_text_guess(path: Path) -> str:
    for enc in ("utf-8", "cp932", "cp936"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")

def parse_lrc(path: Path) -> List[Dict]:
    txt = read_text_guess(path)
    lines = txt.splitlines()
    entries = []
    time_tag = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
    for line in lines:
        tags = list(time_tag.finditer(line))
        if not tags:
            continue
        text = time_tag.sub("", line).strip()
        text = jp_clean(text)
        if not text:
            continue
        for m in tags:
            mm = int(m.group(1)); ss = int(m.group(2))
            frac = m.group(3)
            if frac is None:
                ms = 0
            else:
                ms = int((frac + "00")[:3])
            start_ms = (mm * 60 + ss) * 1000 + ms
            entries.append(dict(
                id=f"{path}|{start_ms}",
                media_type="song",
                title=path.stem,
                episode_or_track="",
                media_path="",
                text=text,
                start_ms=start_ms,
                end_ms=start_ms + 3000,
                source_path=str(path),
                context_prev="",
                context_next=""
            ))
    entries.sort(key=lambda x: x["start_ms"])
    for i, e in enumerate(entries):
        e["context_prev"] = entries[i-1]["text"] if i > 0 else ""
        e["context_next"] = entries[i+1]["text"] if i < len(entries)-1 else ""
    return entries

def parse_subtitle(path: Path) -> List[Dict]:
    subs = pysubs2.load(str(path))
    entries = []
    for ev in subs.events:
        text = getattr(ev, "plaintext", None)
        if text is None:
            raw = ev.text or ""
            text = re.sub(r"\{[^}]*\}", "", raw)
        text = jp_clean(text)
        if not text:
            continue
        start_ms = int(ev.start); end_ms = int(ev.end)
        entries.append(dict(
            id=f"{path}|{start_ms}",
            media_type="anime",
            title=path.stem,
            episode_or_track="",
            media_path="",
            text=text,
            start_ms=start_ms,
            end_ms=end_ms,
            source_path=str(path),
            context_prev="",
            context_next=""
        ))
    for i, e in enumerate(entries):
        e["context_prev"] = entries[i-1]["text"] if i > 0 else ""
        e["context_next"] = entries[i+1]["text"] if i < len(entries)-1 else ""
    return entries

# ------------------ SQLite 索引 ------------------
def ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
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

    CREATE VIRTUAL TABLE IF NOT EXISTS fts
    USING fts5(
        text_tok,
        reading_tok,
        content='entries',
        content_rowid='rowid'
    );
    """)
    conn.commit()

def insert_batch(conn: sqlite3.Connection, batch: List[Dict]):
    cur = conn.cursor()
    # 插入主表
    cur.executemany("""
        INSERT OR IGNORE INTO entries
        (id, media_type, title, episode_or_track, media_path, source_path, start_ms, end_ms, text, context_prev, context_next)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, [(
        e["id"], e["media_type"], e.get("title",""), e.get("episode_or_track",""),
        e.get("media_path",""), e.get("source_path",""),
        int(e["start_ms"]), int(e["end_ms"]),
        e["text"], e.get("context_prev",""), e.get("context_next","")
    ) for e in batch])
    # 获取 rowid 并写 FTS
    for e in batch:
        rid = cur.execute("SELECT rowid FROM entries WHERE id=?", (e["id"],)).fetchone()
        if not rid:
            continue
        rid = rid[0]
        text = jp_clean(e["text"])
        text_tok = " ".join(tokenize_surface(text))
        reading_tok = " ".join(tokenize_reading_kana(text))
        cur.execute("INSERT INTO fts(rowid, text_tok, reading_tok) VALUES (?,?,?)",
                    (rid, text_tok, reading_tok))

# ------------------ 扁平风样式（简版） ------------------
def apply_flat_style(app: QtWidgets.QApplication, theme: str = "dark"):
    if theme == "dark":
        p = dict(bg="#1f2125", panel="#26292e", card="#2c2f36", text="#EDEDED", sub="#B9C0CC",
                 border="#343841", grid="#3a3f48", accent="#4C8BF5", sel_bg="#3b60a8", sel_fg="#ffffff")
    else:
        p = dict(bg="#f5f6f8", panel="#ffffff", card="#ffffff", text="#1f2328", sub="#606C80",
                 border="#e6e8eb", grid="#e9ebee", accent="#3577F1", sel_bg="#DCE6FF", sel_fg="#0F172A")
    qss = f"""
    QWidget {{ background:{p['bg']}; color:{p['text']}; font:13px "Segoe UI"; }}
    QFrame#Card {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:10px; }}
    QLineEdit, QSpinBox {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:8px; padding:6px 8px; }}
    QPushButton#Primary {{ background:{p['accent']}; color:white; border:none; border-radius:8px; padding:8px 14px; font-weight:600; }}
    QPushButton#Primary:hover {{ background:{p['accent']}; }}
    QPushButton#Ghost {{ background:transparent; border:1px solid {p['grid']}; border-radius:8px; padding:7px 12px; }}
    QTableWidget {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:10px;
                   selection-background-color:{p['sel_bg']}; selection-color:{p['sel_fg']}; }}
    QHeaderView::section {{ background:{p['panel']}; color:{p['sub']}; padding:6px 8px; border:none; border-bottom:1px solid {p['border']}; }}
    QProgressBar {{ border:1px solid {p['border']}; border-radius:6px; text-align:center; }}
    QProgressBar::chunk {{ background-color:{p['accent']}; border-radius:6px; }}
    """
    app.setStyleSheet(qss)

# ------------------ 后台工作线程 ------------------
class IndexerWorker(QtCore.QObject):
    sig_log = QtCore.Signal(str)
    sig_stage = QtCore.Signal(str)                 # e.g. "扫描文件", "解析/索引"
    sig_progress_files = QtCore.Signal(int, int)   # current, total
    sig_progress_entries = QtCore.Signal(int)      # total entries
    sig_done = QtCore.Signal(bool, str)            # ok, message

    def __init__(self, roots: List[Path], exts: List[str], db_path: Path, rebuild: bool):
        super().__init__()
        self.roots = roots
        self.exts = {e.lower() for e in exts}
        self.db_path = db_path
        self.rebuild = rebuild
        self._cancel = False

    @QtCore.Slot()
    def cancel(self):
        self._cancel = True

    @QtCore.Slot()
    def run(self):
        try:
            if self.rebuild and self.db_path.exists():
                try:
                    self.db_path.unlink()
                except Exception:
                    pass
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            ensure_schema(conn)

            # 扫描文件
            self.sig_stage.emit("扫描文件")
            files: List[Path] = []
            for root in self.roots:
                for p in root.rglob("*"):
                    if self._cancel: 
                        conn.close(); self.sig_done.emit(False, "已取消"); return
                    if p.suffix.lower() in self.exts and p.is_file():
                        files.append(p)
            total_files = len(files)
            self.sig_log.emit(f"找到 {total_files} 个文件。")

            # 解析与索引
            self.sig_stage.emit("解析与索引")
            total_entries = 0
            for i, f in enumerate(files, 1):
                if self._cancel:
                    conn.close(); self.sig_done.emit(False, "已取消"); return
                try:
                    if f.suffix.lower() == ".lrc":
                        items = parse_lrc(f)
                    else:
                        items = parse_subtitle(f)
                    if items:
                        # 清理文本并批量入库（每文件一批）
                        for e in items:
                            e["text"] = jp_clean(e["text"])
                            e["context_prev"] = jp_clean(e.get("context_prev",""))
                            e["context_next"] = jp_clean(e.get("context_next",""))
                        insert_batch(conn, items)
                        total_entries += len(items)
                        if total_entries % 1000 == 0:
                            conn.commit()
                    self.sig_log.emit(f"[{i}/{total_files}] {f.name} -> {len(items)} 行")
                except Exception as ex:
                    self.sig_log.emit(f"[WARN] 解析失败: {f} | {ex}")
                self.sig_progress_files.emit(i, total_files)
                self.sig_progress_entries.emit(total_entries)

            conn.commit()
            # 辅助索引
            cur = conn.cursor()
            cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_title ON entries(title)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_entries_media_type ON entries(media_type)")
            conn.commit(); conn.close()
            self.sig_done.emit(True, f"完成！共索引 {total_entries} 行。")
        except Exception as ex:
            self.sig_done.emit(False, f"异常: {ex}")

# ------------------ 主窗口 ------------------
class IndexerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JPFinder 索引构建器")
        self.resize(960, 680)

        self.worker_thread: Optional[QtCore.QThread] = None
        self.worker: Optional[IndexerWorker] = None

        # 顶部卡片
        card_top = QtWidgets.QFrame(); card_top.setObjectName("Card")
        hl = QtWidgets.QHBoxLayout(card_top); hl.setContentsMargins(12,12,12,12); hl.setSpacing(8)

        self.ed_db = QtWidgets.QLineEdit(str(DEFAULT_DB)); self.ed_db.setReadOnly(True)
        btn_db = QtWidgets.QPushButton("更改..."); btn_db.setObjectName("Ghost")
        btn_db.clicked.connect(self.pick_db)

        self.chk_srt = QtWidgets.QCheckBox(".srt"); self.chk_srt.setChecked(True)
        self.chk_ass = QtWidgets.QCheckBox(".ass"); self.chk_ass.setChecked(True)
        self.chk_lrc = QtWidgets.QCheckBox(".lrc"); self.chk_lrc.setChecked(True)

        self.btn_add = QtWidgets.QPushButton("添加目录"); self.btn_add.setObjectName("Ghost")
        self.btn_remove = QtWidgets.QPushButton("移除选中"); self.btn_remove.setObjectName("Ghost")
        self.btn_clear = QtWidgets.QPushButton("清空列表"); self.btn_clear.setObjectName("Ghost")

        self.list_dirs = QtWidgets.QListWidget()
        self.list_dirs.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_dirs.setMinimumHeight(100)

        self.chk_rebuild = QtWidgets.QCheckBox("重建（清空现有索引）")
        self.btn_scan = QtWidgets.QPushButton("扫描预览"); self.btn_scan.setObjectName("Ghost")
        self.btn_start = QtWidgets.QPushButton("开始构建索引"); self.btn_start.setObjectName("Primary")
        self.btn_cancel = QtWidgets.QPushButton("中止"); self.btn_cancel.setObjectName("Ghost"); self.btn_cancel.setEnabled(False)

        hl.addWidget(QtWidgets.QLabel("输出数据库:"))
        hl.addWidget(self.ed_db, 1); hl.addWidget(btn_db)
        hl.addSpacing(12)
        hl.addWidget(QtWidgets.QLabel("扩展名:"))
        hl.addWidget(self.chk_srt); hl.addWidget(self.chk_ass); hl.addWidget(self.chk_lrc)
        hl.addStretch(1)

        # 中部卡片（目录和日志）
        card_mid = QtWidgets.QFrame(); card_mid.setObjectName("Card")
        vl = QtWidgets.QVBoxLayout(card_mid); vl.setContentsMargins(12,12,12,12); vl.setSpacing(8)
        hl2 = QtWidgets.QHBoxLayout()
        hl2.addWidget(self.btn_add); hl2.addWidget(self.btn_remove); hl2.addWidget(self.btn_clear); hl2.addStretch(1)
        vl.addLayout(hl2)
        vl.addWidget(self.list_dirs)

        # 进度与控制
        card_prog = QtWidgets.QFrame(); card_prog.setObjectName("Card")
        pl = QtWidgets.QGridLayout(card_prog); pl.setContentsMargins(12,12,12,12); pl.setSpacing(8)
        self.pb_files = QtWidgets.QProgressBar(); self.pb_files.setFormat("扫描文件: %v / %m")
        self.pb_entries = QtWidgets.QProgressBar(); self.pb_entries.setFormat("索引行数: %v")
        pl.addWidget(self.pb_files, 0, 0, 1, 3)
        pl.addWidget(self.pb_entries, 1, 0, 1, 3)
        pl.addWidget(self.chk_rebuild, 2, 0)
        pl.addWidget(self.btn_scan, 2, 1)
        pl.addWidget(self.btn_start, 2, 2)
        pl.addWidget(self.btn_cancel, 2, 3)

        # 日志卡片
        card_log = QtWidgets.QFrame(); card_log.setObjectName("Card")
        ll = QtWidgets.QVBoxLayout(card_log); ll.setContentsMargins(12,12,12,12); ll.setSpacing(8)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(2000)
        ll.addWidget(QtWidgets.QLabel("日志："))
        ll.addWidget(self.log)

        # 主布局
        central = QtWidgets.QWidget(); root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(16,12,16,12); root.setSpacing(12)
        root.addWidget(card_top)
        root.addWidget(card_mid, 1)
        root.addWidget(card_prog)
        root.addWidget(card_log, 2)
        self.setCentralWidget(central)

        # 事件
        self.btn_add.clicked.connect(self.add_dir)
        self.btn_remove.clicked.connect(self.remove_dir)
        self.btn_clear.clicked.connect(self.list_dirs.clear)
        self.btn_scan.clicked.connect(self.scan_only)
        self.btn_start.clicked.connect(self.start_build)
        self.btn_cancel.clicked.connect(self.cancel_build)

        # 样式
        apply_flat_style(QtWidgets.QApplication.instance(), theme="dark")

    # ---------- UI handlers ----------
    def pick_db(self):
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "选择/新建数据库文件", str(self.ed_db.text()), "SQLite DB (*.db)")
        if p:
            self.ed_db.setText(p)

    def add_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择字幕/歌词根目录")
        if d:
            self.list_dirs.addItem(d)

    def remove_dir(self):
        for it in self.list_dirs.selectedItems():
            self.list_dirs.takeItem(self.list_dirs.row(it))

    def get_exts(self) -> List[str]:
        exts = []
        if self.chk_srt.isChecked(): exts.append(".srt")
        if self.chk_ass.isChecked(): exts.append(".ass")
        if self.chk_lrc.isChecked(): exts.append(".lrc")
        return exts

    def scan_only(self):
        roots = [Path(self.list_dirs.item(i).text()) for i in range(self.list_dirs.count())]
        if not roots:
            QtWidgets.QMessageBox.information(self, "提示", "请先添加至少一个目录"); return
        exts = set(self.get_exts())
        count = 0
        for r in roots:
            for p in r.rglob("*"):
                if p.suffix.lower() in exts and p.is_file():
                    count += 1
        QtWidgets.QMessageBox.information(self, "扫描结果", f"共找到 {count} 个待解析文件。")

    def start_build(self):
        roots = [Path(self.list_dirs.item(i).text()) for i in range(self.list_dirs.count())]
        if not roots:
            QtWidgets.QMessageBox.information(self, "提示", "请先添加至少一个目录"); return
        if not self.get_exts():
            QtWidgets.QMessageBox.information(self, "提示", "请至少勾选一种扩展名"); return

        db_path = Path(self.ed_db.text()).resolve()
        self.log.clear()
        self.pb_files.reset(); self.pb_entries.reset()

        # 线程
        self.worker_thread = QtCore.QThread(self)
        self.worker = IndexerWorker(roots=roots, exts=self.get_exts(), db_path=db_path, rebuild=self.chk_rebuild.isChecked())
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.sig_log.connect(self.append_log)
        self.worker.sig_stage.connect(lambda s: self.statusBar().showMessage(s))
        self.worker.sig_progress_files.connect(self.on_prog_files)
        self.worker.sig_progress_entries.connect(self.on_prog_entries)
        self.worker.sig_done.connect(self.on_done)

        self.btn_start.setEnabled(False); self.btn_cancel.setEnabled(True)
        self.worker_thread.start()

    def cancel_build(self):
        if self.worker:
            self.worker.cancel()
        self.btn_cancel.setEnabled(False)

    # ---------- callbacks ----------
    def append_log(self, text: str):
        self.log.appendPlainText(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def on_prog_files(self, cur: int, total: int):
        self.pb_files.setMaximum(max(1, total))
        self.pb_files.setValue(cur)

    def on_prog_entries(self, total: int):
        self.pb_entries.setMaximum(0)  # 不限定上限，仅显示数值
        self.pb_entries.setValue(total)

    def on_done(self, ok: bool, msg: str):
        self.btn_start.setEnabled(True); self.btn_cancel.setEnabled(False)
        if self.worker_thread:
            self.worker_thread.quit(); self.worker_thread.wait()
        QtWidgets.QMessageBox.information(self, "完成" if ok else "中止/失败", msg)

def main():
    app = QtWidgets.QApplication(sys.argv)
    win = IndexerWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
