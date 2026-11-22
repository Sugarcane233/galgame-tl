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
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget, QTextEdit,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.detect import detect                       # noqa: E402
from core.llm import Backend                          # noqa: E402
from core.pipeline import Pipeline, PipelineOptions   # noqa: E402
import ui_theme                                       # noqa: E402

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
    card = QColor(ui_theme.CARD_HI)
    text = QColor(ui_theme.TEXT)
    dim = QColor(ui_theme.TEXT_MUTE)
    pal.setColor(QPalette.Window, bg)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, card)
    pal.setColor(QPalette.AlternateBase, QColor(ui_theme.CARD))
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.PlaceholderText, dim)
    pal.setColor(QPalette.Button, card)
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.Highlight, QColor(ui_theme.ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor(ui_theme.CARD_HI))
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
class DropZone(QFrame):
    dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName("dropzone")
        self.setMinimumHeight(110)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QFrame#dropzone {{
                border: 1.5px dashed {ui_theme.BORDER_HI};
                border-radius: 12px;
                background: rgba(23, 27, 35, 0.7);
            }}
            QFrame#dropzone[hover="true"] {{
                border: 1.5px dashed {ui_theme.ACCENT};
                background: rgba(79, 140, 255, 0.10);
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(6)

        self.icon = QLabel("⤓")
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setStyleSheet(
            f"color:{ui_theme.ACCENT}; font-size:26px; background:transparent;")
        lay.addWidget(self.icon)

        self.title = QLabel("把游戏目录 / 启动文件拖到这里")
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet(f"color:{ui_theme.TEXT}; background:transparent;")
        self.hint = QLabel("支持 Ren'Py · RPG Maker MV/MZ · VX Ace · Unity（Linux / Windows 版都行）")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(
            f"color:{ui_theme.TEXT_MUTE}; font-size:12px; background:transparent;")
        lay.addWidget(self.title)
        lay.addWidget(self.hint)

    def mousePressEvent(self, e):
        # 拖放区兼作按钮：点一下也能选目录（拖放不是每个人都会想到）
        self.window().on_pick_dir()
        super().mousePressEvent(e)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setProperty("hover", True)
            self.style().unpolish(self); self.style().polish(self)

    def dragLeaveEvent(self, e):
        self.setProperty("hover", False)
        self.style().unpolish(self); self.style().polish(self)

    def dropEvent(self, e: QDropEvent):
        self.setProperty("hover", False)
        self.style().unpolish(self); self.style().polish(self)
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p:
                self.dropped.emit(p)
                break


# ------------------------------------------------------------------- 主窗
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("galgame-tl · galgame 汉化工具")
        self.resize(1040, 760)
        self.settings = QSettings(ORG, APP_NAME)
        self.worker: Worker | None = None
        self.thread: QThread | None = None
        self.current_target = ""
        self.detection = None
        self.glossary: dict[str, str] = self._load_glossary_file()

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
    def _field_label(self, text: str) -> QLabel:
        """表单左侧的字段名。右对齐 + 次要色，跟输入框拉开层次。"""
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lbl.setStyleSheet(
            f"color:{ui_theme.TEXT_DIM}; font-size:12px; background:transparent;")
        return lbl

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # 1) 拖放区
        self.drop = DropZone()
        self.drop.dropped.connect(self.on_drop)
        root.addWidget(self.drop)

        pick = QHBoxLayout()
        pick.setSpacing(8)
        lbl = QLabel("目标")
        lbl.setStyleSheet(f"color:{ui_theme.TEXT_DIM}; font-size:12px;")
        pick.addWidget(lbl)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("或手动选择游戏目录…")
        self.path_edit.setReadOnly(True)
        btn_pick = QPushButton("选择目录")
        btn_pick.setObjectName("ghost")
        btn_pick.clicked.connect(self.on_pick_dir)
        pick.addWidget(self.path_edit, 1)
        pick.addWidget(btn_pick)
        root.addLayout(pick)

        # 2) 识别结果
        self.info = QLabel("等待拖入游戏…")
        self.info.setWordWrap(True)
        self.info.setObjectName("infobox")
        self.info.setStyleSheet(f"""
            QLabel#infobox {{
                background: rgba(79, 140, 255, 0.07);
                border: 1px solid rgba(79, 140, 255, 0.28);
                border-left: 3px solid {ui_theme.ACCENT};
                border-radius: 8px;
                padding: 10px 14px;
                color: {ui_theme.TEXT_DIM};
            }}
        """)
        root.addWidget(self.info)

        # 3) 模型配置
        gb_model = QGroupBox("翻译模型")
        ml = QGridLayout(gb_model)
        ml.setHorizontalSpacing(10)
        ml.setVerticalSpacing(10)
        ml.setColumnStretch(1, 1)
        ml.setColumnStretch(3, 1)

        ml.addWidget(self._field_label("来源"), 0, 0)
        self.source_combo = QComboBox()
        self.source_combo.addItems([
            "WorkBuddy（直连，免 token）",
            "WorkBuddy（本机网关，需 token）",
            "OpenAI 兼容（自定义）",
        ])
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        ml.addWidget(self.source_combo, 0, 1)

        ml.addWidget(self._field_label("Base URL"), 1, 0)
        self.base_edit = QLineEdit()
        self.base_edit.setPlaceholderText("http://127.0.0.1:11434/v1")
        ml.addWidget(self.base_edit, 1, 1, 1, 3)

        ml.addWidget(self._field_label("模型"), 2, 0)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ml.addWidget(self.model_combo, 2, 1)

        ml.addWidget(self._field_label("API Key"), 2, 2)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-…（本地端点可留空）")
        ml.addWidget(self.key_edit, 2, 3)

        btn_probe = QPushButton("探测 / 刷新模型")
        btn_probe.setObjectName("ghost")
        btn_probe.clicked.connect(self.on_probe)
        ml.addWidget(btn_probe, 3, 3)

        self.glossary_btn = QPushButton(f"术语表 · {len(self.glossary)} 条")
        self.glossary_btn.setObjectName("ghost")
        self.glossary_btn.setToolTip("手动编辑术语表（原文 = 译文，一行一条）")
        self.glossary_btn.clicked.connect(self.on_glossary)
        ml.addWidget(self.glossary_btn, 3, 1)

        self.auto_gloss_btn = QPushButton("自动生成术语表")
        self.auto_gloss_btn.setObjectName("ghost")
        self.auto_gloss_btn.setToolTip(
            "先用模型看完台词里的专有名词（人名/地名/招式），生成术语表再翻译。\n"
            "需要先拖入游戏并选好模型——要先提取文本才能分析。")
        self.auto_gloss_btn.clicked.connect(self.on_auto_glossary)
        ml.addWidget(self.auto_gloss_btn, 3, 2)

        root.addWidget(gb_model)

        # 4) 选项
        gb_opt = QGroupBox("选项")
        ov = QVBoxLayout(gb_opt)
        ov.setSpacing(12)

        # 第一行：三个数值参数
        ol = QHBoxLayout()
        ol.setSpacing(8)
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 100)
        self.batch_spin.setValue(20)
        self.batch_spin.setFixedWidth(78)
        ol.addWidget(self._field_label("每批句数"))
        ol.addWidget(self.batch_spin)

        ol.addSpacing(16)
        self.ab_spin = QSpinBox()
        self.ab_spin.setRange(1, 5)
        self.ab_spin.setValue(3)
        self.ab_spin.setFixedWidth(70)
        ol.addWidget(self._field_label("防拒绝重试"))
        ol.addWidget(self.ab_spin)

        ol.addSpacing(16)
        self.conc_spin = QSpinBox()
        self.conc_spin.setRange(1, 16)
        self.conc_spin.setValue(4)
        self.conc_spin.setFixedWidth(70)
        self.conc_spin.setToolTip("同时发出的请求数。串行翻两万句要十几小时，"
                                  "4 路实测不触发限速；被限速就降到 2。")
        ol.addWidget(self._field_label("并发"))
        ol.addWidget(self.conc_spin)
        ol.addStretch(1)
        ov.addLayout(ol)

        # 第二行：开关类
        oc = QHBoxLayout()
        oc.setSpacing(18)

        self.dry_check = QCheckBox("只翻译不写回（预览）")
        oc.addWidget(self.dry_check)

        self.nv_check = QCheckBox("NVIDIA 兜底")
        self.nv_check.setChecked(True)
        self.nv_check.setToolTip(
            "主模型（DeepSeek）对露骨性描写会直接内容过滤，或者在流中途掐断，\n"
            "实测约 3% 的台词过不去。勾上就把这些句子交给 NVIDIA 的\n"
            "nemotron-3-ultra——它同样带术语表和前后文，译名与语气能接上。\n"
            "key 自动从 DSH 凭据里读，读不到就自动关掉。")
        oc.addWidget(self.nv_check)

        self.gf_check = QCheckBox("谷歌翻译兜底")
        self.gf_check.setChecked(True)
        self.gf_check.setToolTip(
            "第三道防线：前两道（DeepSeek / NVIDIA）都拒的句子交给谷歌翻译。\n"
            "机器翻译不过滤文学性描写，剩下的露骨句它能翻（实测 60/60 全过）；\n"
            "质量比大模型差一档，但比留英文强。人名照样走术语表占位符。\n"
            "需要能连上谷歌（自动探环境变量代理与本机 7890 端口）。")
        oc.addWidget(self.gf_check)

        self.simp_check = QCheckBox("繁转简清洗")
        self.simp_check.setChecked(True)
        self.simp_check.setToolTip(
            "模型偶尔会在中文长句里滑到繁体（实测 TLS 8483 条译文有 1047 条混了繁体字）。\n"
            "勾上就在收尾时统一转成大陆简体，连词汇一起转（软体→软件、网路→网络）。\n"
            "需要 zhconv 库。")
        oc.addWidget(self.simp_check)

        self.port_check = QCheckBox("Windows 版改造成 Linux 原生跑")
        self.port_check.setToolTip(
            "Ren'Py 的 renpy/ 和 game/ 完全跨平台，只有 lib/py3-windows-* 是平台二进制。\n"
            "勾上就从本机其它 Ren'Py 游戏借一份 lib/py3-linux-*，直接原生运行——"
            "不是 Wine 转译，帧率和原生版一样。\n"
            "只在识别到 Windows 版 Ren'Py 时生效。")
        oc.addWidget(self.port_check)
        oc.addStretch(1)
        ov.addLayout(oc)

        root.addWidget(gb_opt)

        # 5) 操作按钮
        ops = QHBoxLayout()
        ops.setSpacing(10)
        self.btn_start = QPushButton("开始汉化")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumHeight(46)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop = QPushButton("取消")
        self.btn_stop.setObjectName("secondary")
        self.btn_stop.setMinimumHeight(46)
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)
        ops.addWidget(self.btn_start, 3)
        ops.addWidget(self.btn_stop, 1)
        root.addLayout(ops)

        # 6) 进度
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        self.bar.setFixedHeight(16)
        root.addWidget(self.bar)

        # 7) 日志
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        root.addWidget(self.log_view, 1)

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
        self.model_combo.clear()
        if models:
            self.model_combo.addItems(models)
            # 默认挑一个便宜的通用模型
            # hy4free 免费但实测极慢（4 句 79 秒，还会解析失败），
            # deepseek-v4.1-flash 是实测 1.6 秒/4 句的档位
            for pref in ("deepseek-v4.1-flash", "deepseek-v4-flash",
                         "fast-model", "hy4std", "auto"):
                if pref in models:
                    self.model_combo.setCurrentText(pref)
                    break
            self.log(f"[WorkBuddy] 上游提供 {len(models)} 个模型")
        else:
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
        self.glossary_btn.setText(f"术语表（{len(g)} 条）")
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
        self.glossary_btn.setText(f"术语表（{len(self.glossary)} 条）")
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
        )

        self.log_view.clear()
        self.bar.setValue(0)
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

    def on_stage(self, name: str, pct: float):
        self.bar.setValue(int(max(0.0, min(1.0, pct)) * 1000))
        label = {"detect": "识别引擎", "prepare": "准备（解包/反编译）",
                 "extract": "提取文本", "glossary": "生成术语表", "translate": "翻译",
                 "apply": "写回游戏", "finalize": "字体与收尾",
                 "done": "完成"}.get(name, name)
        self.setWindowTitle(f"galgame-tl · {label}")

    def on_progress(self, done: int, total: int):
        # 注意 %v 是进度条的原始值（这里是 0~1000），不是百分比 ——
        # 直接拼 %v 会显示成 "470%"。百分比要自己算。
        frac = done / max(1, total)
        if total:
            self.bar.setFormat(f"翻译 {frac*100:.0f}%  ({done}/{total})")
        self.bar.setValue(int(1000 * 0.25 + 600 * frac))

    def on_done(self, result):
        self._cleanup_thread()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.setWindowTitle("galgame-tl · galgame 汉化工具")
        self.bar.setValue(1000)
        self.bar.setFormat("完成" if result.ok else "失败")
        self.log("=" * 50)
        self.log(result.summary())
        if result.json_path:
            self.log(f"提取结果: {result.json_path}")
        if result.ok:
            self.info.setText(self.info.text() + f"\n\n✅ 完成：{result.summary().splitlines()[0]}")
        else:
            self.info.setText(self.info.text() + f"\n\n❌ {result.error}")

    def on_failed(self, msg: str):
        self._cleanup_thread()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log("[崩溃] " + msg)

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
