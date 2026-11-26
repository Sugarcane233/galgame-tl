"""galgame-tl 界面主题（QSS）

设计取向
--------
Fluent 式浅色界面（参照 AiNiee / Win11 应用）：

* 背景是极浅的冷灰白 ``#f5f6f8``，内容装在纯白卡片里，靠 1px 浅描边分层
* 左侧固定宽度导航栏，右侧是页面栈，页面内部用卡片纵向排列
* 强调色单一：``#3b7ddd``（蓝）。只有主按钮、选中项、进度用色
* 圆角 8px（控件）/ 12px（卡片），留白大，行距松

Qt 的 QSS 不是 CSS，这些用不了：box-shadow / CSS 变量 / ::before。
所以层次感靠「背景深浅差 + 1px 描边」，不用阴影。

为什么不打包图片资源：QSS 里的图标统一由 runtime 生成小 SVG 落到缓存目录
（见下面 _write_icons 与 ui_widgets.icon），仓库里不放二进制。
"""
from __future__ import annotations

import os

# ---- 色板（改这里就能整体换色） ----------------------------------------
BG = "#f5f6f8"          # 窗口底（极浅冷灰）
SIDEBAR = "#fbfbfd"     # 侧边栏
CARD = "#ffffff"        # 卡片
CARD_ALT = "#f7f8fa"    # 卡片内的次级块 / 输入框底
BORDER = "#e5e7eb"      # 常规描边
BORDER_HI = "#d3d7de"   # 悬停描边
TEXT = "#1f2329"        # 主文字
TEXT_DIM = "#5b6270"    # 次要文字
TEXT_MUTE = "#8b919c"   # 更弱（提示 / 单位）
ACCENT = "#3b7ddd"      # 强调色
ACCENT_HI = "#2f6ec4"   # 强调色悬停
ACCENT_SOFT = "#eaf1fb" # 强调色的浅底（选中态）
OK = "#12a150"
WARN = "#c47f17"
ERR = "#d93b3b"

ACCENT_GRAD = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 #5a91e8)"
ACCENT_GRAD_H = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_HI}, stop:1 #4f86da)"


# ---- 图标（SVG → 缓存文件） ---------------------------------------------
# 为什么不用 QSS 的 border 画三角：Qt 会把它渲染成实心小方块（实测 offscreen）。
# 为什么不嵌 data URI：QSS 的 `image: url(data:...)` 在 Qt6 上不生效，
# 必须给真实文件路径。所以运行时把几个小 SVG 落盘，QSS 引用绝对路径。
def _svg_markup(path_d: str, stroke: str, width: float = 1.6) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" '
        'viewBox="0 0 12 12"><path d="' + path_d + '" fill="none" stroke="'
        + stroke + '" stroke-width="' + str(width)
        + '" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def _icon_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "galgame-tl")


def _write_icons() -> dict[str, str]:
    """把 QSS 用的小图标写到缓存目录，返回 {名字: 绝对路径}。

    失败（只读文件系统 / 权限）时返回空 dict，QSS 里对应 image 退化成 none，
    控件只是「没图标」，不会启动失败。
    """
    specs = {
        "down": _svg_markup("M2.5 4.5 L6 8 L9.5 4.5", TEXT_MUTE),
        "up": _svg_markup("M2.5 7.5 L6 4 L9.5 7.5", TEXT_MUTE),
        "check": _svg_markup("M2.5 6.2 L4.8 8.5 L9.5 3.8", "#ffffff", 1.9),
    }
    out: dict[str, str] = {}
    try:
        base = _icon_dir()
        os.makedirs(base, exist_ok=True)
        for name, svg in specs.items():
            path = os.path.join(base, name + ".svg")
            # 每次启动重写：色板改了旧图标会跟界面打架
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
            out[name] = path.replace(os.sep, "/")
    except Exception:
        return {}
    return out


_ICONS = _write_icons()


def _icon_url(name: str) -> str:
    p = _ICONS.get(name)
    return f"url({p})" if p else "none"


DOWN_ICON = _icon_url("down")
UP_ICON = _icon_url("up")
CHECK_ICON = _icon_url("check")


QSS = f"""
/* ============================== 基础 ============================== */
QWidget {{
    color: {TEXT};
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei",
                 "PingFang SC", "WenQuanYi Micro Hei", sans-serif;
    font-size: 13px;
}}

QMainWindow, QDialog, QStackedWidget, QWidget#pageRoot {{
    background: {BG};
}}

QWidget#pageFooter {{
    background: {CARD};
    border-top: 1px solid {BORDER};
}}

QWidget#pageRoot {{
    background: {BG};
}}

QToolTip {{
    background: #ffffff;
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 6px 9px;
    font-size: 12px;
}}

/* ============================== 侧边栏 ============================== */
QWidget#sidebar {{
    background: {SIDEBAR};
    border-right: 1px solid {BORDER};
}}

QLabel#brandName {{
    font-size: 14px;
    font-weight: 700;
    color: {TEXT};
}}

QLabel#brandCap {{
    font-size: 11px;
    color: {TEXT_MUTE};
}}

QLabel#navGroup {{
    font-size: 11px;
    font-weight: 600;
    color: {TEXT_MUTE};
    padding-left: 10px;
    letter-spacing: 0.4px;
}}

QPushButton#nav {{
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 8px 10px;
    text-align: left;
    color: {TEXT_DIM};
    font-size: 13px;
}}

QPushButton#nav:hover {{
    background: #f0f1f4;
    color: {TEXT};
}}

QPushButton#nav:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    font-weight: 600;
}}

/* ============================== 卡片 ============================== */
QFrame#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}

QFrame#statCard {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QLabel#cardTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {TEXT};
}}

QLabel#cardSubtitle {{
    font-size: 12px;
    color: {TEXT_MUTE};
}}

QLabel#statTitle {{
    font-size: 12px;
    color: {TEXT_MUTE};
}}

QLabel#statValue {{
    font-size: 26px;
    font-weight: 700;
    color: {TEXT};
}}

QLabel#statText {{
    font-size: 17px;
    font-weight: 600;
    color: {TEXT};
}}

QLabel#statUnit {{
    font-size: 12px;
    color: {TEXT_MUTE};
    padding-bottom: 5px;
}}

QLabel#pageTitle {{
    font-size: 19px;
    font-weight: 700;
    color: {TEXT};
}}

QLabel#pageDesc {{
    font-size: 12px;
    color: {TEXT_MUTE};
}}

QFrame#hline {{
    background: {BORDER};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}

/* ============================== 输入控件 ============================== */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {CARD_ALT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    min-height: 18px;
}}

QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{
    border-color: {BORDER_HI};
}}

QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
    background: #ffffff;
}}

QLineEdit:read-only {{
    color: {TEXT_DIM};
    background: {CARD_ALT};
}}

QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {TEXT_MUTE};
    background: #f0f1f4;
}}

/* --- 下拉框箭头 --- */
QComboBox::drop-down {{
    border: none;
    width: 26px;
    background: transparent;
}}

QComboBox::down-arrow {{
    image: {DOWN_ICON};
    width: 11px;
    height: 11px;
    margin-right: 9px;
}}

/* 弹出列表是独立的 top-level 窗口，QSS 必须显式给 background-color：
   只写 background 会被继承成透明，露出黑色窗口底 —— 实测就是一整片黑。
   padding 也别留，那块不画 item，会露出边框色。 */
QComboBox {{
    combobox-popup: 0;
}}

QComboBox QAbstractItemView {{
    background-color: #ffffff;
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 0px;
    margin: 0px;
    outline: 0;
    color: {TEXT};
    selection-background-color: {ACCENT_SOFT};
    selection-color: {ACCENT};
}}

QComboBox QAbstractItemView::item {{
    padding: 7px 10px;
    min-height: 22px;
    border: none;
    background-color: transparent;
    color: {TEXT};
}}

QComboBox QAbstractItemView::item:selected {{
    background-color: {ACCENT_SOFT};
    color: {ACCENT};
}}

QComboBox QAbstractItemView::item:hover {{
    background: {CARD_ALT};
}}

/* --- 数字框箭头 --- */
QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 16px;
    height: 11px;
    margin-right: 4px;
}}

QSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    margin-top: 3px;
}}

QSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    margin-bottom: 3px;
}}

QSpinBox::up-arrow {{
    image: {UP_ICON};
    width: 9px; height: 9px;
}}

QSpinBox::down-arrow {{
    image: {DOWN_ICON};
    width: 9px; height: 9px;
}}

/* ============================== 复选 / 单选 ============================== */
QCheckBox, QRadioButton {{
    spacing: 8px;
    color: {TEXT_DIM};
    background: transparent;
    padding: 1px 0;
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1px solid {BORDER_HI};
    background: #ffffff;
}}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {ACCENT};
}}

QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: {CHECK_ICON};
}}

QCheckBox:checked {{
    color: {TEXT};
}}

/* ============================== 按钮 ============================== */
QPushButton {{
    background: #ffffff;
    border: 1px solid {BORDER_HI};
    border-radius: 7px;
    padding: 8px 16px;
    color: {TEXT};
    font-weight: 500;
    min-height: 18px;
}}

QPushButton:hover {{
    background: {CARD_ALT};
    border-color: {BORDER_HI};
}}

QPushButton:pressed {{
    background: #eceef2;
}}

QPushButton:disabled {{
    background: #f2f3f5;
    color: {TEXT_MUTE};
    border-color: {BORDER};
}}

/* 主按钮：实心蓝 + 白字 */
QPushButton#primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-size: 14px;
    font-weight: 600;
    border-radius: 9px;
    padding: 11px 22px;
}}

QPushButton#primary:hover {{
    background: {ACCENT_HI};
    border-color: {ACCENT_HI};
}}

QPushButton#primary:disabled {{
    background: #b9cde9;
    border-color: #b9cde9;
    color: #ffffff;
}}

/* 次级按钮：白底描边 */
QPushButton#secondary {{
    background: #ffffff;
    border: 1px solid {BORDER_HI};
    color: {TEXT_DIM};
    font-size: 14px;
    border-radius: 9px;
    padding: 11px 20px;
}}

QPushButton#secondary:hover {{
    background: {CARD_ALT};
    color: {TEXT};
}}

/* 幽灵按钮：卡片标题栏里的小工具 */
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {BORDER};
    color: {TEXT_DIM};
    font-size: 12px;
    padding: 5px 12px;
    border-radius: 6px;
    min-height: 16px;
}}

QPushButton#ghost:hover {{
    background: {CARD_ALT};
    color: {TEXT};
    border-color: {BORDER_HI};
}}

/* ============================== 拖放区 ============================== */
QFrame#dropzone {{
    background: {CARD_ALT};
    border: 1.5px dashed {BORDER_HI};
    border-radius: 12px;
}}

QFrame#dropzone[hover="true"] {{
    background: {ACCENT_SOFT};
    border: 1.5px dashed {ACCENT};
}}

/* ============================== 提示条 ============================== */
QLabel#infobox {{
    background: {ACCENT_SOFT};
    border: 1px solid #d6e4f7;
    border-radius: 9px;
    padding: 12px 16px;
    color: {TEXT_DIM};
}}

QLabel#warnbox {{
    background: #fdf6e7;
    border: 1px solid #f2e2c2;
    border-radius: 9px;
    padding: 12px 16px;
    color: {WARN};
}}

/* ============================== 进度条 ============================== */
QProgressBar {{
    background: #eceef2;
    border: none;
    border-radius: 5px;
    text-align: center;
    color: {TEXT_DIM};
    font-size: 11px;
}}

QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 5px;
}}

/* ============================== 日志 ============================== */
QPlainTextEdit#log {{
    background: {CARD_ALT};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 12px 14px;
    color: {TEXT_DIM};
    font-family: "JetBrains Mono", "Cascadia Mono", "DejaVu Sans Mono",
                 "Noto Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}

/* ============================== 滚动条 ============================== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: #cfd3da;
    border-radius: 5px;
    min-height: 30px;
}}

QScrollBar::handle:vertical:hover {{
    background: #b6bbc4;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}

QScrollBar::handle:horizontal {{
    background: #cfd3da;
    border-radius: 5px;
    min-width: 30px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

QScrollArea {{
    background: transparent;
    border: none;
}}

/* ============================== 对话框 ============================== */
QMessageBox, QDialog {{
    background: {CARD};
}}

QMessageBox QLabel, QDialog QLabel {{
    color: {TEXT};
    font-size: 13px;
}}

QMessageBox QPushButton, QDialog QPushButton {{
    min-width: 80px;
}}

QListWidget {{
    background: {CARD_ALT};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 6px;
    outline: none;
}}

QListWidget::item {{
    padding: 6px 8px;
    border-radius: 6px;
}}

QListWidget::item:selected {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
}}

QListWidget::item:hover {{
    background: {CARD_ALT};
}}

/* ============================== 其它 ============================== */
QLabel {{
    background: transparent;
    color: {TEXT};
}}

QFrame#card QLabel, QFrame#statCard QLabel {{
    background: transparent;
}}
"""
