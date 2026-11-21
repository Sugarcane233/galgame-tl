"""引擎探测：把一个游戏目录/启动文件判定成 renpy / rpgmaker / unity / unknown

判定优先级按「特征强度」排，不按目录里文件多少：
  1. Ren'Py      —— renpy/ 目录、*.rpa、game/ 下有 .rpyc、lib/py3-linux-x86_64
  2. RPG Maker   —— data/System.json + js/rmmz_core.js(MZ) 或 www/js/rpg_core.js(MV)
  3. Unity       —— *_Data/Managed/Assembly-CSharp.dll、UnityPlayer.so、globalgamemanagers
  4. 其他引擎    —— 只做提示，返回 unknown 但带上线索

输入可以是：
  * 游戏根目录（含 game/ 或 www/ 的）
  * 启动脚本（.sh / .AppImage / 无后缀可执行）
  * .exe（wine 跑的 Windows 版，同样按目录判断）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Detection:
    engine: str                      # renpy | rpgmaker | unity | unknown
    root: Path                       # 真正要操作的根目录
    launch: Optional[Path] = None     # 启动入口（探测到才填）
    variant: str = ""                # MZ / MV / Mono / IL2CPP …
    data_dir: Optional[Path] = None   # RPG Maker 的 data 目录
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.engine != "unknown"

    def summary(self) -> str:
        s = f"{self.engine}"
        if self.variant:
            s += f" ({self.variant})"
        return s


# ------------------------------------------------------------------ 入口归一
def normalize_input(p: str | os.PathLike) -> Path:
    """把用户拖进来的东西归一成「目录」：目录直接用，文件取父目录（智能上跳）"""
    path = Path(p).expanduser()
    if path.is_dir():
        return _smart_root(path)
    if path.is_file():
        # AppImage / 可执行脚本 / .exe 都在游戏根目录里
        return _smart_root(path.parent)
    raise FileNotFoundError(f"路径不存在: {p}")


def _smart_root(d: Path) -> Path:
    """从拖进来的目录往上/往下找真正的游戏根。
    常见情况：用户拖的是 `GameDir/game` 或 `GameDir/www`，要回到 GameDir。
    """
    d = d.resolve()
    # 如果是 game/ 或 www/ 或 xxx_Data 的子目录，往上退
    for _ in range(3):
        name = d.name.lower()
        if name in ("game", "www", "data", "tl"):
            d = d.parent
            continue
        if name.endswith("_data"):
            d = d.parent
            continue
        break
    return d


# ------------------------------------------------------------------ 单项探测
def _has(d: Path, *rels: str) -> Optional[Path]:
    for r in rels:
        p = d / r
        if p.exists():
            return p
    return None


def _glob_any(d: Path, pattern: str, limit: int = 1) -> list[Path]:
    out = []
    try:
        for p in d.rglob(pattern):
            out.append(p)
            if len(out) >= limit:
                break
    except (PermissionError, OSError):
        pass
    return out


def _detect_renpy(root: Path, det: Detection) -> bool:
    game = root / "game"
    hits = []
    if (root / "renpy").is_dir():
        hits.append("renpy/ 目录")
    if (root / "lib").is_dir() and any(
        (root / "lib").glob("py3-linux-x86_64*")
    ):
        hits.append("lib/py3-linux-x86_64*")
    if game.is_dir():
        if list(game.glob("*.rpa")):
            hits.append(f"{len(list(game.glob('*.rpa')))} 个 .rpa")
        rpyc = list(game.glob("*.rpyc"))
        if rpyc:
            hits.append(f"{len(rpyc)} 个 .rpyc")
        if (game / "script.rpy").exists() or list(game.glob("*.rpy")):
            hits.append("明文 .rpy 脚本")
        if (game / "tl").is_dir():
            hits.append("已有 tl/ 翻译目录")
    # 顶层也可能直接是 game 内容（打包异常的构建）
    if not hits and list(root.glob("*.rpa")):
        hits.append(f"{len(list(root.glob('*.rpa')))} 个 .rpa（顶层）")

    # 判定：任一强特征即可。明文 .rpy 也算 —— 解包后的项目就是纯 .rpy，
    # 不能因为「没有 rpa/rpyc」就否掉（这是最常见的中间状态）。
    strong = (
        (root / "renpy").is_dir()
        or (game / "tl").is_dir()
        or bool(list(game.glob("*.rpa")))
        or bool(list(game.glob("*.rpyc")))
        or bool(list(game.glob("*.rpy")))
        or bool(list(root.glob("*.rpa")))
    )
    if hits and strong:
        det.engine = "renpy"
        det.evidence.extend(hits)
        for cand in ("renpy.sh", "launch.sh", "start.sh"):
            if (root / cand).exists():
                det.launch = root / cand
                break
        if det.launch is None:
            for p in root.glob("*.sh"):
                det.launch = p
                break
        return True
    return False


def _detect_vxace(root: Path, det: Detection) -> bool:
    """RPG Maker VX Ace / VX / XP（RGSS 系列）。

    和 MV/MZ 的根本区别：文本在 `Data/*.rvdata2`（Ruby Marshal），
    资源常打包在 `Game.rgss3a` —— 没有 System.json。
    """
    # 打包在归档里
    arc = None
    for pat in ("*.rgss3a", "*.rgss2a", "*.rgssad"):
        for p in root.glob(pat):
            arc = p
            break
        if arc:
            break

    # 已解包：Data/*.rvdata2
    ddir = None
    for c in (root / "Data", root / "data", root / "www" / "Data"):
        if (c / "Scripts.rvdata2").exists() or (c / "System.rvdata2").exists():
            ddir = c
            break

    ini = None
    for c in (root / "Game.ini", root / "game.ini"):
        if c.exists():
            ini = c
            break

    if not (arc or ddir or ini):
        return False

    det.engine = "rpgmaker"
    det.root = root

    if ddir:
        det.data_dir = ddir
        det.evidence.append(f"Data/*.rvdata2 @ {ddir.relative_to(root) if ddir != root else '.'}")
    if arc:
        det.evidence.append(f"归档 {arc.name}（{arc.stat().st_size:,} 字节）")
        det.variant = {".rgss3a": "VX Ace", ".rgss2a": "VX", ".rgssad": "XP"}.get(
            arc.suffix.lower(), "RGSS")
    if ini:
        det.evidence.append("Game.ini")
        try:
            txt = ini.read_text(encoding="utf-8", errors="ignore")
            for line in txt.splitlines():
                if line.lower().startswith("library="):
                    lib = line.split("=", 1)[1].strip()
                    det.evidence.append(f"Library={lib}")
                    if "301" in lib:
                        det.variant = det.variant or "VX Ace"
                    elif "202" in lib:
                        det.variant = det.variant or "VX"
                    elif "104" in lib:
                        det.variant = det.variant or "XP"
                elif line.lower().startswith("title="):
                    det.evidence.append(f"标题: {line.split('=',1)[1].strip()}")
        except OSError:
            pass

    # 运行时 dll 也能确认版本
    for p in root.glob("System/RGSS*.dll"):
        det.evidence.append(p.name)
        if "301" in p.name:
            det.variant = "VX Ace"
        elif "300" in p.name:
            det.variant = det.variant or "VX"
        elif "202" in p.name:
            det.variant = det.variant or "VX"
        elif "104" in p.name:
            det.variant = det.variant or "XP"
        break

    det.variant = det.variant or "RGSS"

    for cand in ("Game.exe", "game.exe", "Game.sh"):
        if (root / cand).exists():
            det.launch = root / cand
            break

    if not ddir and not arc:
        det.warnings.append("只看到 Game.ini，没找到 Data/*.rvdata2 或 .rgss3a")
    return True


def _detect_rpgmaker(root: Path, det: Detection) -> bool:
    # MV: root/www/data ; MZ: root/data
    cands = []
    for d in (root / "data", root / "www" / "data", root / "js", root / "www" / "js"):
        if d.is_dir():
            cands.append(d)
    sysjson = None
    for d in (root / "data", root / "www" / "data"):
        p = d / "System.json"
        if p.exists():
            sysjson = p
            break
    if sysjson is None:
        found = _glob_any(root, "System.json", limit=3)
        for p in found:
            if p.parent.name == "data":
                sysjson = p
                break
    if sysjson is None:
        return False

    data_dir = sysjson.parent
    det.data_dir = data_dir
    det.root = root
    det.engine = "rpgmaker"
    det.evidence.append(f"System.json @ {data_dir.relative_to(root)}")

    # MV / MZ 区分
    mz_core = list(root.glob("js/rmmz_core.js")) or list(root.glob("www/js/rmmz_core.js"))
    mv_core = list(root.glob("js/rpg_core.js")) or list(root.glob("www/js/rpg_core.js"))
    if mz_core:
        det.variant = "MZ"
        det.evidence.append("js/rmmz_core.js")
    elif mv_core:
        det.variant = "MV"
        det.evidence.append("js/rpg_core.js")
    else:
        det.variant = "MV/MZ"

    # 加密 key
    try:
        sys_data = json.loads(sysjson.read_text(encoding="utf-8"))
        key = sys_data.get("encryptionKey") or ""
        if key:
            det.evidence.append("加密资源（有 encryptionKey）")
            enc = len(_glob_any(root, "*.rpgmvp", limit=1))
            if enc:
                det.evidence.append("发现 .rpgmvp 加密图")
        title = sys_data.get("gameTitle")
        if title:
            det.evidence.append(f"标题: {title}")
    except Exception as e:
        det.warnings.append(f"读 System.json 失败: {e}")

    # 启动入口
    if det.variant == "MZ":
        for cand in (root / "nw", root / "Game.exe", root / "nw.exe"):
            if cand.exists():
                det.launch = cand
                break
    for cand in sorted(root.glob("*.sh")) + sorted(root.glob("*.AppImage")):
        det.launch = det.launch or cand
    for cand in sorted(root.glob("*.exe")):
        det.launch = det.launch or cand
        break
    return True


def _detect_unity(root: Path, det: Detection) -> bool:
    data_dirs = [p for p in root.iterdir() if p.is_dir() and p.name.endswith("_Data")] \
        if root.is_dir() else []
    hits = []
    variant = ""
    if data_dirs:
        dd = data_dirs[0]
        hits.append(f"{dd.name}/")
        if (dd / "Managed" / "Assembly-CSharp.dll").exists():
            hits.append("Managed/Assembly-CSharp.dll")
            variant = "Mono"
        elif (dd / "il2cpp_data").is_dir() or (dd / "Managed" / "Metadata").is_dir():
            hits.append("il2cpp_data/")
            variant = "IL2CPP"
        if (dd / "resources.assets").exists():
            hits.append("resources.assets")
        if list(dd.glob("*.assets")):
            hits.append(f"{len(list(dd.glob('*.assets')))} 个 .assets")
        if (dd / "globalgamemanagers").exists():
            hits.append("globalgamemanagers")
    if (root / "UnityPlayer.so").exists():
        hits.append("UnityPlayer.so")
    # 有些 Linux 版把 assets 直接放根目录
    if not hits and list(root.glob("*.assets")):
        hits.append("根目录 .assets")

    if hits:
        det.engine = "unity"
        det.variant = variant or "Unity"
        det.evidence.extend(hits)
        for cand in sorted(root.glob("*.x86_64")) + sorted(root.glob("*.sh")) + \
                sorted(root.glob("*.AppImage")):
            det.launch = cand
            break
        # IL2CPP 提醒
        if variant == "IL2CPP":
            det.warnings.append(
                "IL2CPP 游戏静态改文本基本不可行，建议用 BepInEx + XUnity 运行时路线"
            )
        return True
    return False


# 其他引擎的线索表（只提示，不处理）
OTHER_ENGINES = [
    (("*.xp3",), "KiriKiri (XP3)", "kiriKiri2 / xp3 解包工具"),
    (("*.arc",), "BGI / Ethornell", "arc_unpacker"),
    (("*.int",), "CatSystem2", "GARbro / IntParse"),
    (("*.pfs",), "Artemis", "GARbro"),
    (("*.pac",), "RealLive", "rlvm / GARbro"),
    (("nscript.dat",), "NScripter", "nsdec"),
    (("*.wolf", "Data.wolf"), "Wolf RPG", "WolfDec / GARbro"),
    (("*.ks", "*.tjs"), "吉里吉里 (KAG)", "KAG 脚本明文，可直接改"),
]


def detect(path: str | os.PathLike) -> Detection:
    root = normalize_input(path)
    det = Detection(engine="unknown", root=root)

    if _detect_renpy(root, det):
        return det
    if _detect_vxace(root, det):
        return det
    if _detect_rpgmaker(root, det):
        return det
    if _detect_unity(root, det):
        return det

    # 都不匹配：给线索
    for pats, name, tool in OTHER_ENGINES:
        for pat in pats:
            if list(root.glob(pat)) or list(root.rglob(pat))[:1]:
                det.evidence.append(f"疑似 {name} → 可用 {tool}")
                det.variant = name
                break
    if not det.evidence:
        try:
            sample = [p.name for p in list(root.iterdir())[:12]]
            det.evidence.append("目录内容: " + ", ".join(sample))
        except OSError:
            pass
    return det


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        d = detect(arg)
        print(f"== {arg}")
        print(f"   引擎: {d.summary()}")
        print(f"   根目录: {d.root}")
        if d.launch:
            print(f"   启动: {d.launch}")
        if d.data_dir:
            print(f"   数据: {d.data_dir}")
        for e in d.evidence:
            print(f"   · {e}")
        for w in d.warnings:
            print(f"   ! {w}")
