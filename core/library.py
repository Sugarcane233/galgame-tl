"""游戏库 —— 记住「汉化过 / 打算汉化」的游戏，方便反复回来操作。

为什么需要它
------------
汉化一个游戏不是一次性的：翻到一半想换模型、跑完想看看有没有漏、
改完术语表要重跑。每次都靠文件管理器翻目录太麻烦，而且还容易忘掉
某个游戏到底装在哪、快捷方式建没建、缓存还剩多少。

接口分两半
----------
* 记录层：`load() / save() / add() / remove()`，落在
  `~/.local/share/galgame-tl/library.json`，只是「我知道有这么个游戏」。
* 探查层：`probe()` 现场去磁盘看状态（引擎能不能识别、缓存多少条、
  快捷方式在不在）。**不缓存状态**——用户可能在文件管理器里删了东西，
  显示过期的「已汉化」比不显示更糟。

删除是最危险的一步，所以拆成「先出计划、再执行」
（`plan_delete()` → `apply_delete()`），确认对话框拿计划去列清单，
用户看到的每一条路径就是真正会被删的路径。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .detect import Detection, detect, normalize_input

# 缓存目录名，必须和 pipeline._out_dir() 保持一致
CACHE_DIRNAME = "_galgame_tl"

# 扫描游戏时最多下钻几层。玩家目录常见的布局：
#   ~/下载/<游戏>/
#   ~/下载/<打包组>/<游戏>/
#   ~/Games/<平台>/<游戏>/
# 3 层能覆盖，再深就是压缩包内部结构了，扫进来也是噪音。
SCAN_DEPTH = 3

# 扫描上限，防止误扫到 500G 的素材目录里卡死
SCAN_LIMIT = 200


# ------------------------------------------------------------------ 存储位置

def data_dir() -> Path:
    """XDG 数据目录下的 galgame-tl/。"""
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "galgame-tl"


def library_path() -> Path:
    return data_dir() / "library.json"


# ------------------------------------------------------------------ 条目

@dataclass
class GameEntry:
    """游戏库里的一条记录。

    只存「用户告诉我的事实」，不存状态。`path` 是 realpath，避免同一个
    游戏因为软链接被记两遍。
    """
    path: str
    title: str = ""
    engine: str = ""
    variant: str = ""
    added_at: float = field(default_factory=time.time)
    note: str = ""

    @property
    def root(self) -> Path:
        return Path(self.path)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameEntry":
        return cls(
            path=str(d.get("path", "")),
            title=str(d.get("title", "")),
            engine=str(d.get("engine", "")),
            variant=str(d.get("variant", "")),
            added_at=float(d.get("added_at") or time.time()),
            note=str(d.get("note", "")),
        )


@dataclass
class GameStatus:
    """一条记录在磁盘上的真实状态（`probe()` 的产物，不落盘）。"""
    exists: bool = False
    engine: str = ""
    variant: str = ""
    launch: str = ""
    size_bytes: int = 0
    cache_dir: str = ""
    cache_entries: int = 0
    has_snapshot: bool = False
    shortcut_path: str = ""
    runtime_ready: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.exists:
            return "目录已不存在"
        bits = [self.engine or "未知引擎"]
        if self.variant:
            bits[-1] += f" ({self.variant})"
        if self.cache_entries:
            bits.append(f"已译 {self.cache_entries} 句")
        else:
            bits.append("未翻译")
        if self.runtime_ready:
            bits.append("运行时汉化已装")
        if self.shortcut_path:
            bits.append("已有快捷方式")
        return " · ".join(bits)


# ------------------------------------------------------------------ 读写

def load() -> list[GameEntry]:
    p = library_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        # 文件损坏不能直接崩：坏文件留个备份，当作空库继续用
        try:
            p.rename(p.with_suffix(".json.broken"))
        except Exception:
            pass
        return []
    items = raw.get("games") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[GameEntry] = []
    for d in items:
        if isinstance(d, dict) and d.get("path"):
            out.append(GameEntry.from_dict(d))
    return out


def save(entries: Iterable[GameEntry]) -> None:
    p = library_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "games": [e.to_dict() for e in entries]}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)          # 原子替换，写一半断电不会留下半个 JSON


def _key(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path).rstrip("/")


def _related(a: Path, b: Path) -> bool:
    """两个路径是不是指向同一个游戏（互相包含，或识别出的 root 相同）。

    为什么需要：一个游戏常有两层目录 —— 外层是发行包
    （`Star_Knightess_Aura/`），里层才是引擎根（`star-knightess-aura-linux/`）。
    用户可能先拖外层、后来又拖里层，库里就出现两行同一个游戏；更麻烦的是
    删掉其中一个后，另一个还留着但游戏已经不在了。

    判据用「一个是另一个的祖先」，比路径字符串前缀更准（不会把
    `Game` 和 `Game2` 判成相关）。
    """
    try:
        ra, rb = a.resolve(), b.resolve()
    except Exception:
        ra, rb = a, b
    if ra == rb:
        return True
    for x, y in ((ra, rb), (rb, ra)):
        try:
            x.relative_to(y)
            return True
        except ValueError:
            continue
    # 路径不同但识别到同一个引擎根，也是同一个游戏
    try:
        return detect(ra).root.resolve() == detect(rb).root.resolve()
    except Exception:
        return False


def add(entries: list[GameEntry], path: str | os.PathLike,
        title: str = "", note: str = "") -> tuple[list[GameEntry], GameEntry]:
    """加一条记录（同一个游戏的另一层目录会合并，不重复记）。

    返回 (新列表, 那条记录)。
    """
    root = normalize_input(path)
    for e in entries:
        if _related(e.root, root):
            if title:
                e.title = title
            if note:
                e.note = note
            return entries, e
    e = GameEntry(path=_key(root), title=(title or root.name), note=note)
    try:
        det = detect(root)
        if det.ok:
            e.engine = det.engine
            e.variant = det.variant
            # 记「引擎真正所在的那一层」，这样打开目录 / 删缓存都指对地方
            e.path = _key(det.root)
    except Exception:
        pass
    entries.append(e)
    return entries, e


def remove(entries: list[GameEntry], path: str | os.PathLike) -> list[GameEntry]:
    """只删记录，不动磁盘上的任何文件。

    会把指向同一个游戏的「另一层目录」记录一起摘掉 —— 否则删完游戏，
    库里还留着一行点不开的空壳。
    """
    p = Path(path)
    return [e for e in entries if not _related(e.root, p)]


# ------------------------------------------------------------------ 探查

def cache_dirs(root: Path) -> list[Path]:
    """这个游戏**可能**有几个汉化缓存目录，按存在性过滤后返回。

    为什么不是一个固定路径：pipeline 的 `_out_dir()` 用的是
    `det.root.parent / "_galgame_tl"`，而「det.root」取决于用户当初拖
    进去的是哪一层 —— 有人拖的是外层包目录（`Star_Knightess_Aura/`），
    有人拖的是里面真正的游戏目录（`star-knightess-aura-linux/`），
    于是缓存会落在外层或内层，两种都可能。实测本机就同时存在这两种形态。

    所以按「有可能的几处」全查一遍，谁在磁盘上就算谁。删的时候也全删，
    否则清掉一个还剩一个，用户以为删干净了其实没有。
    """
    out: list[Path] = []
    seen: set[str] = set()

    def push(p: Path):
        try:
            k = str(p.resolve())
        except Exception:
            k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)

    if root.name == CACHE_DIRNAME:
        push(root)
    # 外层：游戏目录的兄弟
    push(root.parent / CACHE_DIRNAME)
    # 内层：游戏目录自己下面
    push(root / CACHE_DIRNAME)
    return [p for p in out if p.is_dir()]


def cache_dir(root: Path) -> Path:
    """首选缓存目录（给「点一下打开缓存」这类单目标场景用）。"""
    found = cache_dirs(root)
    if found:
        return found[0]
    if root.parent.name == CACHE_DIRNAME:
        return root.parent
    return root.parent / CACHE_DIRNAME


def cache_files(root: Path) -> list[Path]:
    """所有 `*.cache.json`（可能横跨多个根名，比如外层目录名与内层不同）。"""
    out: list[Path] = []
    for d in cache_dirs(root):
        try:
            out.extend(sorted(d.glob("*.cache.json")))
        except Exception:
            pass
    return out


def _dir_size(p: Path, budget: int = 4000) -> int:
    """目录大小。只数到 budget 个文件就收手 —— 游戏动辄几万个文件，
    为了显示一个数字去遍历整棵树不值得，用户也能接受「大概」。"""
    total = 0
    n = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(p):
            for fn in filenames:
                try:
                    total += (Path(dirpath) / fn).stat().st_size
                except OSError:
                    continue
                n += 1
                if n >= budget:
                    return total
    except Exception:
        pass
    return total


def shortcut_files(title: str, root: Path) -> list[Path]:
    """这个游戏可能对应的快捷方式文件与图标目录。

    不能只按「当前算出来的 slug」找：用户可能改过标题、或者早先版本
    用的 slug 规则不一样。所以 applications 目录里所有 slug 带的
    路径哈希对得上的都算命中，宁可多删也不留下孤儿图标。
    """
    from . import shortcut as sc

    out: list[Path] = []
    app_dir = sc._applications_dir()
    slug = sc._slug(title, str(root))
    out.append(app_dir / f"{slug}.desktop")
    icon_dir = app_dir / "galgame-tl-icons" / slug
    out.append(icon_dir)
    for d in sc._desktop_dirs():
        out.append(d / f"{slug}.desktop")

    # 兜底：换个标题重算过的 slug 也一并收进来
    tail = slug.rsplit("-", 1)[-1]
    try:
        for p in app_dir.glob(f"*.desktop"):
            if p.name.endswith(f"-{tail}.desktop") and p not in out:
                out.append(p)
        icons = app_dir / "galgame-tl-icons"
        if icons.is_dir():
            for p in icons.glob(f"*-{tail}"):
                if p not in out:
                    out.append(p)
    except Exception:
        pass
    return out


def probe(entry: GameEntry, log: Optional[Callable[[str], None]] = None) -> GameStatus:
    """现场看一眼磁盘，填 `GameStatus`。任何一步失败都只记 note，不抛。"""
    st = GameStatus()
    root = entry.root
    if not root.is_dir():
        st.notes.append("目录已被删除或移动")
        return st
    st.exists = True

    try:
        det = detect(root)
        if det.ok:
            st.engine, st.variant = det.engine, det.variant
            if det.launch:
                st.launch = str(det.launch)
        else:
            st.notes.append("引擎没识别出来")
    except Exception as e:
        st.notes.append(f"识别失败: {e}")

    cd = cache_dir(root)
    st.cache_dir = str(cd) if cd.is_dir() else ""
    st.cache_entries = 0
    files = cache_files(root)
    for f in files:
        try:
            n = len(json.loads(f.read_text(encoding="utf-8")))
            st.cache_entries = max(st.cache_entries, n)
        except Exception:
            st.notes.append(f"缓存读不出来（可能正在写）: {f.name}")
    st.has_snapshot = any(
        list(d.glob("*.bak")) or (d / "_bak_data").is_dir()
        for d in cache_dirs(root))

    # Unity 运行时汉化：BepInEx 装上了才算
    st.runtime_ready = (root / "BepInEx" / "core" / "BepInEx.Preloader.dll").is_file()

    title = entry.title or root.name
    for p in shortcut_files(title, root):
        if p.is_file() and p.suffix == ".desktop":
            st.shortcut_path = str(p)
            break

    st.size_bytes = _dir_size(root)
    return st


def human_size(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


# ------------------------------------------------------------------ 扫描

def default_scan_roots() -> list[Path]:
    home = Path.home()
    cands = [
        home / "下载", home / "Downloads",
        home / "游戏", home / "Games", home / "games",
        home / "桌面", home / "Desktop",
        home / ".local" / "share" / "Steam" / "steamapps" / "common",
        home / "文档",
    ]
    out, seen = [], set()
    for p in cands:
        if p.is_dir():
            k = str(p.resolve())
            if k not in seen:
                seen.add(k)
                out.append(p)
    return out


def scan(roots: Optional[Iterable[Path]] = None,
         depth: int = SCAN_DEPTH,
         limit: int = SCAN_LIMIT,
         stop: Optional[Callable[[], bool]] = None,
         log: Optional[Callable[[str], None]] = None) -> list[tuple[Path, Detection]]:
    """在常见目录里找能被识别的游戏。

    只看目录，且**识别成功就不再往下钻**——游戏目录里通常还有几万个
    资源文件，钻进去既慢又没有意义。
    """
    log = log or (lambda *a: None)
    roots = list(roots) if roots is not None else default_scan_roots()
    found: list[tuple[Path, Detection]] = []
    seen: set[str] = set()

    # 广度优先，逐层加目录，这样浅层的游戏先被找到
    queue: list[tuple[Path, int]] = [(r, 0) for r in roots]
    while queue and len(found) < limit:
        if stop and stop():
            break
        d, lvl = queue.pop(0)
        try:
            if lvl > 0:
                det = detect(d)
                if det.ok:
                    k = str(det.root.resolve())
                    if k not in seen:
                        seen.add(k)
                        found.append((det.root, det))
                        log(f"  ✓ {det.root.name}  →  {det.summary()}")
                    continue        # 认出来就不再下钻
        except Exception:
            continue
        if lvl >= depth:
            continue
        try:
            subdirs = [p for p in d.iterdir() if p.is_dir() and not p.is_symlink()]
        except (PermissionError, OSError):
            continue
        for p in subdirs:
            if p.name.startswith(".") or p.name.startswith("_"):
                continue
            queue.append((p, lvl + 1))
    return found


# ------------------------------------------------------------------ 删除

@dataclass
class DeletePlan:
    """删除计划。每一条 target 都是**会真正被删掉**的绝对路径。"""
    root: str = ""
    game_paths: list[str] = field(default_factory=list)
    cache_paths: list[str] = field(default_factory=list)
    shortcut_paths: list[str] = field(default_factory=list)
    game_bytes: int = 0
    cache_bytes: int = 0
    blocked: str = ""            # 非空 = 拒绝执行的原因

    @property
    def ok(self) -> bool:
        return not self.blocked

    def all_paths(self, game: bool, cache: bool, shortcut: bool) -> list[str]:
        out: list[str] = []
        if game:
            out += self.game_paths
        if cache:
            out += self.cache_paths
        if shortcut:
            out += self.shortcut_paths
        return out


# 绝对不能删的目录。用户手滑把「/」加进库、或者库文件被改坏时，
# 这一层是最后一道闸。
_FORBIDDEN = {
    Path("/"), Path("/home"), Path("/usr"), Path("/etc"), Path("/var"),
    Path("/boot"), Path("/opt"), Path("/root"), Path("/tmp"), Path("/srv"),
    Path("/bin"), Path("/lib"), Path("/lib64"), Path("/media"), Path("/mnt"),
}


def plan_delete(entry: GameEntry, log: Optional[Callable[[str], None]] = None) -> DeletePlan:
    """算出「删这个游戏」要动哪些路径，不执行任何删除。"""
    log = log or (lambda *a: None)
    plan = DeletePlan(root=str(entry.root))
    root = entry.root
    try:
        real = root.resolve()
    except Exception:
        plan.blocked = f"路径无法解析: {root}"
        return plan

    if not real.is_absolute():
        plan.blocked = "路径不是绝对路径"
        return plan
    if real in _FORBIDDEN or real == Path.home().resolve():
        plan.blocked = f"拒绝删除系统目录: {real}"
        return plan
    # 深度 < 2（比如 /home/xxx）一律拒绝：游戏不可能装在那里
    if len(real.parts) < 3:
        plan.blocked = f"路径太浅，不像游戏目录: {real}"
        return plan
    if not real.is_dir():
        plan.blocked = "目录不存在（可能已经删过了）"
        return plan

    plan.game_paths = [str(real)]
    plan.game_bytes = _dir_size(real)

    for d in cache_dirs(real):
        # 缓存目录绝不能等于游戏目录本身（那是「删缓存」变「删游戏」），
        # 也不能是它的祖先（同理）。嵌套在游戏目录里是正常的 ——
        # 有些布局就是 `<游戏>/_galgame_tl/`，删缓存时要能单独删掉它。
        try:
            dr = d.resolve()
        except Exception:
            continue
        if dr == real:
            continue
        try:
            real.relative_to(dr)        # real 在 dr 下面 → dr 是祖先
            continue
        except ValueError:
            pass
        plan.cache_paths.append(str(dr))
        plan.cache_bytes += _dir_size(d)

    title = entry.title or real.name
    for p in shortcut_files(title, real):
        if p.exists():
            plan.shortcut_paths.append(str(p))
            log(f"[删除] 快捷方式 {p}")
    return plan


def _safe_rm(p: Path) -> tuple[bool, str]:
    """删一个路径。删前再确认一次它不在受保护目录里。"""
    try:
        rp = p.resolve()
    except Exception:
        rp = p
    if rp in _FORBIDDEN or len(rp.parts) < 2:
        return False, f"拒绝删除受保护路径 {rp}"
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        return True, ""
    except Exception as e:
        return False, str(e)


def apply_delete(plan: DeletePlan, *, game: bool = True, cache: bool = True,
                 shortcut: bool = True,
                 log: Optional[Callable[[str], None]] = None) -> tuple[int, list[str]]:
    """执行删除计划，返回 (删掉几项, 失败信息列表)。"""
    log = log or (lambda *a: None)
    if not plan.ok:
        return 0, [plan.blocked]
    done = 0
    errs: list[str] = []
    targets = plan.all_paths(game, cache, shortcut)
    # 先删快捷方式再删游戏：万一游戏目录删到一半失败，图标不会变孤儿
    order = []
    if shortcut:
        order += plan.shortcut_paths
    if cache:
        order += plan.cache_paths
    if game:
        order += plan.game_paths
    for s in order:
        p = Path(s)
        if not p.exists():
            continue
        ok, err = _safe_rm(p)
        if ok:
            done += 1
            log(f"[删除] ✓ {p}")
        else:
            errs.append(f"{p}: {err}")
            log(f"[删除] ✗ {p} —— {err}")
    if shortcut:
        # 图标父目录空了就顺手收掉，免得 applications 里堆一串空壳
        icons = None
        from . import shortcut as sc
        icons = sc._applications_dir() / "galgame-tl-icons"
        try:
            if icons.is_dir() and not any(icons.iterdir()):
                icons.rmdir()
        except Exception:
            pass
    return done, errs


def open_in_file_manager(p: Path, log: Optional[Callable[[str], None]] = None) -> bool:
    """用桌面环境的文件管理器打开目录（不阻塞）。"""
    import subprocess
    log = log or (lambda *a: None)
    try:
        subprocess.Popen(["xdg-open", str(p)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log(f"[打开] 失败: {e}")
        return False
