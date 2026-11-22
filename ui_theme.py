"""galgame-tl 界面主题（QSS）

设计取向
--------
深色现代风：低饱和背景 + 卡片分层 + 单一强调色（蓝紫渐变）。
不用纯黑（#000）也不用纯白，取 #0e1014 / #e8ecf3 这种带一点冷调的中性色，
长时间盯着不刺眼。分区靠「卡片 + 1px 描边」区分，不靠分割线堆叠。

Qt 的 QSS 不是 CSS，这些都用不了：box-shadow / CSS 变量 / ::before。
所以：
* 层次感靠 background 深浅差（三级：#0e1014 底 → #171b23 卡片 → #1e2430 输入）
* 「阴影」用一圈更暗的 border 模拟
* 圆角统一 10px（大卡）/ 8px（控件），保证视觉节奏一致

一处关键取舍：QSpinBox / QComboBox 的箭头不能用图片（没打包资源），
用 Unicode 字符 ▾ ▴ 画，跨平台都不会变豆腐块。
"""
from __future__ import annotations

# ---- 色板（改这里就能整体换色） ----------------------------------------
BG = "#0e1014"          # 窗口底
CARD = "#171b23"        # 卡片
CARD_HI = "#1e2430"     # 输入框 / 悬停
BORDER = "#262c38"      # 常规描边
BORDER_HI = "#39414f"   # 悬停描边
TEXT = "#e8ecf3"        # 主文字
TEXT_DIM = "#8b93a7"    # 次要文字
TEXT_MUTE = "#5c6478"   # 更弱（提示）
ACCENT = "#4f8cff"      # 强调色起点
ACCENT2 = "#7c5cff"     # 强调色终点
OK = "#3ddc97"
WARN = "#ffb84d"
ERR = "#ff6b6b"

ACCENT_GRAD = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2})"
ACCENT_GRAD_H = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #639bff, stop:1 #8d70ff)"


# ---- 图标（内联 SVG → 临时文件） ---------------------------------------
# 为什么不直接用 QSS 的 border 三角：Qt 在部分平台上会把它渲染成实心小方块
# （实测 offscreen 渲染就是这样）。
# 为什么不嵌 data URI：QSS 的 `image: url(data:...)` 实测在 Qt6 上不生效，
# 必须是真实文件路径。所以运行时把几个小 SVG 落到临时目录，QSS 引用绝对路径。
def _svg_markup(path_d: str, stroke: str, width: float = 1.6) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" '
        'viewBox="0 0 12 12"><path d="' + path_d + '" fill="none" stroke="'
        + stroke + '" stroke-width="' + str(width)
        + '" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def _write_icons() -> dict[str, str]:
    """把图标 SVG 写到缓存目录，返回 {名字: 绝对路径}。

    失败（只读文件系统 / 权限）时返回空 dict，QSS 里对应的 image 就留空，
    控件退化成「没有图标」而不是报错——宁可丑一点也不能启动不了。
    """
    import os
    import tempfile

    specs = {
        "down": _svg_markup("M2.5 4.5 L6 8 L9.5 4.5", TEXT_DIM),
        "up": _svg_markup("M2.5 7.5 L6 4 L9.5 7.5", TEXT_DIM),
        "check": _svg_markup("M2.5 6.2 L4.8 8.5 L9.5 3.8", "#ffffff", 1.9),
    }
    base = os.path.join(tempfile.gettempdir(), "galgame-tl-icons")
    out: dict[str, str] = {}
    try:
        os.makedirs(base, exist_ok=True)
        for name, svg in specs.items():
            path = os.path.join(base, name + ".svg")
            # 每次启动都重写：主题色可能改了，缓存旧图会跟色板对不上
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
    background: {BG};
    color: {TEXT};
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei",
                 "PingFang SC", "WenQuanYi Micro Hei", sans-serif;
    font-size: 13px;
}}

QMainWindow, QDialog {{
    background: {BG};
}}

QToolTip {{
    background: {CARD_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 6px 9px;
    font-size: 12px;
}}

/* ============================== 卡片 ============================== */
QGroupBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 20px;
    padding: 16px 14px 12px 14px;
    font-weight: 600;
    color: {TEXT};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: 2px;
    padding: 0 4px;
    color: {TEXT_DIM};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}}

/* 卡片内的子控件底色要跟卡片一致，否则 Qt 会给 QWidget 铺窗口底色 */
QGroupBox QWidget {{
    background: transparent;
}}

QGroupBox QLineEdit,
QGroupBox QComboBox,
QGroupBox QSpinBox,
QGroupBox QPlainTextEdit {{
    background: {CARD_HI};
}}

/* ============================== 输入控件 ============================== */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {CARD_HI};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
    min-height: 18px;
}}

QLineEdit:hover, QSpinBox:hover, QComboBox:hover, QPlainTextEdit:hover {{
    border-color: {BORDER_HI};
}}

QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
    background: {CARD_HI};
}}

QLineEdit:read-only {{
    color: {TEXT_DIM};
    background: rgba(30, 36, 48, 0.55);
}}

QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {TEXT_MUTE};
    background: rgba(30, 36, 48, 0.4);
}}

QLineEdit[placeholderText] {{
    color: {TEXT_MUTE};
}}

/* QSS 没有 ::placeholder，占位符颜色只能靠 palette，这里用属性兜底 */

/* --- 下拉框箭头 --- */
QComboBox::drop-down {{
    border: none;
    width: 26px;
    background: transparent;
}}

QComboBox::down-arrow {{
    image: {DOWN_ICON};
    width: 12px;
    height: 12px;
    margin-right: 8px;
}}

QComboBox QAbstractItemView {{
    background: {CARD_HI};
    border: 1px solid {BORDER_HI};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}

QComboBox QAbstractItemView::item {{
    padding: 6px 8px;
    border-radius: 6px;
    min-height: 20px;
}}

QComboBox QAbstractItemView::item:hover {{
    background: rgba(79, 140, 255, 0.18);
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

/* ============================== 按钮 ============================== */
QPushButton {{
    background: {CARD_HI};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 16px;
    color: {TEXT};
    font-weight: 500;
    min-height: 18px;
}}

QPushButton:hover {{
    background: #252d3b;
    border-color: {BORDER_HI};
}}

QPushButton:pressed {{
    background: #1a2029;
}}

QPushButton:disabled {{
    background: rgba(30, 36, 48, 0.45);
    color: {TEXT_MUTE};
    border-color: {BORDER};
}}

/* 主按钮：渐变 + 白字 */
QPushButton#primary {{
    background: {ACCENT_GRAD};
    border: none;
    color: #ffffff;
    font-size: 14px;
    font-weight: 700;
    border-radius: 10px;
    padding: 12px 22px;
}}

QPushButton#primary:hover {{
    background: {ACCENT_GRAD_H};
}}

QPushButton#primary:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #3f76e0, stop:1 #6a4ce0);
}}

QPushButton#primary:disabled {{
    background: #2a3140;
    color: {TEXT_MUTE};
}}

/* 次按钮：描边风 */
QPushButton#secondary {{
    background: transparent;
    border: 1px solid {BORDER_HI};
    color: {TEXT_DIM};
    border-radius: 10px;
    padding: 12px 22px;
    font-size: 14px;
}}

QPushButton#secondary:hover {{
    border-color: {ERR};
    color: {ERR};
    background: rgba(255, 107, 107, 0.08);
}}

QPushButton#secondary:disabled {{
    border-color: {BORDER};
    color: {TEXT_MUTE};
    background: transparent;
}}

/* 小工具按钮 */
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {BORDER};
    color: {TEXT_DIM};
    padding: 7px 12px;
    font-size: 12px;
}}

QPushButton#ghost:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
    background: rgba(79, 140, 255, 0.08);
}}

/* ============================== 复选框 ============================== */
QCheckBox {{
    spacing: 7px;
    color: {TEXT_DIM};
    padding: 2px;
}}

QCheckBox:hover {{
    color: {TEXT};
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER_HI};
    border-radius: 5px;
    background: {CARD_HI};
}}

QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}

QCheckBox::indicator:checked {{
    background: {ACCENT_GRAD};
    border-color: {ACCENT};
    image: {CHECK_ICON};
}}

/* ============================== 进度条 ============================== */
QProgressBar {{
    background: #141821;
    border: none;
    border-radius: 7px;
    height: 14px;
    text-align: center;
    color: {TEXT_DIM};
    font-size: 11px;
    font-weight: 600;
}}

QProgressBar::chunk {{
    background: {ACCENT_GRAD};
    border-radius: 7px;
}}

/* ============================== 日志区 ============================== */
QPlainTextEdit#log {{
    background: #0b0d11;
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 10px 12px;
    color: #a8b2c4;
    font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: {ACCENT};
}}

/* ============================== 滚动条 ============================== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: #333b4a;
    border-radius: 5px;
    min-height: 30px;
}}

QScrollBar::handle:vertical:hover {{
    background: #48536a;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    border: none;
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
    background: #333b4a;
    border-radius: 5px;
    min-width: 30px;
}}

QScrollBar::handle:horizontal:hover {{
    background: #48536a;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
    border: none;
}}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ============================== 消息框 ============================== */
QMessageBox {{
    background: {CARD};
}}

QMessageBox QLabel {{
    color: {TEXT};
    font-size: 13px;
}}

QMessageBox QPushButton {{
    min-width: 80px;
}}

/* ============================== 其它 ============================== */
QLabel {{
    background: transparent;
    color: {TEXT};
}}

QFrame#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
"""
