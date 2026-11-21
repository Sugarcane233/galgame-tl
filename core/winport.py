"""把 Windows 版 Ren'Py 游戏改造成 Linux 原生运行 —— 不需要 Wine。

原理：Ren'Py 的目录分成两半。

    GameDir/
      renpy/          ← 纯 Python（几百个 .py），一行原生代码都没有，跨平台完全一样
      game/           ← 脚本和素材，跨平台完全一样
      lib/python3.12/ ← 标准库 .pyc，跨平台完全一样
      lib/py3-windows-x86_64/  ← 只有这里和平台有关：解释器 + librenpython.so + SDL 的 dll
      lib/py3-linux-x86_64/    ← 换成这一套就能在 Linux 上原生跑

所以 Windows 版只要补上 `lib/py3-linux-x86_64/` + 一个 `.sh` 启动器，
就能直接在 Linux 上跑，帧率和原生版一样（不是 Wine 的转译层）。

引擎从哪儿借？按这个顺序找最省事：
  1. 本机其它 Ren'Py 游戏自带的 Linux 引擎（同版本最好，8.x 之间基本通用）
  2. apt 装的 renpy
  3. 用户指定的目录
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# 找引擎时扫这些地方（只扫一层深，避免翻遍整块盘）
_SEARCH_ROOTS = (
    "~/下载",
    "~/Downloads",
    "~/游戏",
    "~/Games",
    "~/.local/share",
    "~/桌面",
    "~/Desktop",
)

_STD_SOURCES = (
    "/usr/lib/renpy",
    "/usr/share/games/renpy",
    "/usr/lib/games/renpy",
    "/opt/renpy",
)


def _arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def linux_lib_name() -> str:
    return f"py3-linux-{_arch()}"


def engine_of_dirs(root: Path) -> Path | None:
    """这个包里现成的 Linux 引擎目录（没有就 None）。"""
    d = root / "lib" / linux_lib_name()
    return d if (d / "librenpython.so").is_file() else None


def is_windows_build(root: Path) -> bool:
    """判断是不是纯 Windows 包（没有 Linux 引擎、有 Windows 引擎或 .exe）。"""
    if engine_of_dirs(root):
        return False
    lib = root / "lib"
    has_win = lib.is_dir() and any(lib.glob("py3-windows-*"))
    has_exe = any(root.glob("*.exe"))
    return has_win or has_exe


def read_version(root: Path) -> str:
    """读游戏自带的 Ren'Py 版本号，读不到返回空串。"""
    p = root / "renpy" / "vc_version.py"
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("version"):
                # version = '8.5.2.26010301'
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def _ver_key(v: str) -> tuple[int, ...]:
    """'8.5.2.26010301' → (8, 5, 2)。用于比兼容性。"""
    out = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            break
    return tuple(out) or (0,)


def _engine_ok(cand: Path) -> bool:
    return (cand / "librenpython.so").is_file() and (cand / "python").exists()


def _candidate_roots(extra: list[Path] | None = None):
    """列出所有可能带 Linux 引擎的游戏根目录。"""
    seen: set[Path] = set()
    roots: list[Path] = []
    for raw in _SEARCH_ROOTS:
        base = Path(os.path.expanduser(raw))
        if not base.is_dir():
            continue
        try:
            for sub in base.iterdir():
                if not sub.is_dir() or sub in seen:
                    continue
                seen.add(sub)
                roots.append(sub)
        except (PermissionError, OSError):
            continue
    for e in extra or []:
        e = Path(e).expanduser()
        if e.is_dir() and e not in seen:
            seen.add(e)
            roots.append(e)
    return roots


@dataclass
class PortResult:
    ok: bool
    root: Path
    engine_from: str = ""        # 引擎来源说明
    version_game: str = ""
    version_engine: str = ""
    launcher: Path | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return "移植失败：" + "；".join(self.notes)
        return (f"已改造成 Linux 原生（引擎来自 {self.engine_from}，"
                f"游戏 {self.version_game} / 引擎 {self.version_engine}）")


def find_linux_engine(root: Path, extra: list[Path] | None = None) -> tuple[Path | None, str]:
    """找一个可借的 `py3-linux-*` 目录。返回 (目录, 来源说明)。"""
    own = engine_of_dirs(root)
    if own:
        return own, "游戏自带"

    game_ver = read_version(root)
    gk = _ver_key(game_ver)

    # 1) 其它游戏自带的引擎，优先同大版本、版本最接近的
    scored: list[tuple[tuple[int, int], Path, str]] = []
    for cand_root in _candidate_roots(extra):
        try:
            eng = cand_root / "lib" / linux_lib_name()
            if not _engine_ok(eng):
                continue
        except OSError:
            continue
        ver = read_version(cand_root)
        k = _ver_key(ver)
        same_major = 1 if (k and gk and k[0] == gk[0]) else 0
        # 同 major 优先，其次版本号越接近越好（用负距离当排序键的倒数项）
        dist = abs((k[1] if len(k) > 1 else 0) - (gk[1] if len(gk) > 1 else 0))
        scored.append(((same_major, -dist), eng, f"{cand_root.name}（Ren'Py {ver or '?'}）"))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0]
        if best[0][0] == 0 and game_ver:
            # 没有同 major 的，仍然可以用，但提醒
            return best[1], best[2] + "，版本不同"
        return best[1], best[2]

    # 2) 系统装的 renpy
    for s in _STD_SOURCES:
        base = Path(s)
        for eng in (base / "lib" / linux_lib_name(), base / linux_lib_name()):
            if _engine_ok(eng):
                return eng, f"系统 renpy（{base}）"

    return None, ""


def _write_launcher(root: Path, base: str) -> Path:
    """按 Ren'Py 官方模板生成 .sh 启动器。"""
    sh = root / f"{base}.sh"
    sh.write_text('''#!/bin/sh
# 由 galgame-tl winport 生成：把 Windows 版当 Linux 版跑
PYTHON="py3"
SCRIPT="$0"
while [ -L "$SCRIPT" ] ; do
    LINK=$(readlink "$SCRIPT")
    case "$LINK" in
        /*) SCRIPT="$LINK" ;;
        *)  SCRIPT="$(dirname "$SCRIPT")/$LINK" ;;
    esac
done
ROOT=$(dirname "$SCRIPT")
ROOT=$(cd "$ROOT"; pwd)
BASEFILE=$(basename "$SCRIPT" .sh)

if [ -z "$RENPY_PLATFORM" ] ; then
    RENPY_PLATFORM="$(uname -s)-$(uname -m)"
    case "$RENPY_PLATFORM" in
        Darwin-*|mac-*) RENPY_PLATFORM="mac-universal" ;;
        *-x86_64|amd64) RENPY_PLATFORM="linux-x86_64" ;;
        *-i*86)         RENPY_PLATFORM="linux-i686" ;;
        Linux-*)        RENPY_PLATFORM="linux-$(uname -m)" ;;
    esac
fi

LIB="$ROOT/lib/$PYTHON-$RENPY_PLATFORM"
if ! test -d "$LIB"; then
    echo "找不到 $LIB"
    echo "重新跑一次 galgame-tl 的「移植到 Linux」即可。"
    exit 1
fi

if [ -e "$LIB/$BASEFILE" ] ; then
    exec $RENPY_GDB "$LIB/$BASEFILE" "$@"
fi
if [ -e "$LIB/python" ] ; then
    exec $RENPY_GDB "$LIB/python" "$@"
fi
echo "$LIB 里没有可执行文件。"
exit 1
''', encoding="utf-8")
    sh.chmod(0o755)
    return sh


def port(root: Path, engine_src: Path | None = None,
         extra_search: list[Path] | None = None,
         dry_run: bool = False) -> PortResult:
    """把 Windows 版 Ren'Py 游戏改造成 Linux 原生可跑。

    - 只补 lib/py3-linux-* 和 .sh/.py，不动 game/ 和 renpy/
    - 幂等：已经有 Linux 引擎就直接返回成功
    """
    root = Path(root).resolve()
    res = PortResult(ok=False, root=root)
    res.version_game = read_version(root)

    if engine_of_dirs(root):
        res.ok = True
        res.engine_from = "游戏自带（已经是 Linux 版）"
        res.launcher = next((p for p in root.glob("*.sh")), None)
        return res

    if not (root / "renpy").is_dir() or not (root / "game").is_dir():
        res.notes.append("不是 Ren'Py 游戏目录（缺 renpy/ 或 game/）")
        return res

    src, why = (engine_src, "手动指定") if engine_src else find_linux_engine(root, extra_search)
    if not src or not _engine_ok(Path(src)):
        res.notes.append(
            "本机找不到可借的 Ren'Py Linux 引擎。"
            "装一个：sudo apt install renpy（8.3，可用），"
            "或把任意 Linux 版 Ren'Py 游戏的目录加进来。")
        return res

    src = Path(src).resolve()
    res.engine_from = why
    # 引擎版本：源目录往上两级如果能找到 renpy/vc_version.py 就读
    src_ver = ""
    for up in (src.parent.parent, src, src.parent):
        if (up / "renpy" / "vc_version.py").is_file():
            src_ver = read_version(up)
            break
    res.version_engine = src_ver or "?"

    if res.version_game and src_ver:
        g, e = _ver_key(res.version_game), _ver_key(src_ver)
        if g[0] != e[0]:
            res.notes.append(
                f"⚠ 引擎是 Ren'Py {src_ver}，游戏是 {res.version_game}，"
                "大版本不同可能跑不起来（先备份）")

    dst = root / "lib" / linux_lib_name()
    if dry_run:
        res.ok = True
        res.notes.append(f"要复制 {src} → {dst}")
        return res

    try:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True)
        for f in dst.iterdir():
            if f.is_file():
                f.chmod(f.stat().st_mode | 0o111)
    except OSError as e:
        res.notes.append(f"复制引擎失败: {e}")
        return res

    # 启动器：文件名跟 .exe 同名，方便用户认
    exe = next(iter(sorted(root.glob("*.exe"))), None)
    base = exe.stem if exe else root.name
    res.launcher = _write_launcher(root, base)

    # .py 入口（Ren'Py 的 bootstrap，纯 Python，跨平台）
    py = root / f"{base}.py"
    if not py.exists():
        donor_py = next((p for p in (src.parent.parent).glob("*.py")
                         if p.stem.lower() in ("renpy", base.lower())), None)
        if donor_py:
            shutil.copyfile(donor_py, py)
        else:
            py.write_text(_MIN_PY, encoding="utf-8")

    res.ok = True
    return res


# 极端情况下的兜底入口（游戏连 .py 都没带）
_MIN_PY = '''import os, sys, warnings
def path_to_gamedir(basedir, name):
    for c in [name, "game"]:
        d = os.path.join(basedir, c)
        if os.path.isdir(d):
            return d
    return basedir
def path_to_common(renpy_base):
    p = renpy_base + "/renpy/common"
    return p if os.path.isdir(p) else None
def path_to_saves(gamedir, save_directory=None):
    import renpy
    if save_directory is None:
        save_directory = renpy.config.save_directory
    if not save_directory:
        return os.path.join(gamedir, "saves")
    return os.path.expanduser("~/.renpy/" + save_directory)
def path_to_renpy_base():
    return os.path.abspath(os.path.dirname(os.path.abspath(__file__)))
def path_to_logdir(basedir):
    return basedir
def predefined_searchpath(commondir):
    sp = []
    if commondir and os.path.isdir(commondir):
        sp.append(commondir)
    return sp
android = False
def main():
    base = path_to_renpy_base()
    sys.path.append(base)
    warnings.simplefilter("ignore", DeprecationWarning)
    import renpy.bootstrap
    renpy.__main__ = sys.modules[__name__]
    renpy.bootstrap.bootstrap(base)
if __name__ == "__main__":
    main()
'''


# ------------------------------------------------------------------ 其它引擎
def rpgmaker_advice(root: Path) -> list[str]:
    """RPG Maker MV/MZ 的 Windows 包怎么在 Linux 上跑（只给建议，不动手）。"""
    www = root / "www"
    if not www.is_dir():
        www = root
    has_js = (www / "js").is_dir() or (www / "data").is_dir()
    if not has_js:
        return ["没看到 www/js 或 www/data，不像 MV/MZ 的包"]
    mz = (www / "js" / "rmmz_core.js").exists()
    ver = "MZ" if mz else "MV"
    return [
        f"这是 RPG Maker {ver} 的包。汉化侧不受影响：www/data 里的 .json 直接翻译，"
        "工具已经支持。",
        f"游玩侧：{ver} 的游戏本体就是 www/ 里的 JS，跟平台无关。",
        "  最简单的办法：装一个 Linux 版 RPG Maker MV/MZ 试玩版（Steam 有），"
        "把 www/ 整个换进去。",
        "  或者用 nw.js 的 Linux 版：官网下 nwjs-normal-linux-x64，"
        "解包后 `./nw /path/to/GameDir/www` 就能跑。",
        "  MV 用的是 nw.js 0.29 附近，MZ 用 0.48+；版本差太多会白屏，"
        "那就换对应的 nw 版本。",
    ]


def unity_advice(root: Path) -> list[str]:
    """Unity 的 Windows 包：没有跨平台捷径。"""
    data = next((p for p in root.iterdir() if p.name.endswith("_Data") and p.is_dir()), None)
    if not data:
        return ["没看到 xxx_Data 目录，不像 Unity 的包"]
    exe = next((p for p in root.glob("*.exe")), None)
    return [
        "这是 Unity 的 Windows 包。汉化侧不受影响：Assembly-CSharp.dll 里的字符串"
        "可以直接抽出来翻（工具已支持）。",
        "游玩侧没有 Ren'Py 那样的捷径——UnityPlayer.dll 是平台二进制，"
        "Data/ 里的 .assets 也跟引擎版本绑定，换 Linux 引擎会读不了。所以只有两条路：",
        "  1) 用 Wine / Proton 跑 .exe（推荐，见 README「Windows 游戏」）；",
        "  2) 如果作者发过 Linux 版，直接用 Linux 版 + 复制翻译好的 .assets。",
        f"Wine 试运行：`wine {exe.name if exe else 'Game.exe'}`（工作目录必须是游戏根目录）",
    ]
