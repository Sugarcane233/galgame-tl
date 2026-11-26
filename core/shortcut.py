# -*- coding: utf-8 -*-
"""给汉化完的游戏生成启动快捷方式（.desktop）。

需求的来源：翻完一个游戏，玩家还得自己跑到下载目录里找那个 .x86_64 / .sh
双击，翻过的十几个游戏放在一起分不清谁是谁。这里做一件事——把游戏的
启动器和**它自己的图标**绑成一个桌面条目，图标一律用游戏原图，绝不换成
galgame-tl 的图标（那样主人一眼看不出是哪个游戏）。

图标从哪来（按可靠性排序，各引擎不同）
--------------------------------------
Ren'Py   game/gui/window_icon.png（256x256 RGBA，几乎每个游戏都有）
         → 退回 game/icon.png、根目录 icon.png
RPG MV/MZ 根目录 icon/icon.png（nw.js 的 window.icon）
         → 退回 package.json 里 window.icon 指的文件
RPG VX Ace 没有图标文件，从 Game.exe 里抠 PE 资源（vendor 里有 pefile？没有，
         那就退回用窗口标题生成字母图标，见下）
Unity    Linux 版的图标编译进可执行文件，静态抠不出来；
         _Data 里偶有 icon.png，多数没有 → 退回字母图标

「字母图标」是最后的兜底：用游戏的显示名首字 + 固定配色画一张 256x256 PNG。
它不是游戏原图标，但至少同一个游戏每次生成的完全一样（颜色由名字哈希决定），
而且比空图标好看。

.desktop 写到哪里
-----------------
~/.local/share/applications/<slug>.desktop  （开始菜单 / 应用列表可见）
桌面上的 .desktop 文件（~/桌面、~/Desktop，存在哪个写哪个）是另一回事：
GNOME 需要把它标记为 trusted 才会显示成图标，这里尽力而为，失败只记日志。

幂等
----
同一游戏重复生成会覆盖同一个文件（slug 由目录名决定），不会堆一堆副本。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# 图标搜索：相对游戏根目录，按优先级排列
_ICON_CANDIDATES = {
    "renpy": (
        "game/gui/window_icon.png",
        "game/icon.png",
        "game/gui/icon.png",
        "icon.png",
        "game/gui/main_menu.png",
    ),
    "rpgmaker": (
        "icon/icon.png",
        "icon/icon.ico",
        "www/icon/icon.png",
        "Game/icon/icon.png",
    ),
    "unity": (
        "icon.png",
        "Icon.png",
        "*_Data/icon.png",
        "*_Data/*.png",
    ),
    "rpgmaker_vxace": (
        "icon.png",
        "Icon.png",
    ),
}

# 字母图标配色：按名字哈希取，保证同一游戏每次一样
_LETTER_COLORS = (
    (0x3B, 0x7D, 0xDD), (0x7C, 0x5C, 0xFF), (0x2E, 0xA0, 0x6A),
    (0xD9, 0x7B, 0x2E), (0xC0, 0x4A, 0x6B), (0x2A, 0x9D, 0xB8),
    (0x8A, 0x5A, 0xC0), (0xB0, 0x8A, 0x2E),
)

# 这些字段里的引号/反斜杠要转义，否则 .desktop 解析出错
_SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


@dataclass
class ShortcutResult:
    ok: bool = False
    desktop_path: str = ""          # ~/.local/share/applications 下那个
    desktop_icon_path: str = ""     # 拷贝出来的图标
    icon_source: str = ""           # original / generated
    launch: str = ""                # 实际的启动命令
    messages: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"快捷方式生成失败：{self.error}"
        src = "游戏原图标" if self.icon_source == "original" else "生成的字母图标"
        return f"快捷方式已创建（图标：{src}）"


# ---------------------------------------------------------------- 定位

def _launch_candidates(root: Path, det) -> list[Path]:
    """按可靠性列出可能的启动器。先看 detect 给的，再自己扫。"""
    out: list[Path] = []
    seen: set[str] = set()

    def add(p):
        if not p:
            return
        pp = Path(p)
        if not pp.is_absolute():
            pp = root / pp
        try:
            rp = str(pp.resolve())
        except Exception:
            rp = str(pp)
        if rp in seen or not pp.is_file():
            return
        seen.add(rp)
        out.append(pp)

    if det is not None and getattr(det, "launch", None):
        add(det.launch)
    # 引擎各自的常见命名
    for pat in ("*.x86_64", "*.x86", "*.sh", "*.AppImage", "Game", "nw", "nw.exe",
                "*.exe"):
        for p in sorted(root.glob(pat)):
            if p.is_file():
                add(p)
    return out


def _pick_launch(root: Path, det) -> Optional[Path]:
    """挑一个真正能跑的启动器。

    优先级：detect 的结论 > 根目录可执行的 *.x86_64 / *.AppImage > *.sh > Game
    排除明显不是游戏本体的（uninstall、crash handler、dotnet 之类）。
    """
    bad = re.compile(
        r"(unins|uninst|crash|handler|UnityCrashHandler|dotnet|mono|"
        r"steam_api|linuxdeploy|\.so$)", re.I)
    cands = [p for p in _launch_candidates(root, det)
             if not bad.search(p.name)]
    if not cands:
        return None

    # 装了 BepInEx（Unity 运行时汉化）就必须走 run_bepinex.sh，直接开游戏本体
    # 会绕过 doorstop，词表根本不加载 —— 玩家看到的是英文，还以为汉化没生效。
    if (root / "BepInEx" / "core" / "BepInEx.Preloader.dll").is_file():
        runner = root / "run_bepinex.sh"
        if runner.is_file():
            runner.chmod(0o755)
            return runner

    def score(p: Path) -> tuple:
        ex = os.access(p, os.X_OK)
        n = p.name.lower()
        s = 0
        if det is not None and getattr(det, "launch", None):
            try:
                if p.resolve() == Path(det.launch).resolve():
                    s += 100
            except Exception:
                pass
        if n.endswith(".x86_64") or n.endswith(".x86"):
            s += 40
        elif n.endswith(".appimage"):
            s += 35
        elif n.endswith(".sh"):
            s += 20
        elif n == "game":
            s += 15
        elif n.endswith(".exe"):
            s -= 10            # .exe 在 Linux 上跑不了，只能是 hint
        if ex:
            s += 10
        return (-s, len(p.name), n)

    cands.sort(key=score)
    return cands[0]


def _game_title(root: Path) -> str:
    """游戏的显示名：优先 package.json 的 window.title，否则用目录名。"""
    pj = root / "package.json"
    if pj.is_file():
        try:
            d = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
            w = d.get("window") or {}
            t = (w.get("title") or d.get("displayName") or d.get("name") or "").strip()
            if t:
                return t
        except Exception:
            pass
    return root.name


def _slug(text: str, extra: str = "") -> str:
    """做文件名用的稳定 slug。带 extra（游戏根路径）的哈希避免同名撞车。"""
    base = _SAFE_NAME.sub("-", text).strip("-")[:48] or "galgame"
    h = hashlib.sha1((str(text) + "|" + extra).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{h}"


# ---------------------------------------------------------------- 图标

def find_icon(root: Path, engine: str) -> Optional[Path]:
    """找游戏自带的图标文件。找不到返回 None。"""
    pats = _ICON_CANDIDATES.get(engine or "", ()) + _ICON_CANDIDATES["renpy"][:2]
    for pat in pats:
        if "*" in pat:
            for p in sorted(root.glob(pat)):
                if p.is_file() and p.stat().st_size > 512:
                    return p
            continue
        p = root / pat
        if p.is_file() and p.stat().st_size > 512:
            return p
    # 从 package.json 的 window.icon 里取（MZ 有些游戏改了路径）
    pj = root / "package.json"
    if pj.is_file():
        try:
            d = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
            ic = ((d.get("window") or {}).get("icon") or "").strip()
            if ic:
                p = root / ic
                if p.is_file() and p.stat().st_size > 512:
                    return p
        except Exception:
            pass
    return None


def _make_letter_icon(title: str, out: Path) -> bool:
    """用标题首字画一张 256x256 圆角底 + 白色字的 PNG。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    size = 256
    h = int(hashlib.sha1(title.encode("utf-8")).hexdigest(), 16)
    r, g, b = _LETTER_COLORS[h % len(_LETTER_COLORS)]
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    # 圆角方底，半径 20% 是现代图标的主流比例
    dr.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5,
                         fill=(r, g, b, 255))
    ch = (title.strip()[:1] or "G").upper()
    font = None
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ):
        if os.path.exists(cand):
            try:
                font = ImageFont.truetype(cand, size // 2)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    try:
        box = dr.textbbox((0, 0), ch, font=font)
        w, hh = box[2] - box[0], box[3] - box[1]
        dr.text(((size - w) / 2 - box[0], (size - hh) / 2 - box[1]), ch,
                font=font, fill=(255, 255, 255, 255))
    except Exception:
        dr.text((size // 3, size // 3), ch, font=font, fill=(255, 255, 255, 255))
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, "PNG")
        return True
    except Exception:
        return False


def prepare_icon(root: Path, engine: str, title: str, dest_dir: Path,
                 log: Callable[[str], None]) -> tuple[Optional[Path], str]:
    """把图标落到 dest_dir，返回 (路径, 来源)。来源是 original / generated / 空。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = find_icon(root, engine)
    if src is not None:
        out = dest_dir / "icon.png"
        try:
            if src.suffix.lower() == ".png":
                shutil.copy2(src, out)
                return out, "original"
            # .ico / .jpg 之类转成 png：.desktop 的 Icon= 只认 png/svg/xpm
            try:
                from PIL import Image
                im = Image.open(src).convert("RGBA")
                im.save(out, "PNG")
                return out, "original"
            except Exception as e:
                log(f"  ! 图标 {src.name} 转 PNG 失败（{e}），改用生成图标")
        except Exception as e:
            log(f"  ! 拷贝图标失败（{e}），改用生成图标")

    out = dest_dir / "icon.png"
    if _make_letter_icon(title, out):
        return out, "generated"
    return None, ""


# ---------------------------------------------------------------- .desktop

def _exec_arg(a: str) -> str:
    """把单个参数转成 Exec= 里的写法。

    .desktop 的 Exec 是按空白切参数的，所以**含空格的单个参数**要加引号；
    双引号内的 `"`、`` ` ``、`$`、`\\` 要反斜杠转义（freedesktop 规范）。
    注意：不能把整条命令用引号包起来 —— 那样会被当成一个可执行文件名。
    """
    special = " \t\n\"\\$`"
    if a == "" or any(c in a for c in special):
        esc = (a.replace("\\", "\\\\").replace('"', '\\"')
               .replace("`", "\\`").replace("$", "\\$"))
        return f'"{esc}"'
    return a


def _quote_exec(*args: str) -> str:
    return " ".join(_exec_arg(a) for a in args)


def _desktop_entry(title: str, exec_args: tuple[str, ...], workdir: Path,
                   icon_path: Optional[Path]) -> str:
    def one_line(s: str) -> str:
        # .desktop 的字符串值不能有裸换行
        return s.replace("\r", " ").replace("\n", " ").strip()

    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={one_line(title)}",
        f"Comment={one_line('汉化版 · ' + title)}",
        f"Exec={_quote_exec(*exec_args)}",
        f"Path={workdir}",
    ]
    if icon_path is not None:
        lines.append(f"Icon={icon_path}")
    lines.extend([
        "Terminal=false",
        "Categories=Game;",
        "StartupNotify=true",
        # 让桌面环境把它和已有的游戏窗口归成一类
        f"StartupWMClass={one_line(title)}",
    ])
    return "\n".join(lines) + "\n"


def _ensure_exec(p: Path, log: Callable[[str], None]) -> None:
    """启动器得是可执行的，否则双击没反应。"""
    try:
        mode = p.stat().st_mode
        if not mode & stat.S_IXUSR:
            p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            log(f"  已给 {p.name} 加上执行权限")
    except Exception as e:
        log(f"  ! 加执行权限失败: {e}")


def _applications_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "applications"


def _desktop_dirs() -> list[Path]:
    """找桌面目录。中文系统是 ~/桌面，英文是 ~/Desktop。"""
    cands: list[Path] = []
    try:
        out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True,
                             text=True, timeout=5)
        d = out.stdout.strip()
        if d and d != str(Path.home()):
            cands.append(Path(d))
    except Exception:
        pass
    for name in ("桌面", "Desktop"):
        cands.append(Path.home() / name)
    seen: set[str] = set()
    out2: list[Path] = []
    for c in cands:
        if c.is_dir():
            k = str(c.resolve())
            if k not in seen:
                seen.add(k)
                out2.append(c)
    return out2


def create(root: Path, det=None, title: str = "",
           also_desktop: bool = True,
           log: Optional[Callable[[str], None]] = None) -> ShortcutResult:
    """给游戏建启动快捷方式。

    root  游戏根目录
    det   Detection 对象（拿 engine / launch 线索），可为 None
    title 显示名，空则自动取
    """
    log = log or (lambda *a: None)
    res = ShortcutResult()
    root = Path(root)
    if not root.is_dir():
        res.error = f"目录不存在: {root}"
        return res

    engine = getattr(det, "engine", "") if det is not None else ""
    title = (title or "").strip() or _game_title(root)
    log(f"[快捷方式] 目标 {title}")

    launch = _pick_launch(root, det)
    if launch is None:
        res.error = "没找到可执行的启动器"
        return res
    log(f"  启动器 {launch.name}")
    _ensure_exec(launch, log)
    res.launch = str(launch)

    slug = _slug(title, str(root))
    app_dir = _applications_dir()
    icon_dir = app_dir / "galgame-tl-icons" / slug
    icon_path, icon_src = prepare_icon(root, engine, title, icon_dir, log)
    res.icon_source = icon_src
    if icon_path:
        res.desktop_icon_path = str(icon_path)
        log(f"  图标 {icon_path.name}（{'游戏原图' if icon_src == 'original' else '生成'}）")

    # 启动命令：.sh 走 bash 更保险（有些游戏 .sh 没执行位或 shebang 怪）。
    # 这里必须传**参数列表**，让每个参数各自决定要不要加引号 ——
    # 把整串包成一个引号参数会让桌面环境找不到可执行文件。
    if launch.suffix.lower() == ".sh":
        exec_args = ("bash", str(launch))
    else:
        exec_args = (str(launch),)

    body = _desktop_entry(title, exec_args, launch.parent, icon_path)

    app_dir.mkdir(parents=True, exist_ok=True)
    dest = app_dir / f"{slug}.desktop"
    try:
        dest.write_text(body, encoding="utf-8")
        dest.chmod(0o755)
        res.desktop_path = str(dest)
        log(f"  写入 {dest}")
    except Exception as e:
        res.error = f"写 .desktop 失败: {e}"
        return res

    if also_desktop:
        for d in _desktop_dirs():
            try:
                tgt = d / f"{slug}.desktop"
                tgt.write_text(body, encoding="utf-8")
                tgt.chmod(0o755)
                # GNOME 需要 trusted 标记才显示图标；gio 不在就跳过
                try:
                    subprocess.run(
                        ["gio", "set", str(tgt), "metadata::trusted", "true"],
                        capture_output=True, timeout=5)
                except Exception:
                    pass
                log(f"  桌面图标 {tgt}")
            except Exception as e:
                log(f"  ! 桌面图标写入失败（{d}）: {e}")

    res.ok = True
    res.messages.append(res.summary())
    return res
