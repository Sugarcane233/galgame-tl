"""galgame-tl —— 图形界面（PySide6）

交互设计
--------
1. 拖游戏进去 → 自动识别引擎 → 显示「识别到 Ren'Py，找到 3421 句台词」
2. 选模型（workbuddy 自动探测 / 手动填 OpenAI 兼容端点）
3. 点「开始汉化」→ 后台线程跑流水线，日志实时刷、进度条实时走
4. 跑完显示「写入 3402 句，19 句未成功」，可点「查看未成功」定位

线程模型
--------
流水线在 QThread 里跑，通过信号回主线程更新 UI（Qt 不允许跨线程操作控件）。
取消是协作式的：设 stop 事件，流水线在自己的检查点退出，已翻好的留在缓存里。
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.detect import detect                       # noqa: E402
from core.llm import Backend                          # noqa: E402
from core.pipeline import Pipeline, PipelineOptions   # noqa: E402
import ui_theme                                       # noqa: E402
from ui_widgets import Card, ProgressRing, Sidebar, StatCard   # noqa: E402

APP_NAME = "galgame-tl"
ORG = "galgame-tl"


def _init_palette(app: QApplication):
    """给 QSS 覆盖不到的地方（占位符文字、选中的行等）补上深色调色板。

    QSS 没有 ::placeholder 这类选择器，QLineEdit 的占位符颜色只能通过
    QPalette.PlaceholderText 控制；不打这套调色板，深色底上会残留浅灰占位符。
    """
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    bg = QColor(ui_theme.BG)
    card = QColor(ui_theme.CARD_ALT)
    text = QColor(ui_theme.TEXT)
    dim = QColor(ui_theme.TEXT_MUTE)
    pal.setColor(QPalette.Window, bg)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, card)
    pal.setColor(QPalette.AlternateBase, QColor(ui_theme.CARD_ALT))
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.PlaceholderText, dim)
    pal.setColor(QPalette.Button, card)
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.Highlight, QColor(ui_theme.ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor(ui_theme.CARD))
    pal.setColor(QPalette.ToolTipText, text)
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(ui_theme.TEXT_MUTE))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(ui_theme.TEXT_MUTE))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(ui_theme.TEXT_MUTE))
    app.setPalette(pal)


# --------------------------------------------------------------- 工作线程
class Worker(QObject):
    log = Signal(str)
    stage = Signal(str, float)
    progress = Signal(int, int)
    done = Signal(object)          # PipelineResult
    failed = Signal(str)

    def __init__(self, opts: PipelineOptions):
        super().__init__()
        self.opts = opts
        self._stop = threading.Event()
        self._pipe: Pipeline | None = None

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            self._pipe = Pipeline(
                self.opts,
                emit=self._emit,
                should_stop=lambda: self._stop.is_set(),
            )
            result = self._pipe.run()
            self.done.emit(result)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")

    def _emit(self, kind, payload):
        if kind == "log":
            self.log.emit(str(payload))
        elif kind == "stage":
            self.stage.emit(payload["name"], payload.get("pct") or 0.0)
        elif kind == "progress":
            self.progress.emit(payload["done"], payload["total"])
        elif kind == "deferred":
            pass


# --------------------------------------------------------------- 拖放区域
class SkinComboBox(QComboBox):
    """自带白底弹出层的下拉框。

    为什么需要子类：popup 是独立 top-level 窗口，Qt 用
    QComboBoxPrivateContainer(QFrame) 包住 QListView。QSS 的
    `QComboBox QAbstractItemView` 只选得到里面的 view，外层容器仍然透明，
    透明 top-level 会露出宿主窗口的黑色底 —— 视觉上就是列表上下两条黑边
    （截图确认过）。容器没有稳定的 objectName 可选，只能在 showPopup 后
    直接给窗口设样式。
    """

    def showPopup(self):
        super().showPopup()
        view = self.view()
        popup = view.window() if view else None
        if popup is None or popup is self:
            return
        popup.setAttribute(Qt.WA_StyledBackground, True)
        popup.setStyleSheet(
            "QFrame { background-color: #ffffff; border: 1px solid #d3d7de;"
            " border-radius: 6px; }"
            " QListView { background-color: #ffffff; border: none;"
            " outline: 0; padding: 2px; }"
        )


class DropZone(QFrame):
    dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName("dropzone")
        self.setMinimumHeight(148)
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(8)

        self.icon = QLabel("⤓")
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setStyleSheet(
            f"color:{ui_theme.ACCENT}; font-size:30px; background:transparent;")
        lay.addWidget(self.icon)

        self.title = QLabel("把游戏目录拖到这里")
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet(f"color:{ui_theme.TEXT}; background:transparent;")
        lay.addWidget(self.title)

        self.hint = QLabel("也可以点一下选择目录 · 支持 Ren'Py / RPG Maker MV·MZ·VX Ace / Unity")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(
            f"color:{ui_theme.TEXT_MUTE}; font-size:12px; background:transparent;")
        lay.addWidget(self.hint)

    def mousePressEvent(self, e):
        # 拖放区兼作按钮：不是每个人都会想到往窗口里拖
        self.window().on_pick_dir()
        super().mousePressEvent(e)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_hover(True)

    def dragLeaveEvent(self, e):
        self._set_hover(False)

    def dropEvent(self, e: QDropEvent):
        self._set_hover(False)
        for url in e.mimeData().urls():
            p = str(url.toLocalFile())
            if p:
                self.dropped.emit(p)
                break

    def _set_hover(self, on: bool):
        self.setProperty("hover", on)
        self.style().unpolish(self)
        self.style().polish(self)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("galgame-tl · galgame 汉化工具")
        self.resize(1180, 820)
        self.setMinimumSize(980, 680)
        self.settings = QSettings(ORG, APP_NAME)
        self.worker: Worker | None = None
        self.thread: QThread | None = None
        self.current_target = ""
        self.detection = None
        self.glossary: dict[str, str] = self._load_glossary_file()
        # 下拉里的模型来自哪个来源：workbuddy / custom。
        # 切到自定义端点时靠它判断要不要清空旧列表。
        self._models_owner = "workbuddy"

        # 运行页的实时统计（on_progress 里更新）
        self._t0 = 0.0
        self._stage_name = ""

        self._build_ui()
        self._load_settings()
        QTimer.singleShot(200, self._on_source_changed_idx)

    def _on_source_changed_idx(self):
        """启动时按已保存的来源初始化，而不是一律去探旧网关。"""
        idx = self.source_combo.currentIndex()
        if idx == 0:
            self._load_wb_accounts()
            saved = self.settings.value("model")
            if saved and self.model_combo.findText(saved) >= 0:
                self.model_combo.setCurrentText(saved)
        elif idx == 1:
            self._autodetect_gateway()

    # ------------------------------------------------------------ UI
    def _field_label(self, text: str, width: int = 86) -> QLabel:
        """表单左侧字段名：竖向居中、次要色、固定宽度让冒号对齐。"""
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lbl.setFixedWidth(width)
        lbl.setStyleSheet(
            f"color:{ui_theme.TEXT_DIM}; font-size:12px; background:transparent;")
        return lbl

    def _hline(self) -> QFrame:
        line = QFrame()
        line.setObjectName("hline")
        line.setFrameShape(QFrame.NoFrame)
        return line

    def _new_page(self, title: str, desc: str = "") -> tuple[QWidget, QVBoxLayout]:
        """建一个可滚动的空白页并挂到 stack，返回 (外层页面, 内容布局)。

        内容比窗口高时（小窗口 / 笔记本屏）必须能滚，否则底部按钮会被裁掉。
        """
        outer = QWidget()
        outer.setObjectName("pageRoot")
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setObjectName("pageRoot")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(26, 22, 34, 28)
        lay.setSpacing(14)
        if title:
            t = QLabel(title)
            t.setObjectName("pageTitle")
            lay.addWidget(t)
        if desc:
            d = QLabel(desc)
            d.setObjectName("pageDesc")
            d.setWordWrap(True)
            lay.addWidget(d)

        scroll.setWidget(inner)
        outer_lay.addWidget(scroll, 1)
        self.stack.addWidget(outer)
        self._page_outer = outer_lay
        return outer, lay

    def _goto_page(self, idx: int):
        self.stack.setCurrentIndex(idx)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # ---- 左侧导航
        self.nav = Sidebar()
        self.nav.page_changed.connect(self._goto_page)
        row.addWidget(self.nav)

        self.stack = QStackedWidget()
        row.addWidget(self.stack, 1)

        self._build_page_project()
        self._build_page_run()
        self._build_page_settings()

        self.nav.add_group("任务")
        self.nav.add_page("项目", "folder")
        self.nav.add_page("运行", "play")
        self.nav.add_group("其它")
        self.nav.add_page("设置", "settings")
        self.nav.finish()
        self.nav.select(0)

    # ---------------------------------------------------------- 页 1：项目
    def _build_page_project(self):
        page, root = self._new_page(
            "项目",
            "把游戏目录拖进来，识别引擎 → 提取文本 → 翻译 → 回填，一条龙。")

        # 拖放 + 路径
        c1 = Card("游戏目录", "支持 Ren'Py / RPG Maker MV·MZ·VX Ace / Unity")
        self.drop = DropZone()
        self.drop.dropped.connect(self.on_drop)
        c1.body.addWidget(self.drop)

        pick = QHBoxLayout()
        pick.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("还没有选择目录…")
        self.path_edit.setReadOnly(True)
        btn_pick = QPushButton("选择目录")
        btn_pick.setObjectName("ghost")
        btn_pick.clicked.connect(self.on_pick_dir)
        pick.addWidget(self.path_edit, 1)
        pick.addWidget(btn_pick)
        c1.body.addLayout(pick)

        self.info = QLabel("等待拖入游戏…")
        self.info.setWordWrap(True)
        self.info.setObjectName("infobox")
        c1.body.addWidget(self.info)
        root.addWidget(c1)

        # 模型配置
        c2 = Card("翻译模型", "主模型负责正文；两道兜底只在前一道拒绝时才上场。",
                  "translate")
        ml = QGridLayout()
        ml.setHorizontalSpacing(10)
        ml.setVerticalSpacing(10)
        ml.setColumnStretch(1, 1)
        ml.setColumnStretch(3, 1)

        ml.addWidget(self._field_label("来源"), 0, 0)
        self.source_combo = SkinComboBox()
        self.source_combo.addItems([
            "WorkBuddy（直连，免 token）",
            "WorkBuddy（本机网关，需 token）",
            "OpenAI 兼容（自定义）",
        ])
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        ml.addWidget(self.source_combo, 0, 1, 1, 3)

        ml.addWidget(self._field_label("Base URL"), 1, 0)
        self.base_edit = QLineEdit()
        self.base_edit.setPlaceholderText("http://127.0.0.1:11434/v1")
        ml.addWidget(self.base_edit, 1, 1, 1, 3)

        ml.addWidget(self._field_label("模型"), 2, 0)
        self.model_combo = SkinComboBox()
        self.model_combo.setEditable(True)
        # 上游有 63 个模型，不限制的话弹出列表会拉到屏幕外
        self.model_combo.setMaxVisibleItems(12)
        self.model_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ml.addWidget(self.model_combo, 2, 1)

        ml.addWidget(self._field_label("API Key", 72), 2, 2)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-…（本地端点可留空）")
        ml.addWidget(self.key_edit, 2, 3)

        btn_probe = QPushButton("探测 / 刷新模型")
        btn_probe.setObjectName("ghost")
        btn_probe.clicked.connect(self.on_probe)
        ml.addWidget(btn_probe, 3, 1, 1, 2)

        self.auto_gloss_btn = QPushButton("自动生成术语表")
        self.auto_gloss_btn.setObjectName("ghost")
        self.auto_gloss_btn.setToolTip(
            "先用模型看完台词里的专有名词（人名/地名/招式），生成术语表再翻译。\n"
            "需要先拖入游戏并选好模型——要先提取文本才能分析。")
        self.auto_gloss_btn.clicked.connect(self.on_auto_glossary)
        ml.addWidget(self.auto_gloss_btn, 3, 3)
        c2.body.addLayout(ml)
        root.addWidget(c2)

        # 选项
        c3 = Card("翻译选项", "数值影响速度与配额消耗；开关决定要不要兜底。",
                  "settings")
        ol = QHBoxLayout()
        ol.setSpacing(10)
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 100)
        self.batch_spin.setValue(20)
        self.batch_spin.setFixedWidth(84)
        ol.addWidget(self._field_label("每批句数", 64))
        ol.addWidget(self.batch_spin)

        ol.addSpacing(18)
        self.ab_spin = QSpinBox()
        self.ab_spin.setRange(1, 5)
        self.ab_spin.setValue(3)
        self.ab_spin.setFixedWidth(76)
        ol.addWidget(self._field_label("重试", 34))
        ol.addWidget(self.ab_spin)

        ol.addSpacing(18)
        self.conc_spin = QSpinBox()
        self.conc_spin.setRange(1, 16)
        self.conc_spin.setValue(4)
        self.conc_spin.setFixedWidth(76)
        self.conc_spin.setToolTip("同时发出的请求数。串行翻两万句要十几小时，"
                                  "4 路实测不触发限速；被限速就降到 2。")
        ol.addWidget(self._field_label("并发", 34))
        ol.addWidget(self.conc_spin)
        ol.addStretch(1)
        c3.body.addLayout(ol)

        c3.body.addWidget(self._hline())

        oc = QGridLayout()
        oc.setHorizontalSpacing(24)
        oc.setVerticalSpacing(6)
        oc.setColumnStretch(0, 1)
        oc.setColumnStretch(1, 1)

        self.dry_check = QCheckBox("只翻译不写回（预览）")
        self.dry_check.setToolTip("跑完全程但不修改游戏文件，先看看译文质量。")
        oc.addWidget(self.dry_check, 0, 0)

        self.nv_check = QCheckBox("NVIDIA 兜底")
        self.nv_check.setChecked(True)
        self.nv_check.setToolTip(
            "主模型（DeepSeek）对露骨性描写会直接内容过滤，或者在流中途掐断，\n"
            "实测约 3% 的台词过不去。勾上就把这些句子交给 NVIDIA 的\n"
            "nemotron-3-ultra——它同样带术语表和前后文，译名与语气能接上。\n"
            "key 自动从 DSH 凭据里读，读不到就自动关掉。")
        oc.addWidget(self.nv_check, 0, 1)

        self.gf_check = QCheckBox("谷歌翻译兜底")
        self.gf_check.setChecked(True)
        self.gf_check.setToolTip(
            "第三道防线：前两道（DeepSeek / NVIDIA）都拒的句子交给谷歌翻译。\n"
            "机器翻译不过滤文学性描写，剩下的露骨句它能翻（实测 60/60 全过）；\n"
            "质量比大模型差一档，但比留英文强。人名照样走术语表占位符。\n"
            "需要能连上谷歌（自动探环境变量代理与本机 7890 端口）。")
        oc.addWidget(self.gf_check, 1, 0)

        self.simp_check = QCheckBox("繁转简清洗")
        self.simp_check.setChecked(True)
        self.simp_check.setToolTip(
            "模型偶尔会在中文长句里滑到繁体（实测 TLS 8483 条译文有 1047 条混了繁体字）。\n"
            "勾上就在收尾时统一转成大陆简体，连词汇一起转（软体→软件、网路→网络）。\n"
            "需要 zhconv 库。")
        oc.addWidget(self.simp_check, 1, 1)

        self.port_check = QCheckBox("Windows 版改造成 Linux 原生跑")
        self.port_check.setToolTip(
            "Ren'Py 的 renpy/ 和 game/ 完全跨平台，只有 lib/py3-windows-* 是平台二进制。\n"
            "勾上就从本机其它 Ren'Py 游戏借一份 lib/py3-linux-*，直接原生运行——"
            "不是 Wine 转译，帧率和原生版一样。\n"
            "只在识别到 Windows 版 Ren'Py 时生效。")
        oc.addWidget(self.port_check, 2, 0, 1, 2)

        self.shortcut_check = QCheckBox("完成后创建启动快捷方式")
        self.shortcut_check.setChecked(True)
        self.shortcut_check.setToolTip(
            "汉化完成后在应用列表里加一个游戏启动项，图标用游戏自带的\n"
            "（Ren'Py 的 window_icon.png / MZ 的 icon/icon.png）；\n"
            "找不到原图才退回按名字生成的字母图标——不会换成工具自己的图标。\n"
            "同时会往桌面目录放一份（GNOME 需要 trusted 标记才显示成图标）。")
        oc.addWidget(self.shortcut_check, 3, 0)

        self.shortcut_desktop_check = QCheckBox("桌面上也放一份")
        self.shortcut_desktop_check.setChecked(True)
        self.shortcut_desktop_check.setToolTip(
            "取消勾选就只加到应用列表（开始菜单），不放桌面。")
        oc.addWidget(self.shortcut_desktop_check, 3, 1)
        # 主开关关掉时，「桌面上也放一份」跟着变灰（连接放在控件建好之后，
        # 否则 lambda 里引用的属性还不存在）
        self.shortcut_check.toggled.connect(self.shortcut_desktop_check.setEnabled)

        self.unity_rt_check = QCheckBox("Unity 运行时替换（不动资源文件）")
        self.unity_rt_check.setToolTip(
            "Unity 的 MonoBehaviour 对象长度由 Assembly-CSharp.dll 的类布局锁死。\n"
            "直接改资源文件让译文变长，Unity 会报「different serialization layout」\n"
            "并丢弃该对象 → 场景加载失败 → 闪过 logo 后黑屏（实测必现）。\n\n"
            "勾上就改走运行时替换：装 BepInEx + XUnity.AutoTranslator，游戏启动时\n"
            "用离线词表替换文本。资源文件一个字节都不动，译文不受原长度限制。\n"
            "代价是以后要用 run_bepinex.sh 启动游戏（会顺手做成快捷方式）。\n"
            "只在识别到 Unity 时生效。")
        oc.addWidget(self.unity_rt_check, 4, 0, 1, 2)

        c3.body.addLayout(oc)
        root.addWidget(c3)

        root.addStretch(1)

        # 底部操作条：固定在页面底部（不随内容滚动），随时都能点开始
        footer = QWidget()
        footer.setObjectName("pageFooter")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(26, 14, 26, 16)
        fl.setSpacing(9)

        ops = QHBoxLayout()
        ops.setSpacing(10)
        self.btn_start = QPushButton("开始汉化")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop = QPushButton("取消")
        self.btn_stop.setObjectName("secondary")
        self.btn_stop.setMinimumHeight(44)
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)
        ops.addWidget(self.btn_start, 3)
        ops.addWidget(self.btn_stop, 1)
        fl.addLayout(ops)

        # 进度（留在项目页，不切页也能看到）
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        # 文字不画在条上：QSS 的 chunk 会盖住 text-align:center 的字，
        # 深色块配深色字几乎不可读。改在条下面单独一行。
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        fl.addWidget(self.bar)

        self.bar_text = QLabel("尚未开始")
        self.bar_text.setStyleSheet(
            f"color:{ui_theme.TEXT_MUTE}; font-size:11.5px;")
        fl.addWidget(self.bar_text)

        self._page_outer.addWidget(footer)

    # ---------------------------------------------------------- 页 2：运行
    def _build_page_run(self):
        page, root = self._new_page(
            "运行", "翻译过程中的实时进度。切到别的页不影响任务继续跑。")

        # 顶部：进度环 + 四个统计
        top = QHBoxLayout()
        top.setSpacing(14)

        ring_card = Card("任务进度")
        self.ring = ProgressRing()
        ring_card.body.addWidget(self.ring, 0, Qt.AlignHCenter)
        ring_card.setFixedWidth(220)
        top.addWidget(ring_card)

        stats = QGridLayout()
        stats.setHorizontalSpacing(14)
        stats.setVerticalSpacing(14)
        self.stat_done = StatCard("已完成", "句", "check")
        self.stat_remain = StatCard("剩余", "句", "clock")
        self.stat_speed = StatCard("速度", "句 / 秒", "speed")
        self.stat_stage = StatCard("当前阶段", "", "bolt", big=False)
        stats.addWidget(self.stat_done, 0, 0)
        stats.addWidget(self.stat_remain, 0, 1)
        stats.addWidget(self.stat_speed, 1, 0)
        stats.addWidget(self.stat_stage, 1, 1)
        top.addLayout(stats, 1)
        root.addLayout(top)

        # 日志
        c = Card("运行日志", "翻译失败的句子、被拒的批次、写回结果都在这里。",
                 "log")
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setMinimumHeight(240)
        c.body.addWidget(self.log_view, 1)
        root.addWidget(c, 1)

    # ---------------------------------------------------------- 页 3：设置
    def _build_page_settings(self):
        page, root = self._new_page("设置", "只影响本机，不写进游戏目录。")

        c1 = Card("术语表", "人名、地名、招式名的固定译法。翻译时以占位符形式保护，"
                          "模型改不动。", "book")
        self.glossary_btn = QPushButton(f"编辑术语表 · {len(self.glossary)} 条")
        self.glossary_btn.setToolTip("手动编辑术语表（原文 = 译文，一行一条）")
        self.glossary_btn.clicked.connect(self.on_glossary)
        c1.body.addWidget(self.glossary_btn)
        root.addWidget(c1)

        c2 = Card("关于", "", "doc")
        about = QLabel(
            "galgame-tl · galgame 汉化工具\n\n"
            "支持引擎：Ren'Py · RPG Maker MV/MZ · VX Ace · Unity\n"
            "翻译链路：主模型 → NVIDIA 兜底 → 谷歌翻译兜底\n\n"
            "缓存与快照都写在游戏目录旁的 _galgame_tl/，删掉它等于清空进度。")
        about.setWordWrap(True)
        about.setStyleSheet(f"color:{ui_theme.TEXT_DIM}; font-size:12.5px;")
        c2.body.addWidget(about)
        root.addWidget(c2)

        root.addStretch(1)

    # ---------------------------------------------------------- 术语表
    GLOSSARY_PATH = Path(__file__).resolve().parent / "glossary.json"

    def _load_glossary_file(self) -> dict[str, str]:
        try:
            d = json.loads(self.GLOSSARY_PATH.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in d.items()}
        except Exception:
            return {}

    def _save_glossary_file(self):
        try:
            self.GLOSSARY_PATH.write_text(
                json.dumps(self.glossary, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"[术语] 保存失败: {e}")

    # -------------------------------------------------------- 设置持久化
    def _load_settings(self):
        s = self.settings
        idx = int(s.value("source", 0))
        self.source_combo.setCurrentIndex(idx)
        if idx == 0:
            self.base_edit.setText("workbuddy:")
            self.base_edit.setEnabled(False)
            self.key_edit.setEnabled(False)
            self.key_edit.setPlaceholderText("直连模式不需要（自动读 DSH 登录态）")
        else:
            self.base_edit.setText(s.value("base_url", ""))
            self.key_edit.setText(s.value("api_key", ""))
        if s.value("model"):
            self.model_combo.setCurrentText(s.value("model"))
        self.batch_spin.setValue(int(s.value("batch", 20)))
        self.ab_spin.setValue(int(s.value("retries", 3)))
        self.conc_spin.setValue(int(s.value("concurrency", 4)))
        self.port_check.setChecked(s.value("port_to_linux", "false") == "true")
        self.nv_check.setChecked(s.value("nv_fallback", "true") == "true")
        self.gf_check.setChecked(s.value("gf_fallback", "true") == "true")
        self.simp_check.setChecked(s.value("simplify", "true") == "true")
        self.last_dir = s.value("last_dir", str(Path.home()))

    def _save_settings(self):
        s = self.settings
        s.setValue("source", self.source_combo.currentIndex())
        s.setValue("base_url", self.base_edit.text())
        s.setValue("api_key", self.key_edit.text())
        s.setValue("model", self.model_combo.currentText())
        s.setValue("batch", self.batch_spin.value())
        s.setValue("retries", self.ab_spin.value())
        s.setValue("concurrency", self.conc_spin.value())
        s.setValue("port_to_linux", "true" if self.port_check.isChecked() else "false")
        s.setValue("nv_fallback", "true" if self.nv_check.isChecked() else "false")
        s.setValue("gf_fallback", "true" if self.gf_check.isChecked() else "false")
        s.setValue("simplify", "true" if self.simp_check.isChecked() else "false")
        s.setValue("last_dir", getattr(self, "last_dir", str(Path.home())))

    def closeEvent(self, e):
        self._save_settings()
        if self.worker:
            self.worker.stop()
        super().closeEvent(e)

    # ------------------------------------------------------------ 探测
    def _autodetect_gateway(self):
        try:
            from core.wbgateway import discover
            hits = discover()
        except Exception as e:
            self.log(f"[探测] workbuddy 网关探测失败: {e}")
            hits = []
        if hits:
            self.base_edit.setText(f"http://127.0.0.1:{hits[0]['port']}/v1")
            self.log(f"[探测] 找到 WorkBuddy 网关: 端口 {hits[0]['port']}"
                     + ("（免鉴权）" if hits[0].get("open") else "（需要 Bearer token）"))
            if not hits[0].get("open"):
                self.log("[提示] 这个 token 由插件随机生成且不落盘。两种做法：")
                self.log("       ① 在 DSH 设置 → dsh-connect-workbuddy 里查看本地网关 token 填到上面")
                self.log("       ② 直接改用「OpenAI 兼容」+ DeepSeek/ollama 等端点（更省事）")
            self.on_probe()
        else:
            self.log("[探测] 没找到 WorkBuddy 网关（插件可能没启用），请手动填 Base URL")
        # 网关要 token 才给模型列表，填不上时下拉就空着，看着像坏了。
        # 上游是同一批 WorkBuddy 账号，模型名完全通用，所以直接借直连的目录填上。
        if self.model_combo.count() == 0:
            self._fill_models_from_vault("网关需要 token，模型列表先借用直连账号的目录")

    def _on_source_changed(self, idx: int):
        if idx == 0:
            # 直连模式：不需要填地址和 key，凭据从 DSH 的 vault 读
            self.base_edit.setText("workbuddy:")
            self.base_edit.setEnabled(False)
            self.key_edit.setEnabled(False)
            self.key_edit.clear()
            self.key_edit.setPlaceholderText("直连模式不需要（自动读 DSH 登录态）")
            self._load_wb_accounts()
        elif idx == 1:
            self.base_edit.setEnabled(True)
            self.key_edit.setEnabled(True)
            self.key_edit.setPlaceholderText("粘贴插件网关的 Bearer token")
            self._autodetect_gateway()
        else:
            self.base_edit.setEnabled(True)
            self.key_edit.setEnabled(True)
            self.key_edit.setPlaceholderText("sk-…（本地端点可留空）")
            if not self.base_edit.text() or self.base_edit.text().startswith("workbuddy:"):
                self.base_edit.setText("http://127.0.0.1:11434/v1")
            # 上一批是 WorkBuddy 的模型名，留着会让人以为这个端点也支持，
            # 实际请求会 404。清空后提示重新探测。
            if self._models_owner != "custom":
                self.model_combo.clear()
                self.model_combo.setCurrentText("")
                self._models_owner = "custom"

    def _fill_models_from_vault(self, why: str = ""):
        """用 DSH vault 里的 WorkBuddy 账号拉模型目录填进下拉。

        网关模式拿不到 token 时也走这条路：上游是同一批账号，模型名通用，
        至少让下拉不是空的。失败就静默返回，不打扰用户。
        """
        try:
            from core import wbdirect
            accs = wbdirect.discover_accounts()
            if not accs:
                return False
            models = wbdirect.list_models(accs[0])
        except Exception as e:
            self.log(f"[模型] 借直连目录失败: {e}")
            return False
        if not models:
            return False
        if why:
            self.log(f"[模型] {why}")
        self._apply_models(models)
        return True

    def _apply_models(self, models: list[str], owner: str = "workbuddy"):
        """统一填模型下拉 + 挑默认值。两条路径（直连 / 网关）共用。"""
        self._models_owner = owner
        cur = self.model_combo.currentText().strip()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if cur and cur in models:
            self.model_combo.setCurrentText(cur)
            return
        # deepseek-v4.1-flash 实测 1.6 秒/4 句；hy4free 免费但极慢（4 句 79 秒）
        for pref in ("deepseek-v4.1-flash", "deepseek-v4-flash",
                     "fast-model", "hy4std", "auto"):
            if pref in models:
                self.model_combo.setCurrentText(pref)
                return
        self.model_combo.setCurrentIndex(0)

    def _load_wb_accounts(self):
        """直连模式：把 vault 里的账号和上游模型读出来填进下拉。"""
        try:
            from core import wbdirect
            accs = wbdirect.discover_accounts()
        except Exception as e:
            self.log(f"[WorkBuddy] 读账号失败: {e}")
            return
        if not accs:
            self.model_combo.clear()
            self.log("[WorkBuddy] 没找到登录态。先在 DSH 里把 dsh-connect-workbuddy 登好。")
            return
        self.log(f"[WorkBuddy] 找到 {len(accs)} 个账号: "
                 + "、".join(a.label() for a in accs[:3]))
        acc = accs[0]
        if acc.expired():
            self.log("[WorkBuddy] 主账号 token 已过期，首次请求时自动刷新")
        models = []
        try:
            models = wbdirect.list_models(acc)
        except Exception as e:
            self.log(f"[WorkBuddy] 读模型目录失败: {e}")
        if models:
            self._apply_models(models)
            self.log(f"[WorkBuddy] 上游提供 {len(models)} 个模型")
        else:
            self.model_combo.clear()
            self.log("[WorkBuddy] 没读到模型目录，可以手填 hy4free / hy4std")

    def on_probe(self):
        if self.source_combo.currentIndex() == 0:
            self._load_wb_accounts()
            return
        base = self.base_edit.text().strip()
        if not base:
            self._msg("先填 Base URL")
            return
        be = Backend("probe", base, "", self.key_edit.text().strip(), timeout=15)
        models = be.list_models()
        self.model_combo.clear()
        if models:
            self._models_owner = "custom"
            self.model_combo.addItems(models)
            self.log(f"[探测] {base} 提供 {len(models)} 个模型: "
                     + ", ".join(models[:8]) + ("…" if len(models) > 8 else ""))
        else:
            self.log(f"[探测] {base} 没返回模型列表（端点可能不支持 /models，或需要 token）")
            self.log("       可以手填模型名，例如 deepseek-chat / gpt-4o-mini / qwen2.5:14b")

    # ------------------------------------------------------------ 目标
    def on_pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择游戏目录",
                                             getattr(self, "last_dir", str(Path.home())))
        if d:
            self.last_dir = d
            self.on_drop(d)

    def on_drop(self, path: str):
        self.current_target = path
        self.path_edit.setText(path)
        self.log(f"[识别] {path}")

        # 拖进来的是自解压安装包（InstallShield / Inno / NSIS）：
        # 先拆开再识别，否则只会看到目录里孤零零一个 exe。
        try:
            p = Path(path)
            if p.is_file() and p.suffix.lower() == ".exe":
                from core import vxace
                if vxace.is_self_extracting_exe(p):
                    # 不要解到附件缓存/游戏旁边（可能只读、也可能把原作目录搞乱）
                    base = Path.home() / "下载" / "_galgame_tl"
                    out = base / p.stem
                    self.log(f"[解包] 这是个自解压安装包，正在拆到 {out.name}/ …")
                    ok, msg = vxace.extract_installer(p, out, log=self.log)
                    if ok:
                        self.log(f"[解包] {msg}")
                        path = str(out)
                        self.current_target = path
                        self.path_edit.setText(path)
                    else:
                        self.log(f"[解包] 失败：{msg}")
        except Exception as e:
            self.log(f"[解包] 跳过（{e}）")

        try:
            self.detection = detect(path)
        except Exception as e:
            self.info.setText(f"识别失败：{e}")
            self.log(f"[识别] 失败: {e}")
            return
        d = self.detection
        if d.ok:
            extra = f"，{d.variant}" if d.variant else ""
            self.info.setText(
                f"✅ 识别到 {d.engine}{extra}\n"
                f"根目录：{d.root}\n"
                + ("\n".join(f"· {e}" for e in d.evidence[:4]))
                + ("\n\n⚠ " + "；".join(d.warnings) if d.warnings else "")
            )
            self.log(f"  → {d.summary()} @ {d.root}")
            for e in d.evidence:
                self.log(f"    · {e}")
            for w in d.warnings:
                self.log(f"    ! {w}")

            # Windows 版 Ren'Py：提醒并自动勾上移植
            if d.engine == "renpy":
                try:
                    from core import winport
                    if winport.is_windows_build(d.root):
                        self.port_check.setChecked(True)
                        self.log("[识别] Windows 版 Ren'Py → 已勾选「改造成 Linux 原生跑」")
                        self.info.setText(
                            self.info.text()
                            + "\n\n🐧 Windows 版：已勾上移植，会换成 Linux 引擎"
                            "原生运行（不需要 Wine）")
                except Exception as e:
                    self.log(f"[识别] 移植检查失败: {e}")
        else:
            self.info.setText("⚠ 没识别出引擎。已知线索：\n"
                              + "\n".join(f"· {e}" for e in d.evidence[:6]))
            self.log("  → 未识别，线索：")
            for e in d.evidence:
                self.log(f"    · {e}")

    # ------------------------------------------------------------ 术语表
    def on_glossary(self):
        text = "\n".join(f"{k} = {v}" for k, v in self.glossary.items())
        dlg = QMessageBox(self)
        dlg.setWindowTitle("术语表（每行一条：原文 = 译文）")
        dlg.setText("人名、专有名词在这里固定，全篇一致：\n\n"
                    "ご主人様 = 主人\nエレナ = 艾蕾娜")
        dlg.setDetailedText(text or "")
        # 用 QInputDialog 更直接
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getMultiLineText(
            self, "术语表", "每行一条：原文 = 译文（留空则清空）", text)
        if not ok:
            return
        g = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip():
                g[k.strip()] = v.strip()
        self.glossary = g
        self._save_glossary_file()
        self.glossary_btn.setText(f"编辑术语表 · {len(g)} 条")
        self.log(f"[术语] {len(g)} 条（已保存到 glossary.json）")

    def on_auto_glossary(self):
        """从当前游戏的台词里自动生成术语表。

        需要先有文本，所以这里先跑一次 detect+extract 拿 items，问模型，
        生成后弹预览让主人勾选，再存进 glossary.json。
        """
        if not self.current_target:
            QMessageBox.information(self, "还没有游戏",
                                    "先把这个游戏的启动文件拖进来，识别出引擎后再生成。")
            return
        if not self.detection or not self.detection.ok:
            QMessageBox.warning(self, "引擎没识别出来",
                                "先在日志里看识别线索，确认能提取文本再生成术语表。")
            return
        base = self.base_edit.text().strip()
        model = self.model_combo.currentText().strip()
        if not base or not model:
            QMessageBox.information(self, "先配好模型",
                                    "填 Base URL 和模型名（点「探测 / 刷新模型」）。")
            return

        backend = Backend(
            name=self.source_combo.currentText(),
            base_url=base,
            model=model,
            key=self.key_edit.text().strip(),
            wb=True if base.startswith("workbuddy:") else None,
        )
        if not self._preflight(backend):
            return

        self.log("[术语] 先提取文本…")
        opts = PipelineOptions(target=self.current_target, backend=backend)
        p = Pipeline(opts, emit=self._on_pipeline_emit)
        # prepare 不能漏：VX Ace 要在这里解 Game.rgss3a，没有它 extract 拿不到 Data/*.rvdata2
        p.run(steps=["detect", "prepare", "extract"])
        items = p.items
        if not items:
            QMessageBox.warning(self, "没有文本", "没能从游戏里提取到文本，无法分析术语。")
            return
        self.log(f"[术语] 提取到 {len(items)} 条")

        from core import glossary as gl
        self.log("[术语] 分析台词里的专有名词…（会调用模型）")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            merged = gl.build(backend, items,
                              root=p.det.root if p.det else self.detection.root,
                              engine=p.det.engine if p.det else self.detection.engine,
                              existing=self.glossary, log=self.log)
        except Exception as e:
            self.log(f"[术语] 生成失败: {e}")
            QMessageBox.warning(self, "生成失败", f"{type(e).__name__}: {e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        new = {k: v for k, v in merged.items() if k not in self.glossary}
        if not new:
            QMessageBox.information(self, "没有新发现",
                                    f"现有 {len(self.glossary)} 条已经覆盖了能识别到的专有名词。")
            return

        picked = self._pick_terms(new)
        if picked is None:
            self.log("[术语] 已取消，未改动术语表")
            return
        self.glossary.update(picked)
        self._save_glossary_file()
        self.glossary_btn.setText(f"编辑术语表 · {len(self.glossary)} 条")
        self.log(f"[术语] 新增 {len(picked)} 条，共 {len(self.glossary)} 条（已保存）")

    def _on_pipeline_emit(self, kind, payload):
        """把流水线的事件接到界面上（同步调用，不走线程）。"""
        if kind == "log":
            self.log(payload)
        elif kind == "stage":
            self.on_stage(payload.get("name", ""), payload.get("pct") or 0.0)
        elif kind == "progress":
            self.on_progress(payload.get("done", 0), payload.get("total", 0))

    def _pick_terms(self, new: dict[str, str]) -> dict[str, str] | None:
        """勾选界面。取消返回 None。"""
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QVBoxLayout,
                                       QListWidget, QListWidgetItem, QLabel)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"自动识别到 {len(new)} 个专有名词")
        dlg.resize(480, 460)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("勾选的会写进术语表，全篇译名固定。不需要的取消勾选。"))
        lst = QListWidget()
        for src, dst in sorted(new.items()):
            it = QListWidgetItem(f"{src}   →   {dst}")
            it.setData(Qt.UserRole, (src, dst))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            lst.addItem(it)
        lay.addWidget(lst)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return None
        out = {}
        for i in range(lst.count()):
            it = lst.item(i)
            if it.checkState() == Qt.Checked:
                src, dst = it.data(Qt.UserRole)
                out[src] = dst
        return out

    # ------------------------------------------------------------ 预检
    def _preflight(self, backend: Backend) -> bool:
        base = backend.base_url
        models = backend.list_models()
        if models:
            self.log(f"[预检] 端点正常，{len(models)} 个模型可用")
            if backend.model not in models:
                self.log(f"[预检] ⚠ 模型「{backend.model}」不在列表里，"
                         f"可能填错（列表里有: {', '.join(models[:5])}…）")
            return True
        # /models 不支持（一些代理/自建服务没有），退回最小 chat 探测
        old = backend.max_tokens
        backend.max_tokens = 8
        try:
            backend.chat("Reply with exactly: ok", "ok", temperature=0.0)
            self.log("[预检] 端点正常（/models 不可用，chat 探测通过）")
            return True
        except Exception as e:
            self.log(f"[预检] 端点不可用: {e}")
            self._msg(f"连不上 {base}\n\n{e}\n\n检查：地址对不对、服务起了没、"
                      f"要不要 API Key。")
            return False
        finally:
            backend.max_tokens = old

    # ------------------------------------------------------------ 运行
    def on_start(self):
        if not self.current_target:
            self._msg("先把游戏拖进来")
            return
        if not self.detection or not self.detection.ok:
            self._msg("引擎没识别出来，先在日志里看线索")
            return
        base = self.base_edit.text().strip()
        model = self.model_combo.currentText().strip()
        if not base or not model:
            self._msg("先填 Base URL 和模型名（点「探测 / 刷新模型」）")
            return

        backend = Backend(
            name=self.source_combo.currentText(),
            base_url=base,
            model=model,
            key=self.key_edit.text().strip(),
            wb=True if base.startswith("workbuddy:") else None,
        )
        # 开跑前先确认端点活着：填错地址时立刻报错，而不是跑一分钟后
        # 在日志里刷几千条「未成功」。
        # 顺序：先试 /models（几乎零成本，多数端点支持）；不支持再发一个
        # 1 token 的 chat。两步都失败才拦下。
        if not self._preflight(backend):
            return
        opts = PipelineOptions(
            target=self.current_target,
            backend=backend,
            glossary=self.glossary,
            batch_size=self.batch_spin.value(),
            max_escalate=self.ab_spin.value(),
            concurrency=self.conc_spin.value(),
            dry_run=self.dry_check.isChecked(),
            port_to_linux=self.port_check.isChecked(),
            nv_fallback=self.nv_check.isChecked(),
            gf_fallback=self.gf_check.isChecked(),
            simplify=self.simp_check.isChecked(),
            make_shortcut=self.shortcut_check.isChecked(),
            shortcut_on_desktop=self.shortcut_desktop_check.isChecked(),
            unity_runtime=self.unity_rt_check.isChecked(),
            # GUI 里不开自检：自检要占住一个游戏窗口 45 秒，从界面跑很怪。
            # 要查问题用 CLI 的 --unity-verify。
            unity_verify=False,
        )

        self.log_view.clear()
        self.bar.setValue(0)
        self.bar_text.setText("准备中…")
        self._t0 = 0.0
        self.stat_done.set_value("0")
        self.stat_remain.set_value("0")
        self.stat_speed.set_value("0.0")
        self.ring.set_value(0.0, "启动中")
        self.nav.select(1)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self.thread = QThread()
        self.worker = Worker(opts)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log)
        self.worker.stage.connect(self.on_stage)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.thread.start()

    def on_stop(self):
        if self.worker:
            self.log("[取消] 正在通知流水线停止…（已翻好的会保留在缓存里）")
            self.worker.stop()
            self.btn_stop.setEnabled(False)

    STAGE_LABEL = {
        "detect": "识别引擎", "prepare": "准备（解包/反编译）",
        "extract": "提取文本", "glossary": "生成术语表", "translate": "翻译",
        "apply": "写回游戏", "finalize": "字体与收尾",
        "runtime": "配置运行时替换",
        "shortcut": "创建快捷方式", "done": "完成",
    }

    def on_stage(self, name: str, pct: float):
        self.bar.setValue(int(max(0.0, min(1.0, pct)) * 1000))
        label = self.STAGE_LABEL.get(name, name)
        self._stage_name = label
        self.setWindowTitle(f"galgame-tl · {label}")
        self.stat_stage.set_value(label)
        self.ring.set_value(max(0.0, min(1.0, pct)), label)
        # 速度只在翻译阶段有意义：done 计的是「翻了多少句」，
        # 从识别阶段开始计时会把前面解包/提取的几十秒算进去，速度偏低。
        import time
        if name == "translate":
            if not self._t0:
                self._t0 = time.monotonic()
        else:
            self._t0 = 0.0

    def on_progress(self, done: int, total: int):
        # 注意 %v 是进度条的原始值（这里是 0~1000），不是百分比 ——
        # 直接拼 %v 会显示成 "470%"。百分比要自己算。
        frac = done / max(1, total)
        if total:
            self.bar_text.setText(f"翻译 {frac*100:.0f}%　·　{done:,} / {total:,} 句")
        self.bar.setValue(int(1000 * 0.25 + 600 * frac))

        # 运行页统计
        self.stat_done.set_value(f"{done:,}")
        self.stat_remain.set_value(f"{max(0, total - done):,}")
        self.ring.set_value(0.25 + 0.6 * frac, "翻译中")
        import time
        if self._t0 and done > 0:
            elapsed = max(1e-6, time.monotonic() - self._t0)
            self.stat_speed.set_value(f"{done / elapsed:.1f}")

    def on_done(self, result):
        self._cleanup_thread()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.setWindowTitle("galgame-tl · galgame 汉化工具")
        self.bar.setValue(1000)
        self.bar_text.setText("完成" if result.ok else "失败")
        self.ring.set_value(1.0 if result.ok else 0.0,
                            "完成" if result.ok else "失败")
        self.stat_stage.set_value("完成" if result.ok else "失败")
        self._t0 = 0.0
        self.log("=" * 50)
        self.log(result.summary())
        if result.json_path:
            self.log(f"提取结果: {result.json_path}")
        if result.ok:
            self.info.setText(self.info.text() + f"\n\n✅ 完成：{result.summary().splitlines()[0]}")
            self._notify_ok()
        else:
            self.info.setText(self.info.text() + f"\n\n❌ {result.error}")

    def on_failed(self, msg: str):
        self._cleanup_thread()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.ring.set_value(0.0, "失败")
        self.stat_stage.set_value("失败")
        self._t0 = 0.0
        self.log("[崩溃] " + msg)

    def _notify_ok(self):
        """跑完自动切到运行页看总结，省得主人自己点。"""
        self.nav.select(1)

    def _cleanup_thread(self):
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
        self.thread = None
        self.worker = None

    # ------------------------------------------------------------ 工具
    def log(self, msg: str):
        self.log_view.appendPlainText(msg)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _msg(self, text: str):
        QMessageBox.information(self, "galgame-tl", text)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG)
    # Fusion 是唯一在所有平台上都尊重 QSS 的内置样式；
    # 用系统原生样式（Windows / macOS）会让自绘控件半途而废。
    app.setStyle("Fusion")
    _init_palette(app)
    app.setStyleSheet(ui_theme.QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
