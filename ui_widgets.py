"""通用 UI 组件：卡片、统计块、进度环、侧边导航。

只负责结构与几何；配色和字体全部交给 ui_theme.QSS，
这样换主题只要改 ui_theme 一处。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import ui_theme


# ---------------------------------------------------------------- 图标
# 24×24 线性图标。QSS 不认 data URI（见 ui_theme 的说明），所以统一落成文件，
# 再转成 QIcon —— 侧边栏导航要用 QIcon，不能走 QSS 的 image:。
_ICON_PATHS = {
    # 文件夹 / 项目
    "folder": "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z",
    # 翻译 / 语言
    "translate": "M4 5h9M8.5 3v2c0 4-2 6.5-4.5 7.5M5.5 9c1 2 3 3.5 5 4M13 20l4-10 4 10M14.5 16.5h5",
    # 播放 / 运行
    "play": "M8 5.5v13l11-6.5-11-6.5z",
    # 设置
    "settings": "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0-1.1-2.7H1a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 2.6 7a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H7a1.6 1.6 0 0 0 1-1.5V1a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V7a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z",
    # 日志 / 列表
    "log": "M4 5h16M4 10h16M4 15h10M4 20h7",
    # 术语表 / 书
    "book": "M4 5a2 2 0 0 1 2-2h11v18H6a2 2 0 0 1-2-2V5zM17 3h1a2 2 0 0 1 2 2v14",
    # 对勾（成功）
    "check": "M4 12.5 9 17.5 20 6.5",
    # 时钟
    "clock": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3.5 2",
    # 速度
    "speed": "M4 18a9 9 0 1 1 16 0M12 18l4-5",
    # 文档
    "doc": "M6 3h8l4 4v14H6V3zM14 3v4h4",
    # 闪电
    "bolt": "M13 2 4 14h6l-1 8 9-12h-6l1-8z",
    # 游戏库 / 网格
    "grid": "M4 4h6v6H4V4zM14 4h6v6h-6V4zM4 14h6v6H4v-6zM14 14h6v6h-6v-6z",
    # 垃圾桶
    "trash": "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6",
    # 刷新 / 扫描
    "refresh": "M20 12a8 8 0 1 1-2.3-5.6M20 4v4h-4",
    # 加号
    "plus": "M12 5v14M5 12h14",
    # 打开外部 / 跳转
    "external": "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
    # 硬盘 / 存储
    "disk": "M3 12h18M5 12l2-7h10l2 7M5 12v6a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-6M8 16h.01M12 16h.01",
}

_ICON_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
    "galgame-tl",
)
_ICON_CACHE: dict[str, QIcon] = {}


def icon(name: str, color: str = ui_theme.TEXT_DIM, size: int = 24) -> QIcon:
    """取一枚线性图标。失败返回空 QIcon（宁可没图标，也不要崩）。"""
    key = f"{name}-{color}-{size}"
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    path_d = _ICON_PATHS.get(name)
    if not path_d:
        return QIcon()

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        'viewBox="0 0 24 24">'
        f'<path d="{path_d}" fill="none" stroke="{color}" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )
    try:
        os.makedirs(_ICON_DIR, exist_ok=True)
        p = os.path.join(_ICON_DIR, f"{key}.svg")
        with open(p, "w", encoding="utf-8") as f:
            f.write(svg)
        pm = QPixmap(p)
        ic = QIcon(pm) if not pm.isNull() else QIcon()
    except Exception:
        ic = QIcon()
    _ICON_CACHE[key] = ic
    return ic


# ---------------------------------------------------------------- 卡片
class Card(QFrame):
    """Fluent 风格卡片：标题行 + 副标题 + 内容区。

    内容区是 `self.body`（QVBoxLayout），调用方往里塞控件；
    标题行右侧可以用 `add_action(widget)` 挂按钮。
    """

    def __init__(self, title: str = "", subtitle: str = "",
                 icon_name: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 20)
        outer.setSpacing(0)

        if title:
            head = QHBoxLayout()
            head.setSpacing(12)
            head.setContentsMargins(0, 0, 0, 0)

            if icon_name:
                ic = QLabel()
                ic.setPixmap(icon(icon_name, ui_theme.TEXT_DIM, 22).pixmap(22, 22))
                ic.setFixedSize(22, 22)

                head.addWidget(ic, 0, Qt.AlignTop)

            textbox = QVBoxLayout()
            textbox.setSpacing(3)
            textbox.setContentsMargins(0, 0, 0, 0)
            self.title_label = QLabel(title)
            self.title_label.setObjectName("cardTitle")
            
            textbox.addWidget(self.title_label)
            if subtitle:
                self.subtitle_label = QLabel(subtitle)
                self.subtitle_label.setObjectName("cardSubtitle")
                self.subtitle_label.setWordWrap(True)
                
                textbox.addWidget(self.subtitle_label)
            head.addLayout(textbox, 1)

            self._actions = QHBoxLayout()
            self._actions.setSpacing(8)
            head.addLayout(self._actions)

            outer.addLayout(head)
            outer.addSpacing(16)

        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        self.body.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.body)

    def add_action(self, w: QWidget):
        self._actions.addWidget(w)


class StatCard(QFrame):
    """统计卡片：小标题 + 大数字 + 单位。运行页那一排用它。"""

    def __init__(self, title: str, unit: str = "", icon_name: str | None = None,
                 big: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(96)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        if icon_name:
            ic = QLabel()
            ic.setPixmap(icon(icon_name, ui_theme.TEXT_MUTE, 16).pixmap(16, 16))
            ic.setFixedSize(16, 16)

            head.addWidget(ic)
        t = QLabel(title)
        t.setObjectName("statTitle")

        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("statValue" if big else "statText")

        row.addWidget(self.value_label, 0, Qt.AlignBottom)
        self.unit_label = QLabel(unit)
        self.unit_label.setObjectName("statUnit")

        row.addWidget(self.unit_label, 0, Qt.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)

    def set_value(self, text: str, unit: str | None = None):
        self.value_label.setText(text)
        if unit is not None:
            self.unit_label.setText(unit)


class ProgressRing(QWidget):
    """圆环进度。中间显示状态文字 + 百分比（AiNiee 那种）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self._caption = "等待开始"
        self.setMinimumSize(150, 150)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_value(self, v: float, caption: str | None = None):
        self._value = max(0.0, min(1.0, v))
        if caption is not None:
            self._caption = caption
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = min(self.width(), self.height())
        pad = 10.0
        rect = QRectF((self.width() - w) / 2 + pad, (self.height() - w) / 2 + pad,
                      w - 2 * pad, w - 2 * pad)

        # 底环
        pen = QPen(QColor(ui_theme.BORDER))
        pen.setWidthF(9.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 0, 360 * 16)

        # 进度弧：从 12 点方向顺时针
        if self._value > 0:
            grad = QColor(ui_theme.ACCENT)
            pen = QPen(grad)
            pen.setWidthF(9.0)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, 90 * 16, -int(360 * 16 * self._value))

        # 中间文字
        p.setPen(QColor(ui_theme.TEXT))
        f = QFont()
        f.setPointSize(19)
        f.setBold(True)
        p.setFont(f)
        pct = f"{self._value * 100:.0f}%"
        p.drawText(rect.adjusted(0, -8, 0, -8), Qt.AlignCenter, pct)

        p.setPen(QColor(ui_theme.TEXT_MUTE))
        f2 = QFont()
        f2.setPointSize(9)
        p.setFont(f2)
        p.drawText(rect.adjusted(0, 26, 0, 26), Qt.AlignCenter, self._caption)


# ---------------------------------------------------------------- 导航
class NavButton(QPushButton):
    """侧边栏导航项。选中时由 QSS 加左侧竖条和高亮底。"""

    def __init__(self, text: str, icon_name: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("nav")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(38)
        self.setIcon(icon(icon_name, ui_theme.TEXT_DIM, 18))
        self.setIconSize(self.iconSize().expandedTo(self.iconSize()))
        self._icon_name = icon_name
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, on: bool):
        color = ui_theme.ACCENT if on else ui_theme.TEXT_DIM
        self.setIcon(icon(self._icon_name, color, 18))


class Sidebar(QWidget):
    """左侧导航栏。分组标题 + 页面按钮，点击发 page_changed(索引)。"""

    page_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(212)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 16, 12, 12)
        lay.setSpacing(2)

        # 品牌区
        brand = QHBoxLayout()
        brand.setContentsMargins(8, 0, 8, 0)
        brand.setSpacing(9)
        logo = QLabel("🐟")
        logo.setStyleSheet("font-size:19px;")
        brand.addWidget(logo)
        tb = QVBoxLayout()
        tb.setSpacing(0)
        name = QLabel("galgame-tl")
        name.setObjectName("brandName")

        tb.addWidget(name)
        cap = QLabel("galgame 汉化工具")
        cap.setObjectName("brandCap")

        tb.addWidget(cap)
        brand.addLayout(tb)
        brand.addStretch(1)
        lay.addLayout(brand)
        lay.addSpacing(18)

        self.buttons: list[NavButton] = []

    def add_group(self, title: str):
        lbl = QLabel(title)
        lbl.setObjectName("navGroup")

        lay = self.layout()
        lay.addSpacing(10)
        lay.addWidget(lbl)
        lay.addSpacing(4)

    def add_page(self, text: str, icon_name: str) -> int:
        btn = NavButton(text, icon_name)
        btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        idx = len(self.buttons)
        btn.clicked.connect(lambda _, i=idx: self.select(i))
        self.buttons.append(btn)
        self.layout().addWidget(btn)
        return idx

    def finish(self):
        """所有节点加完后调用：把剩余空间都堆到底部。"""
        self.layout().addStretch(1)

    def add_footer(self, widget: QWidget):
        self.layout().addStretch(1)
        self.layout().addWidget(widget)

    def select(self, idx: int):
        for i, b in enumerate(self.buttons):
            b.setChecked(i == idx)
        self.page_changed.emit(idx)
