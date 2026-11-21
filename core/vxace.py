"""RPG Maker VX Ace（RGSS3）支持。

MV/MZ 的文本在 JSON 里，VX Ace 的文本在 `Data/*.rvdata2` 里 —— 那是 Ruby 的
`Marshal.dump` 字节流，必须用 Marshal 编解码器读写（`vendor/rvdata/`）。
归档 `Game.rgss3a` 是另一层：条目表 + 每 4 字节一组 XOR，见 `unpack()`。

翻译目标是这几处的字符串：
- 事件指令 code 401（「显示文字」）
- 事件指令 code 405（滚动文本）
- 事件指令 code 102 的 params[0]（「显示选项」的分支）与 402 的 params[1]（选项文本）
- 数据库里的名字/描述（Actors / Classes / Items / Weapons / Armors / Enemies /
  States / Skills / Tilesets 的 name、description、nickname、note）
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

# ---------------------------------------------------------------- RGSSAD 解包

_RGSS_MAGIC = {
    b"RGSSAD\x00\x01": 1,
    b"RGSSAD\x00\x02": 2,
    b"RGSSAD\x00\x03": 3,
}


def _rgss_magic_key(head: bytes) -> tuple[int, int]:
    """返回 (版本, magic)。v3 是 *9+3 一次算出，全归档不变。"""
    ver = head[7]
    raw = struct.unpack("<I", head[8:12])[0]
    if ver == 3:
        return 3, (raw * 9 + 3) & 0xFFFFFFFF
    return ver, 0xDEADCAFE


def _decrypt_v3(f, offset: int, size: int, key: int) -> bytes:
    f.seek(offset)
    data = bytearray(f.read(size))
    n = size // 4
    for i in range(n):
        base = i * 4
        for j in range(4):
            data[base + j] ^= (key >> (8 * j)) & 0xFF
        key = (key * 7 + 3) & 0xFFFFFFFF
    # 尾部不足 4 字节：按组内偏移移位（依据 Parser.cs read_data）
    tail = n * 4
    while tail < size:
        data[tail] ^= (key >> (8 * (tail - n * 4))) & 0xFF
        tail += 1
    return bytes(data)


def _encrypt_v3(data: bytes, key: int) -> bytes:
    """`_decrypt_v3` 的逆运算。XOR 对称，算法一致。"""
    buf = bytearray(data)
    size = len(buf)
    n = size // 4
    for i in range(n):
        base = i * 4
        for j in range(4):
            buf[base + j] ^= (key >> (8 * j)) & 0xFF
        key = (key * 7 + 3) & 0xFFFFFFFF
    tail = n * 4
    while tail < size:
        buf[tail] ^= (key >> (8 * (tail - n * 4))) & 0xFF
        tail += 1
    return bytes(buf)


def _decrypt_v12(f, offset: int, size: int, key: int) -> bytes:
    f.seek(offset)
    data = bytearray(f.read(size))
    for i in range(len(data)):
        data[i] ^= (key >> (8 * (i % 4))) & 0xFF
        if i % 4 == 3:
            key = (key * 7 + 3) & 0xFFFFFFFF
    return bytes(data)


def _read_entries(arc: Path) -> list[tuple[bytes, str, int, int, int]]:
    """读归档条目表 → [(原始名字字节, 规范化名字, offset, size, key)]。

    **原始名字字节必须留着**：RGSS3 运行时按字符串查文件，归档里存的是
    反斜杠路径（`Data\\Scripts.rvdata2`）。repack 时必须原样写回反斜杠，
    换成正斜杠会让游戏报 `failed to load script`（找不到 Scripts.rvdata2，
    整包脚本加载失败，游戏直接退出）。
    """
    with open(arc, "rb") as f:
        head = f.read(12)
        ver, magic = _rgss_magic_key(head)
        out = []
        while True:
            buf = f.read(16)
            if len(buf) < 16:
                break
            off, size, key, nlen = struct.unpack("<IIII", buf)
            if ver == 3:
                off ^= magic
                size ^= magic
                key ^= magic
                nlen ^= magic
                if off == 0 or nlen > 1024:
                    break
            else:
                if off == 0:
                    break
            raw = bytearray(f.read(nlen))
            for k in range(nlen):
                raw[k] ^= (magic >> (8 * (k % 4))) & 0xFF
            # 规范化副本只给本地文件系统用；写回归档一律用 raw
            norm = bytes(raw).replace(b"\\", b"/").decode("utf-8", "replace")
            out.append((bytes(raw), norm, off, size, key))
        return out


def list_entries(arc: Path) -> list[tuple[str, int, int, int]]:
    """读归档条目表 → [(规范化名字, offset, size, key)]。解包用这个。"""
    return [(norm, off, size, key)
            for _raw, norm, off, size, key in _read_entries(arc)]


def unpack(arc: Path, dest: Path) -> int:
    """解到 dest，返回文件数。已存在的不覆盖（可重复运行）。"""
    ents = list_entries(arc)
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(arc, "rb") as f:
        ver = _rgss_magic_key(f.read(12))[0]
        f.seek(0)
        for name, off, size, key in ents:
            dst = dest / name
            if dst.exists() and dst.stat().st_size == size:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if ver == 3:
                dst.write_bytes(_decrypt_v3(f, off, size, key))
            else:
                dst.write_bytes(_decrypt_v12(f, off, size, key))
            n += 1
    return n


def find_archive(root: Path) -> Path | None:
    for pat in ("*.rgss3a", "*.rgss2a", "*.rgssad", "**/*.rgss3a"):
        for p in root.glob(pat):
            return p
    return None


def repack(arc: Path, root: Path, out: Path | None = None,
           replace_prefix: str = "Data/", log=None) -> tuple[bool, str]:
    """把 `root` 下的明文改动重新打包回归档。

    为什么要做这一步：RGSS3 **优先读 `Game.rgss3a`**，磁盘上的明文 `Data/`
    只是解包产物 —— 不改归档，游戏里看到的还是英文原文。

    设计要点：
    - 条目顺序、每个条目的初始 key、**文件名原始字节**沿用原归档，一个都不动。
      名字必须带反斜杠写回（见 `_read_entries` 的注释），换成正斜杠游戏会
      报 `failed to load script`。
    - 未改动的资源**直接搬加密块**（不解密再加密，省时且零风险）。
    - 只有 `replace_prefix` 开头且明文大小与原条目不同的文件，才读明文
      重新加密 —— 这些正是被翻译过的 rvdata2。
    - 条目表末尾那个 16 字节终止哨兵（off 字段存 magic）必须照写，
      少了它 list_entries 会把数据区第一个块当条目读。
    """
    src = Path(arc)
    root = Path(root)
    out = Path(out) if out else src.with_suffix(src.suffix + ".new")

    ents = _read_entries(src)
    if not ents:
        return False, "归档条目表为空"

    with open(src, "rb") as f:
        head = f.read(12)
        ver, magic = _rgss_magic_key(head)
        if ver != 3:
            return False, f"暂只支持 v3（这个归档是 v{ver}）"

        # 1. 决定每个条目用哪份数据
        #
        # 注意：**不能**用「明文大小 != 归档大小」来判断有没有改过 ——
        # 译文长度碰巧和原文一样时（很常见：短词换短词）会漏判，游戏仍然
        # 显示英文。所以 Data/ 开头的条目一律重新加密，反正只有 992 个文件、
        # 20MB 左右，代价可以忽略；其余资源直接搬加密块。
        plan = []          # [(原始名字字节, size, key, source)]
        for raw, norm, off, size, key in ents:
            src_file = root / norm
            if norm.startswith(replace_prefix) and src_file.is_file():
                plan.append((raw, src_file.stat().st_size, key, ("file", src_file)))
            else:
                plan.append((raw, size, key, ("arc", off, size)))

        # 2. 算新条目表长度（用原始名字字节的长度，不是规范化后的）
        tbl_len = 12 + sum(16 + len(raw) for raw, _s, _k, _ in plan)
        data_start = tbl_len + 16            # 末尾 16 字节终止哨兵

        # 3. 重算 offset
        layout = []
        cur = data_start
        for raw, size, key, source in plan:
            layout.append((raw, cur, size, key, source))
            cur += size

        # 4. 写新归档
        tmp = out.with_suffix(out.suffix + ".writing")
        changed = 0
        with open(tmp, "wb") as w:
            w.write(b"RGSSAD\x00\x03")
            inv9 = pow(9, -1, 1 << 32)
            raw_magic = ((magic - 3) * inv9) & 0xFFFFFFFF
            w.write(struct.pack("<I", raw_magic))

            for raw, off, size, key, source in layout:
                w.write(struct.pack("<IIII",
                                    off ^ magic, size ^ magic, key ^ magic,
                                    len(raw) ^ magic))
                enc = bytearray(raw)
                for k in range(len(enc)):
                    enc[k] ^= (magic >> (8 * (k % 4))) & 0xFF
                w.write(enc)

            # 终止哨兵
            w.write(struct.pack("<IIII", magic, 0, 0, 0))

            for raw, off, size, key, source in layout:
                if source[0] == "arc":
                    f.seek(source[1])
                    w.write(f.read(source[2]))
                else:
                    plain = source[1].read_bytes()
                    w.write(_encrypt_v3(plain, key))
                    changed += 1
        os.replace(tmp, out)
    return True, (f"{len(layout)} 个条目，其中 {changed} 个重新加密（翻译过的），"
                  f"输出 {out.name}（{out.stat().st_size // 1048576} MB）")


# ---------------------------------------------------------------- 文本定位

# 数据库文件 → 值得翻的字段
DB_FIELDS = {
    "Actors": ("@name", "@nickname", "@description", "@note"),
    "Classes": ("@name", "@description", "@note"),
    "Items": ("@name", "@description", "@note"),
    "Weapons": ("@name", "@description", "@note"),
    "Armors": ("@name", "@description", "@note"),
    "Enemies": ("@name", "@note"),
    "States": ("@name", "@message1", "@message2", "@message3", "@message4", "@note"),
    "Skills": ("@name", "@description", "@message1", "@message2", "@note"),
    "Tilesets": ("@name", "@note"),
    "CommonEvents": ("@name",),
}


_SCRIPT_HINTS = (
    "$game_", "@actor.", "@user", "@item.", "include?(", ".each{", ".each do",
    "b.result", "p.members", "$data_skills", "$game_party", "$game_temp",
    "$game_actors", "reserve_common_event",
)


def is_script_text(s: str) -> bool:
    """判断一段文本是「游戏脚本/标签」还是「给人看的文字」。

    VX Ace 的 note / message 字段里混着大量不是自然语言的东西：
    `<hide eval>\r\n$game_party.in_battle and ...</hide eval>`、
    `<tp mode: 0>`、`<cooldown: 3>`、`<formula effect>...Ruby...</formula effect>`。
    这些**绝对不能送去翻译**——实测谷歌会把 `<hide eval>` 翻成 `<隐藏评估>`，
    把 `$game_party` 里的 `and` 翻成「和」，游戏逻辑当场就废。

    判据（任一命中就算脚本，不翻）：
    1. 每一行都以 `<` 开头（纯标签块）
    2. 含 Ruby / RGSS 代码特征（$game_xxx、@actor.、include?(、.each{ 等）
    """
    s = (s or "").strip()
    if not s:
        return False
    lines = [ln for ln in s.replace("\r\n", "\n").split("\n") if ln.strip()]
    if lines and all(ln.strip().startswith("<") for ln in lines) and ">" in s:
        return True
    # 混合型 `<标签>\r\n值`：`<AI: limit move: 126 once in: 3>\r\nhealer`
    # 正文是标签的参数（healer / fighter / mage），不是给人看的文字。
    head, body = split_script_prefix(s)
    if head != s and body:
        return True
    return any(h in s for h in _SCRIPT_HINTS)


def split_script_prefix(s: str) -> tuple[str, str]:
    """把 `<标签>\r\n正文` 拆成 (标签部分, 正文)。

    这类混合文本要只翻正文、原样保留标签。抠不出来就把整串当标签。
    """
    import re as _re
    m = _re.match(r"^((?:\s*<[^<>\n]*>[ \t]*(?:\r\n|\r|\n)?)+)(.*)$", s, _re.S)
    if m and m.group(2).strip():
        return m.group(1), m.group(2)
    return s, ""


def _to_str(v) -> str:
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    if isinstance(v, str):
        return v
    data = getattr(v, "data", None)      # RubyString
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", "replace")
    return ""


def _mk_str(old, text: str):
    """按 old 的类型造新值。RubyString 必须带上原 ivars（{"E": True} = UTF-8）。"""
    from vendor.rvdata.rubytypes import RubyString

    if isinstance(old, RubyString):
        return RubyString(text.encode("utf-8"), dict(old.ivars))
    if isinstance(old, (bytes, bytearray)):
        return text.encode("utf-8")
    return text


def _set_str(node, attr: str, text: str) -> bool:
    ivars = getattr(node, "ivars", None)
    if ivars is None or attr not in ivars:
        return False
    ivars[attr] = _mk_str(ivars[attr], text)
    return True


def _walk_commands(node):
    """遍历事件指令列表（@list）。"""
    lst = getattr(node, "ivars", {}).get("@list")
    if isinstance(lst, list):
        return lst
    return []


def _map_event_commands(mobj):
    """遍历单个 RPG::Map 对象里所有事件指令，返回 [(event_id, page_idx, cmd_idx, cmd)]。

    MapXXX.rvdata2 顶层是 RPG::Map（单个对象，不是数组），事件在
    @events（RubyHash：event_id => RPG::Event）→ 每事件的 @pages
    （Array of RPG::Event::Page）→ 每页的 @list（Array of RPG::EventCommand）。
    逐页逐条摊平，collect 与 apply_all 用同一套 (eid, pi, ci) 索引定位。
    """
    out = []
    events = getattr(mobj, "ivars", {}).get("@events")
    pairs = getattr(events, "pairs", None)
    if pairs is None and isinstance(events, dict):
        pairs = list(events.items())
    for eid, ev in (pairs or []):
        pages = getattr(ev, "ivars", {}).get("@pages")
        if not isinstance(pages, list):
            continue
        for pi, page in enumerate(pages):
            for ci, cmd in enumerate(_walk_commands(page)):
                out.append((eid, pi, ci, cmd))
    return out


def collect(items_map: dict) -> list[tuple]:
    """把 load 出来的对象里所有待翻文本摊平。

    返回 [(定位元组, 原文)]，定位元组用于回填。
    """
    out: list[tuple] = []

    for fname, root_obj in items_map.items():
        # MapXXX.rvdata2 顶层是单个 RPG::Map 对象（RubyObject），不是数组。
        # 必须先于 isinstance(list) 判断，否则地图对话整片漏收（旧代码把它当
        # list 取 data[0].@list，而事件实际在 @events→@pages→@list）。
        if fname.startswith("Map") and fname != "MapInfos":
            mobj = root_obj
            if not hasattr(mobj, "ivars"):
                continue
            for eid, pi, ci, cmd in _map_event_commands(mobj):
                iv = getattr(cmd, "ivars", {})
                code = iv.get("@code")
                params = iv.get("@parameters")
                if not isinstance(params, list) or not params:
                    continue
                if code in (401, 405):
                    t = _to_str(params[0])
                    if t.strip():
                        out.append((("mcmd", fname, eid, pi, ci), t))
                elif code == 102 and len(params) >= 1:
                    for oi, opt in enumerate(params[0] if isinstance(params[0], list) else []):
                        t = _to_str(opt)
                        if t.strip():
                            out.append((("mopt", fname, eid, pi, ci, oi), t))
            continue

        data = root_obj
        if not isinstance(data, list):
            continue

        if fname == "CommonEvents":
            for idx, ev in enumerate(data):
                if ev is None:
                    continue
                for ci, cmd in enumerate(_walk_commands(ev)):
                    iv = getattr(cmd, "ivars", {})
                    code = iv.get("@code")
                    params = iv.get("@parameters")
                    if not isinstance(params, list) or not params:
                        continue
                    if code in (401, 405):
                        t = _to_str(params[0])
                        if t.strip():
                            out.append((("cmd", fname, idx, ci), t))
                    elif code == 102 and len(params) >= 1:
                        for oi, opt in enumerate(params[0] if isinstance(params[0], list) else []):
                            t = _to_str(opt)
                            if t.strip():
                                out.append((("opt", fname, idx, ci, oi), t))
            continue

        if fname == "MapInfos":
            continue

        # 数据库表
        fields = DB_FIELDS.get(fname)
        if not fields:
            continue
        for idx, node in enumerate(data):
            if node is None or not hasattr(node, "ivars"):
                continue
            if idx == 0:      # 索引 0 是空占位
                continue
            for attr in fields:
                if attr in node.ivars:
                    t = _to_str(node.ivars[attr])
                    if t.strip():
                        out.append((("db", fname, idx, attr), t))
    return out


def apply_all(items_map: dict, done: dict[tuple, str]) -> int:
    """把 done{定位: 译文} 写回 items_map。"""
    n = 0
    # 地图文件顶层是 RPG::Map 对象，按 (eid, pi, ci) 索引命令。每次调用
    # 现算一次，因为同一文件会被多条 mcmd/mopt 命中，按索引直取最快。
    _map_cmd_cache: dict[str, list[tuple]] = {}

    def map_cmd_at(fname, eid, pi, ci):
        if fname not in _map_cmd_cache:
            mobj = items_map.get(fname)
            _map_cmd_cache[fname] = _map_event_commands(mobj) if hasattr(mobj, "ivars") else []
        for (_eid, _pi, _ci, cmd) in _map_cmd_cache[fname]:
            if (_eid, _pi, _ci) == (eid, pi, ci):
                return cmd
        return None

    for key, text in done.items():
        kind = key[0]
        if kind in ("mcmd", "mopt"):
            _, fname, eid, pi, ci = key[:5]
            cmd = map_cmd_at(fname, eid, pi, ci)
            if cmd is None:
                continue
            params = cmd.ivars.get("@parameters")
            if not isinstance(params, list) or not params:
                continue
            if kind == "mcmd":
                params[0] = _mk_str(params[0], text)
                n += 1
            elif isinstance(params[0], list) and key[5] < len(params[0]):
                params[0][key[5]] = _mk_str(params[0][key[5]], text)
                n += 1
        elif kind == "cmd":
            _, fname, idx, ci = key
            data = items_map.get(fname)
            if not isinstance(data, list) or idx >= len(data) or data[idx] is None:
                continue
            cmds = _walk_commands(data[idx])
            if ci < len(cmds):
                params = cmds[ci].ivars.get("@parameters")
                if isinstance(params, list) and params:
                    params[0] = _mk_str(params[0], text)
                    n += 1
        elif kind == "opt":
            _, fname, idx, ci, oi = key
            data = items_map.get(fname)
            if not isinstance(data, list) or idx >= len(data) or data[idx] is None:
                continue
            cmds = _walk_commands(data[idx])
            if ci < len(cmds):
                params = cmds[ci].ivars.get("@parameters")
                if isinstance(params, list) and params and isinstance(params[0], list):
                    if oi < len(params[0]):
                        params[0][oi] = _mk_str(params[0][oi], text)
                        n += 1
        elif kind == "db":
            _, fname, idx, attr = key
            data = items_map.get(fname)
            if not isinstance(data, list) or idx >= len(data) or data[idx] is None:
                continue
            if _set_str(data[idx], attr, text):
                n += 1
    return n


# ---------------------------------------------------------------- 安装包自解压

_IS_CAB_OFFSETS_HINT = "InstallShield"


def cab_extract_bin() -> Path | None:
    """找可用的 cabextract：系统装的，或本地 vendor 的。"""
    import shutil as _sh
    p = _sh.which("cabextract")
    if p:
        return Path(p)
    local = Path(__file__).resolve().parent.parent / "vendor" / "cabextract"
    if local.is_dir():
        # vendor 里放的是 dpkg-deb -x 解出来的树
        for c in (local / "usr" / "bin" / "cabextract", local / "cabextract"):
            if c.is_file():
                return c
    return None


def _lib_path_for(bin_path: Path) -> str:
    """cabextract 依赖 libmspack，vendor 时要一起给 LD_LIBRARY_PATH。"""
    root = bin_path
    for _ in range(4):
        cand = root / "usr" / "lib" / "x86_64-linux-gnu"
        if cand.is_dir():
            return str(cand)
        root = root.parent
    return ""


def is_self_extracting_exe(path: Path) -> bool:
    """判断这个 exe 是不是自解压安装包（InstallShield / Inno / NSIS）。"""
    try:
        sz = path.stat().st_size
        if sz < 1 << 20:
            return False
        with open(path, "rb") as f:
            head = f.read(1024)
            if head[:2] != b"MZ":
                return False
            # 扫前 1MB 找特征串
            f.seek(0)
            blob = f.read(1 << 20)
            for sig in (b"InstallShield", b"Inno Setup", b"Nullsoft", b"MSCF"):
                if sig in blob:
                    return True
    except OSError:
        return False
    return False


def extract_installer(exe: Path, dest: Path, log=None) -> tuple[bool, str]:
    """把自解压 exe 拆到 dest。返回 (成功, 说明)。"""
    import subprocess

    binp = cab_extract_bin()
    if not binp:
        return False, ("需要 cabextract 才能拆安装包。"
                       "装它：sudo apt install cabextract")
    dest.mkdir(parents=True, exist_ok=True)
    env = None
    libp = _lib_path_for(binp)
    if libp:
        import os
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = libp + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    cmd = [str(binp), "-q", "-d", str(dest), str(exe)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800, env=env)
    except subprocess.TimeoutExpired:
        return False, "解包超时（超过 30 分钟）"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
        return False, f"cabextract 失败: {msg[:200]}"
    n = sum(1 for _ in dest.rglob("*") if _.is_file())
    if log and n:
        log(f"  解出 {n} 个文件 → {dest}")
    return n > 0, f"解出 {n} 个文件"
