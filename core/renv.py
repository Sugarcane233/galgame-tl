"""Ren'Py 运行时环境检查与自修。

真机踩过的两个坑，都是"游戏能开但体验极差"而不报错：

1. libGLU.so.1 缺失 → 进不了 gl 渲染器，退回软件渲染，
   几十 G 素材的游戏直接卡死。老 Ubuntu/Debian 装 libglu1-mesa 就有了，
   但 24/26 默认不带，而游戏自带的 lib/ 里也没有。
2. Ren'Py 7.3.x 的 pygame_sdl2 不认 Wayland，必须 X11。从文件管理器
   双击时 XWayland 的 cookie 不一定传给子进程，报
   "Authorization required, but no authorization protocol specified"。
"""

from __future__ import annotations

import glob
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

# 本机 /snap 里 gnome 运行时带的 libGLU，可以作为无 sudo 时的来源
_GLU_SOURCES = (
    "/snap/gnome-46-2404/current/usr/lib/x86_64-linux-gnu/libGLU.so.1.3.1",
    "/snap/gnome-42-2204/current/usr/lib/x86_64-linux-gnu/libGLU.so.1.3.1",
    "/usr/lib/x86_64-linux-gnu/libGLU.so.1",
)


@dataclass
class RenderEnv:
    ok: bool
    libdir: str = ""          # 额外需要的 LD_LIBRARY_PATH 前缀
    display: str = ""
    xauthority: str = ""
    notes: list[str] = None

    def __post_init__(self):
        self.notes = self.notes or []

    def env(self) -> dict:
        """给 subprocess 用的环境变量补丁。"""
        e = {}
        if self.libdir:
            old = os.environ.get("LD_LIBRARY_PATH", "")
            e["LD_LIBRARY_PATH"] = f"{self.libdir}:{old}" if old else self.libdir
        if self.display:
            e["DISPLAY"] = self.display
            e["SDL_VIDEODRIVER"] = ""      # Ren'Py 7.x 用不了 wayland
            e.pop("SDL_VIDEODRIVER")
        if self.xauthority:
            e["XAUTHORITY"] = self.xauthority
        return e


def _has_glu() -> bool:
    for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib"):
        if glob.glob(f"{d}/libGLU.so*"):
            return True
    return False


def _arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def _game_libdir(root: Path) -> Path:
    """游戏自带原生库的目录（Savior.sh 会把它加进 LD_LIBRARY_PATH）。

    注意必须按本机架构选：一个游戏包里常常同时带 linux-x86_64 和
    linux-i686，选错了放进去等于没放（Ren'Py 只加载匹配的那个）。
    """
    want = f"linux-{_arch()}"
    exact = root / "lib" / want
    if exact.is_dir():
        return exact
    cands = sorted((root / "lib").glob("linux-*")) if (root / "lib").is_dir() else []
    for c in cands:
        if any(c.glob("*.so*")):
            return c
    return exact


def is_windows_build(root: Path) -> bool:
    """纯 Windows 发行包：lib/ 下只有 py3-windows-*，没有任何 linux-* 原生库。"""
    lib = root / "lib"
    if not lib.is_dir():
        # 没有 lib/ 但顶层有 .exe，也是 Windows 包（Ren'Py 7 的目录结构）
        return bool(list(root.glob("*.exe")))
    if list(lib.glob("py3-windows-*")) and not list(lib.glob("linux-*")):
        return True
    if list(root.glob("*.exe")) and not list(lib.glob("linux-*.so*")):
        return True
    return False


def ensure_libglu(root: Path, copy: bool = True) -> tuple[bool, str]:
    """保证游戏能找到 libGLU。返回 (好了没, 说明)。"""
    # Windows 版没有 Linux 原生目录，往里塞 .so 只会在游戏目录里留垃圾，
    # 而且 Windows 版本来也不靠 LD_LIBRARY_PATH 加载 GLU（走的是 Wine 的 d3d/gl）。
    if is_windows_build(root):
        return True, "Windows 版游戏，不需要补 libGLU（沙盒里翻译不启动它）"
    if _has_glu():
        return True, "系统已有 libGLU"
    libdir = _game_libdir(root)
    if glob.glob(str(libdir / "libGLU.so*")):
        return True, f"游戏目录已有 libGLU（{libdir}）"
    if not copy:
        return False, "系统缺 libGLU.so.1，且未开启自动补装"
    for src in _GLU_SOURCES:
        if Path(src).exists():
            try:
                libdir.mkdir(parents=True, exist_ok=True)
                dst = libdir / "libGLU.so.1"
                shutil.copyfile(src, dst)
                link = libdir / "libGLU.so"
                if not link.exists():
                    link.symlink_to("libGLU.so.1")
                return True, f"已从 {src} 补入 {libdir}"
            except Exception as e:
                return False, f"补 libGLU 失败: {e}"
    return False, "系统缺 libGLU.so.1，且没找到可借用的副本（装 libglu1-mesa）"


def _xauthority() -> str:
    for c in (os.environ.get("XAUTHORITY"),
              *sorted(glob.glob(f"/run/user/{os.getuid()}/.mutter-Xwaylandauth.*")),
              str(Path.home() / ".Xauthority")):
        if c and Path(c).is_file() and os.access(c, os.R_OK):
            return c
    return ""


def prepare(root: Path, fix_libglu: bool = True, want_run: bool = True) -> RenderEnv:
    """准备运行环境。

    want_run=False 表示这次只翻译、不启动游戏（Windows 版走这条路：
    在 Linux 上翻译不需要能跑，跑起来要靠 Wine 或回 Windows 玩）。
    """
    ok, note = ensure_libglu(root, copy=fix_libglu)
    env = RenderEnv(ok=ok)
    env.notes.append(note)
    if not want_run:
        return env
    if note.startswith("已从"):
        env.libdir = str(_game_libdir(root))

    # Ren'Py 7.x 不支持 wayland：有 X11 就走 X11
    if glob.glob("/tmp/.X11-unix/X*"):
        env.display = os.environ.get("DISPLAY") or ":0"
        env.xauthority = _xauthority()
        if not env.xauthority:
            env.notes.append("找不到 X11 cookie，Wayland 下可能报 Authorization required")
    else:
        env.notes.append("没有 X11 显示，Ren'Py 7.x 可能无法创建窗口")
    return env


def read_log(root: Path) -> str:
    p = root / "log.txt"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def diagnose(root: Path) -> list[str]:
    """跑之前先读上次的 log.txt，把已知症状翻成人话。"""
    log = read_log(root)
    out = []
    if "libGLU.so.1: cannot open shared object file" in log:
        out.append("上次启动缺 libGLU.so.1 → 退回软件渲染，会极卡")
    if "Unknown renderer: gl" in log:
        out.append("上次没能用上 OpenGL 渲染")
    if "wayland not available" in log:
        out.append("上次走 Wayland 失败（Ren'Py 7.x 只支持 X11）")
    if "Authorization required, but no authorization protocol specified" in log:
        out.append("上次 X11 授权被拒（缺 XAUTHORITY）")
    m = [l for l in log.splitlines() if l.startswith("Renderer:")]
    if m:
        out.append("上次渲染器: " + m[0].split(":", 1)[1].strip())
    return out
