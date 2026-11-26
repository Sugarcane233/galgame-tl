"""游戏库页面 —— 把汉化过的游戏集中起来，方便反复回来操作。

设计取舍
--------
* **状态不缓存**。每次刷新都现场扫磁盘（引擎、缓存条数、快捷方式在不在），
  因为用户很可能在文件管理器里删过东西；显示过期的「已汉化」比不显示更糟。
  扫描很便宜：识别引擎只看几个标志文件，缓存只读一个 JSON 的 len()。
* **删除必须过确认框，且确认框里列出真实路径**。用户点「删除」时看到的
  那几条路径就是最终会被 `rmtree` 的路径 —— 不写「删除游戏数据」这种
  含糊说法。删除计划由 `library.plan_delete()` 生成，UI 只负责展示与勾选。
* **打开目录用 xdg-open**，不自己实现文件管理器。

这个模块只碰 UI，所有磁盘操作都走 `core.library`。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

import ui_theme
from core import library as lib
from core import shortcut as sc
from ui_widgets import Card, icon


# ---------------------------------------------------------------- 扫描线程

class ScanWorker(QThread):
    """后台扫描常见目录。QThread 里不能碰控件，只发信号。"""

    found = Signal(str, str, str)      # path, engine, variant
    finished_ok = Signal(int, float)   # 数量, 耗时
    failed = Signal(str)

    def __init__(self, depth: int = lib.SCAN_DEPTH, parent=None):
        super().__init__(parent)
        self.depth = depth
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        t0 = time.time()
        try:
            res = lib.scan(depth=self.depth, stop=lambda: self._stop)
            for root, det in res:
                self.found.emit(str(root), det.engine, det.variant)
            self.finished_ok.emit(len(res), time.time() - t0)
        except Exception as e:
            self.failed.emit(str(e))


# ---------------------------------------------------------------- 单个条目

class GameRow(QFrame):
    """游戏库里的一行：图标 + 标题 + 状态 + 一排操作按钮。"""

    def __init__(self, entry: lib.GameEntry, status: lib.GameStatus, page, parent=None):
        super().__init__(parent)
        self.setObjectName("gameRow")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.entry = entry
        self.status = status
        self.page = page

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)

        # 图标（游戏原图 or 字母图）
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(44, 44)
        self.icon_label.setObjectName("gameIcon")
        self._load_icon()
        top.addWidget(self.icon_label, 0, Qt.AlignTop)

        textbox = QVBoxLayout()
        textbox.setSpacing(3)
        title = QLabel(entry.title or entry.root.name)
        title.setObjectName("gameTitle")
        textbox.addWidget(title)

        sub = QLabel(status.summary())
        sub.setObjectName("gameMeta")
        sub.setWordWrap(True)
        textbox.addWidget(sub)

        path_lbl = QLabel(str(entry.root))
        path_lbl.setObjectName("gamePath")
        path_lbl.setWordWrap(True)
        path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        textbox.addWidget(path_lbl)
        top.addLayout(textbox, 1)

        self.size_label = QLabel(
            lib.human_size(status.size_bytes) if status.exists else "—")
        self.size_label.setObjectName("gameSize")
        self.size_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        top.addWidget(self.size_label, 0)
        outer.addLayout(top)

        # 操作条
        bar = QHBoxLayout()
        bar.setSpacing(8)

        b_dir = QPushButton("打开目录")
        b_dir.setObjectName("ghost")
        b_dir.setIcon(icon("folder", ui_theme.TEXT_DIM, 16))
        b_dir.setToolTip("用文件管理器打开游戏所在目录")
        b_dir.clicked.connect(self.on_open_dir)
        bar.addWidget(b_dir)

        b_short = QPushButton("建快捷方式")
        b_short.setObjectName("ghost")
        b_short.setIcon(icon("external", ui_theme.TEXT_DIM, 16))
        b_short.setToolTip("在应用列表里创建启动入口（装了运行时汉化的会走 run_bepinex.sh）")
        b_short.clicked.connect(self.on_make_shortcut)
        if not status.exists:
            b_short.setEnabled(False)
        bar.addWidget(b_short)

        b_tl = QPushButton("去汉化")
        b_tl.setObjectName("ghost")
        b_tl.setIcon(icon("translate", ui_theme.TEXT_DIM, 16))
        b_tl.setToolTip("在项目页打开这个游戏，接着翻 / 补漏")
        b_tl.clicked.connect(self.on_translate)
        bar.addWidget(b_tl)

        if status.cache_dir:
            b_cache = QPushButton("打开缓存")
            b_cache.setObjectName("ghost")
            b_cache.setIcon(icon("disk", ui_theme.TEXT_DIM, 16))
            b_cache.setToolTip(f"缓存与快照：{status.cache_dir}")
            b_cache.clicked.connect(self.on_open_cache)
            bar.addWidget(b_cache)

        if status.runtime_ready:
            b_run = QPushButton("启动")
            b_run.setObjectName("ghost")
            b_run.setIcon(icon("play", ui_theme.TEXT_DIM, 16))
            b_run.setToolTip("启动游戏（装了运行时汉化的会走 run_bepinex.sh，词表才生效）")
            b_run.clicked.connect(self.on_launch)
            bar.addWidget(b_run)

        b_ren = QPushButton("改名")
        b_ren.setObjectName("ghost")
        b_ren.setToolTip("只改游戏库里显示的名字，不动目录")
        b_ren.clicked.connect(self.on_rename)
        bar.addWidget(b_ren)

        bar.addStretch(1)

        b_del = QPushButton("删除")
        b_del.setObjectName("danger")
        b_del.setIcon(icon("trash", "#d14343", 16))
        b_del.setToolTip("删除游戏本体 / 汉化缓存 / 快捷方式（会先列清单让你确认）")
        b_del.clicked.connect(self.on_delete)
        bar.addWidget(b_del)
        outer.addLayout(bar)

    # -------------------------------------------------------- 内部

    def _load_icon(self):
        """优先显示游戏原图标；没有就用首字母块，不联网不下载。"""
        path: Optional[Path] = None
        try:
            if self.status.exists:
                path = sc.find_icon(self.entry.root, self.entry.engine or self.status.engine)
        except Exception:
            path = None
        if path and path.is_file():
            from PySide6.QtGui import QPixmap
            pm = QPixmap(str(path))
            if not pm.isNull():
                self.icon_label.setPixmap(
                    pm.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        # 退回首字母块
        from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
        pm = QPixmap(44, 44)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        hue = abs(hash(self.entry.title or self.entry.root.name)) % 360
        p.setBrush(QColor.fromHsv(hue, 90, 210))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, 44, 44, 10, 10)
        f = QFont()
        f.setPixelSize(20)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        ch = (self.entry.title or self.entry.root.name or "?")[:1].upper()
        p.drawText(pm.rect(), Qt.AlignCenter, ch)
        p.end()
        self.icon_label.setPixmap(pm)

    # -------------------------------------------------------- 事件

    def on_open_dir(self):
        if not lib.open_in_file_manager(self.entry.root, self.page.log):
            self.page.toast("打不开目录（xdg-open 不可用）")

    def on_open_cache(self):
        p = Path(self.status.cache_dir)
        if p.is_dir():
            lib.open_in_file_manager(p, self.page.log)
        else:
            self.page.toast("缓存目录已经不在了")

    def on_translate(self):
        self.page.goto_project(self.entry.root)

    def on_launch(self):
        """启动游戏。

        装了 BepInEx 的必须走 `run_bepinex.sh` —— 直接开游戏本体的话
        doorstop 不加载，XUnity 不注入，词表等于没装（用户会看到纯英文，
        还以为汉化失败）。这个判断在 `shortcut._pick_launch()` 里已经做过
        一次，这里复用同一套逻辑，避免两处口径不一致。
        """
        import subprocess
        try:
            launch = sc._pick_launch(self.entry.root, None)
        except Exception as e:
            self.page.toast(f"找不到启动器: {e}")
            return
        if launch is None:
            self.page.toast("没找到可执行的启动器")
            return
        args = ["bash", str(launch)] if launch.suffix.lower() == ".sh" else [str(launch)]
        try:
            subprocess.Popen(args, cwd=str(launch.parent),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.page.log(f"[启动] {launch.name}")
            self.page.toast("已启动")
        except Exception as e:
            self.page.toast(f"启动失败: {e}")

    def on_rename(self):
        from PySide6.QtWidgets import QInputDialog
        cur = self.entry.title or self.entry.root.name
        name, ok = QInputDialog.getText(self.page.win, "改个名字",
                                        "游戏库里显示的名字（不动目录）:", text=cur)
        if not ok or not name.strip():
            return
        self.entry.title = name.strip()
        lib.save(self.page.entries)
        self.page.log(f"[库] 改名 → {self.entry.title}")
        self.page.refresh()

    def on_make_shortcut(self):
        if not self.status.exists:
            self.page.toast("目录不存在")
            return
        try:
            res = sc.create(self.entry.root, title=self.entry.title,
                            log=self.page.log)
        except Exception as e:
            self.page.toast(f"建快捷方式失败: {e}")
            return
        if res.ok:
            self.page.log(f"[快捷方式] {res.summary()} → {res.desktop_path}")
            self.page.toast("快捷方式已创建")
        else:
            self.page.toast(res.error or "建快捷方式失败")
        self.page.refresh()

    def on_delete(self):
        dlg = DeleteDialog(self.entry, self.page)
        if dlg.exec() != QDialog.Accepted:
            return
        opts = dlg.selection()
        if not any(opts.values()):
            self.page.toast("什么都没选")
            return
        done, errs = lib.apply_delete(dlg.plan, game=opts["game"],
                                      cache=opts["cache"],
                                      shortcut=opts["shortcut"],
                                      log=self.page.log)
        if errs:
            QMessageBox.warning(self.page.win, "删除未完全成功",
                                "以下项目没删掉：\n\n" + "\n".join(errs[:8]))
        if opts["game"] or not Path(self.entry.path).is_dir():
            # 游戏本体没了，记录也没意义了
            self.page.forget(self.entry.path)
        self.page.log(f"[删除] 共处理 {done} 项")
        self.page.toast(f"已删除 {done} 项")
        self.page.refresh()


# ---------------------------------------------------------------- 删除确认

class DeleteDialog(QDialog):
    """删除确认。列出的就是会被删的真实路径，没有含糊措辞。"""

    def __init__(self, entry: lib.GameEntry, page, parent=None):
        super().__init__(parent or page.win)
        self.setWindowTitle("删除确认")
        self.setMinimumWidth(620)
        self.entry = entry
        self.plan = lib.plan_delete(entry, log=page.log)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)

        head = QLabel(f"即将删除「{entry.title or entry.root.name}」")
        head.setObjectName("dlgTitle")
        lay.addWidget(head)

        if not self.plan.ok:
            warn = QLabel(f"⚠️ {self.plan.blocked}\n\n这条记录不会被动，请检查路径。")
            warn.setWordWrap(True)
            warn.setObjectName("warnbox")
            lay.addWidget(warn)
            bb = QDialogButtonBox(QDialogButtonBox.Close)
            bb.rejected.connect(self.reject)
            bb.clicked.connect(lambda *_: self.reject())
            lay.addWidget(bb)
            self.chk_game = self.chk_cache = self.chk_short = None
            return

        tip = QLabel("永久删除，不进回收站。勾选的项目会立即执行。")
        tip.setObjectName("dlgTip")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.chk_game = self._check(
            lay, f"游戏本体（{lib.human_size(self.plan.game_bytes)}）",
            self.plan.game_paths, checked=False)
        self.chk_cache = self._check(
            lay, f"汉化缓存与快照（{lib.human_size(self.plan.cache_bytes)}）",
            self.plan.cache_paths, checked=True)
        self.chk_short = self._check(
            lay, "启动快捷方式与图标", self.plan.shortcut_paths, checked=True)

        for chk, paths in ((self.chk_game, self.plan.game_paths),
                           (self.chk_cache, self.plan.cache_paths),
                           (self.chk_short, self.plan.shortcut_paths)):
            if not paths:
                chk.setEnabled(False)
                chk.setText(chk.text() + "（没有）")

        lay.addStretch(1)
        bb = QDialogButtonBox()
        self.btn_ok = bb.addButton("删除", QDialogButtonBox.DestructiveRole)
        self.btn_ok.setObjectName("danger")
        bb.addButton("取消", QDialogButtonBox.RejectRole)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        self.btn_ok.clicked.connect(self.accept)
        lay.addWidget(bb)

    def _check(self, lay: QVBoxLayout, label: str, paths: list[str],
               checked: bool) -> QCheckBox:
        box = QFrame()
        box.setObjectName("delItem")
        v = QVBoxLayout(box)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(4)
        chk = QCheckBox(label)
        chk.setChecked(checked and bool(paths))
        v.addWidget(chk)
        for p in paths:
            lbl = QLabel(p)
            lbl.setObjectName("delPath")
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.addWidget(lbl)
        lay.addWidget(box)
        return chk

    def selection(self) -> dict:
        if not self.plan.ok:
            return {"game": False, "cache": False, "shortcut": False}
        return {
            "game": bool(self.chk_game and self.chk_game.isChecked()),
            "cache": bool(self.chk_cache and self.chk_cache.isChecked()),
            "shortcut": bool(self.chk_short and self.chk_short.isChecked()),
        }


# ---------------------------------------------------------------- 页面

class LibraryPage(QWidget):
    """游戏库整页。`refresh()` 会重建全部行（条目数不多，重建最简单）。"""

    def __init__(self, win, log, goto_project, toast):
        super().__init__()
        self.win = win
        self.log = log
        self.goto_project = goto_project      # 回调：把某个游戏带到项目页
        self.toast = toast                    # 回调：右下角轻提示
        self.entries: list[lib.GameEntry] = lib.load()
        self.scanner: Optional[ScanWorker] = None
        self._build()
        self.refresh()

    # -------------------------------------------------------- 构建

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setObjectName("pageRoot")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(26, 22, 34, 28)
        lay.setSpacing(14)

        t = QLabel("游戏库")
        t.setObjectName("pageTitle")
        lay.addWidget(t)
        d = QLabel("汉化过的游戏都收在这里。打开目录、建快捷方式、清缓存、"
                   "整包删掉 —— 不用再翻文件管理器。")
        d.setObjectName("pageDesc")
        d.setWordWrap(True)
        lay.addWidget(d)

        # 工具栏
        bar = Card("", "")
        row = QHBoxLayout()
        row.setSpacing(8)
        b_add = QPushButton("添加游戏目录")
        b_add.setObjectName("primary")
        b_add.setIcon(icon("plus", "#ffffff", 16))
        b_add.clicked.connect(self.on_add)
        row.addWidget(b_add)

        b_scan = QPushButton("扫描本机游戏")
        b_scan.setObjectName("secondary")
        b_scan.setIcon(icon("refresh", ui_theme.TEXT_DIM, 16))
        b_scan.setToolTip("在 下载 / 桌面 / Steam 库 等常见位置找可识别的游戏")
        b_scan.clicked.connect(self.on_scan)
        row.addWidget(b_scan)

        b_ref = QPushButton("刷新状态")
        b_ref.setObjectName("ghost")
        b_ref.setIcon(icon("refresh", ui_theme.TEXT_DIM, 16))
        b_ref.clicked.connect(self.refresh)
        row.addWidget(b_ref)

        row.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("gameMeta")
        row.addWidget(self.count_label)
        bar.body.addLayout(row)
        lay.addWidget(bar)

        # 列表容器
        self.list_host = QWidget()
        self.list_host.setObjectName("pageRoot")
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(10)
        lay.addWidget(self.list_host)

        self.empty = QLabel("库里还没有游戏。点「添加游戏目录」，或让上面那个"
                            "扫描按钮去常见位置翻一遍。")
        self.empty.setObjectName("pageDesc")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(120)
        lay.addWidget(self.empty)

        lay.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

    # -------------------------------------------------------- 数据

    def refresh(self):
        """重建所有行。条目数量级是几十，重建比做增量 diff 简单可靠。"""
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        n = 0
        for e in list(self.entries):
            try:
                st = lib.probe(e)
            except Exception as ex:
                self.log(f"[库] 探查 {e.path} 失败: {ex}")
                continue
            self.list_lay.addWidget(GameRow(e, st, self))
            n += 1
        self.count_label.setText(f"共 {n} 个游戏" if n else "")
        self.empty.setVisible(n == 0)

    def on_add(self):
        start = getattr(self.win, "last_dir", str(Path.home()))
        d = QFileDialog.getExistingDirectory(self.win, "选择游戏目录（或其外层包目录）", start)
        if not d:
            return
        self.win.last_dir = d
        self.add_path(d)

    def add_path(self, path: str):
        self.entries, e = lib.add(self.entries, path)
        self.entries.sort(key=lambda x: (x.title or "").lower())
        lib.save(self.entries)
        self.log(f"[库] 已加入 {e.title} ({e.path})")
        self.refresh()

    def forget(self, path: str):
        self.entries = lib.remove(self.entries, path)
        lib.save(self.entries)

    # -------------------------------------------------------- 扫描

    def on_scan(self):
        if self.scanner and self.scanner.isRunning():
            self.scanner.stop()
            return
        self.log("[扫描] 在常见目录里找游戏…")
        self.scanner = ScanWorker()
        self.scanner.found.connect(self._on_scan_hit)
        self.scanner.finished_ok.connect(self._on_scan_done)
        self.scanner.failed.connect(lambda m: self.log(f"[扫描] 失败: {m}"))
        self.scanner.start()

    def _on_scan_hit(self, path: str, engine: str, variant: str):
        self.log(f"[扫描] + {Path(path).name}  ({engine}{' '+variant if variant else ''})")
        self.entries, _ = lib.add(self.entries, path)
        lib.save(self.entries)

    def _on_scan_done(self, n: int, secs: float):
        self.log(f"[扫描] 完成，{n} 个可识别的游戏，耗时 {secs:.1f}s")
        self.entries.sort(key=lambda x: (x.title or "").lower())
        lib.save(self.entries)
        self.refresh()
        self.toast(f"扫描到 {n} 个游戏")
