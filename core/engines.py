"""引擎适配层：统一接口，把三种引擎收在同一套 extract/apply 语义下。

统一接口
--------
每个适配器实现：
  can_handle(det)                 bool
  prepare(root, log)              解包/反编译等前置步骤
  extract(root, log) -> list[dict]
                                  每条: {id, src, who, label, file, path, kind}
                                  其中 id 必须唯一且 apply 能凭 id 定位
  apply(root, items, log) -> int  把 dst 写回；返回写入条数
  finalize(root, log)             字体、校验、清理

设计取舍（为什么不是三个独立脚本）
--------------------------------
三个脚本各写一套 CLI 也能用，但 GUI 里要「拖进去 → 自动判断 → 一键到底」，
就要求统一的中间格式。中间格式只保留翻译真正需要的东西：id / src / who / label，
引擎特有的定位信息全部塞进 `path`（RPG Maker 的 JSON 路径）或 `id`（Ren'Py 的
file:line）。这样翻译层完全不需要知道引擎是什么。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
UNRPYC = ROOT / "vendor" / "unrpyc" / "unrpyc.py"


class EngineAdapter:
    name = "base"

    def can_handle(self, det) -> bool:
        return det.engine == self.name

    def prepare(self, root: Path, log: Callable[[str], None]) -> None:
        pass

    def extract(self, root: Path, log: Callable[[str], None]) -> list[dict]:
        raise NotImplementedError

    def apply(self, root: Path, items: list[dict], log: Callable[[str], None]) -> int:
        raise NotImplementedError

    def finalize(self, root: Path, log: Callable[[str], None]) -> None:
        pass


# ================================================================ Ren'Py
# 以下三个函数与 tools/renpy_tl.py 中的实现保持一致（已对 Ren'Py 8.5.3 逐字节验证）
NON_SAY_KEYWORDS = {
    "if", "elif", "else", "while", "for", "return", "call", "jump", "scene", "show",
    "hide", "with", "play", "stop", "queue", "window", "menu", "label", "init",
    "python", "define", "default", "image", "transform", "style", "screen",
    "translate", "voice", "pause", "nvl", "and", "or", "not", "in", "is",
    "text", "textbutton", "imagebutton", "add", "bar", "button", "grid",
    "fixed", "frame", "hbox", "vbox", "imagemap", "input", "key", "null", "side",
    "timer", "use", "viewport", "vpgrid", "nearrect", "drag", "draggroup",
    "hotbar", "mousearea", "style_prefix", "style_suffix", "at", "auto",
    "background", "foreground", "thumb", "idle", "hover", "insensitive",
    "selected_idle", "selected_hover", "selected_insensitive", "action",
    "xpos", "ypos", "xalign", "yalign", "xsize", "ysize", "xysize", "pos",
    "size", "spacing", "padding", "margin", "align", "area", "left_padding",
    "right_padding", "top_padding", "bottom_padding", "xminimum", "yminimum",
    "xmaximum", "ymaximum", "font", "color", "outlines", "kerning",
    "line_spacing", "text_align", "layout", "language", "substitute",
    "runtime", "layer", "tag", "as", "behind", "onlayer", "zorder", "expression",
    "filename", "audio", "sound", "music", "loop", "fadein", "fadeout", "noloop",
    "volume", "channel", "if_changed", "relative_volume", "predict_all",
}

SAY_RE = re.compile(
    r'^(?P<indent>\s*)(?P<who>[A-Za-z_][\w\.]*)?\s*(?P<q>"(?:[^"\\]|\\.)*")\s*(?P<tail>.*)$'
)

# Ren'Py 的 say 语句可以带 image attributes：`mc unsure 25 "台词"`。
# 这些 attribute 会原样拼进 get_code() 参与 md5（见 renpy/ast.py:915 Say.get_code），
# 所以漏掉它们的话 identifier 全错，翻译块一个都对不上 —— 游戏里就是英文原文。
# 语法见 renpy/parser.py:1545 say_attributes()：可带 `-` 前缀，`@` 之后是临时属性。
_IMG_WORD = r"[-0-9A-Za-z_\u00a0-\ufffd][-0-9A-Za-z_\u00a0-\ufffd]*"
# who 之后、引号之前的属性串。贪婪匹配到最后一个引号前的 token 序列。
_ATTR_SAY_RE = re.compile(
    r'^(?P<indent>\s*)(?P<who>[A-Za-z_][\w\.]*)[ \t]+'
    # 最后一个 attribute 和引号之间可以没有空格（实际见过
    # `fc1 sff 50"You can be touched..."`），所以尾随空白用 [ \t]*。
    r'(?P<attrs>(?:-?' + _IMG_WORD + r'[ \t]+)*-?' + _IMG_WORD + r')'
    r'[ \t]*(?P<q>"(?:[^"\\]|\\.)*")[ \t]*(?P<tail>.*)$'
)
# `@` 之后的临时属性：`mc happy @ v2 "..."` 很少见但语法允许
_ATTR_TEMP_RE = re.compile(r'^(?P<a>.*?)[ \t]+@[ \t]*(?P<t>' + _IMG_WORD + r'(?:[ \t]+-?' + _IMG_WORD + r')*)?$')

# 界面文案：`_("Back")` / `__( "...")` / `_p("...")` / `_(u"Skip Mode")`。
# 注意引擎自己的 000statements.rpy 等用的是双下划线 `__(`；00library.rpy 里
# 还有 `_(u"...")` 这种带 u 前缀的 py2 写法。两种都要匹配，否则会漏。
# 引号三种都要认：`_p('...')` 实际见过大量用单引号的（flowchart 那些标题），
# 只认双引号会漏掉 56 条。
# 紧跟其后的相邻字符串字面量（Python 的隐式拼接），中间只允许空白。
_ADJ_STRING_RE = re.compile(r"""\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")

_UI_STRING_RE = re.compile(r"""\b__?p?\(\s*[uU]?(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")


def _unescape(s: str) -> str:
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nx = s[i + 1]
            # `\u00e9` 这种 unicode 转义：Ren'Py 的 lexer 会真的转成字符
            # （renpy/lexer.py:1016 dequote）。不转的话 `Caf\u00e9 Partnership`
            # 和官方 strings 表里的 `Café Partnership` 对不上。
            if nx == "u" and re.fullmatch(r"[0-9a-fA-F]{1,4}", s[i + 2:i + 6] or ""):
                m_hex = re.match(r"[0-9a-fA-F]{1,4}", s[i + 2:i + 6])
                out.append(chr(int(m_hex.group(0), 16)))
                i += 2 + len(m_hex.group(0))
                continue
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", " ": " "}.get(nx, nx))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _escape(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return s


def _encode_say_string(s: str) -> str:
    s = s.replace("\\", "\\\\")
    s = s.replace("\n", "\\n")
    s = s.replace('"', '\\"')
    s = re.sub(r"(?<= ) ", "\\\\ ", s)
    return '"' + s + '"'


def _attr_looks_like_code(attrs: str) -> bool:
    """属性串里出现代码符号时，说明这行不是 say 语句（`if x and "..."` 之类）。

    Ren'Py 的 image_name_component 只允许 [0-9A-Za-z_] 和 `-` 前缀，
    所以任何 `. = ( [ + ' :` 都意味着我们误判了。
    """
    for tok in attrs.split():
        core = tok[1:] if tok.startswith("-") else tok
        if not core:
            return True
        if not re.fullmatch(r"[-0-9A-Za-z_\u00a0-\ufffd]+", core):
            return True
        if core in _PY_KEYWORDS:
            return True
    return False


_PY_KEYWORDS = {
    "and", "or", "not", "in", "is", "if", "else", "for", "while", "return",
    "import", "from", "as", "with", "def", "class", "lambda", "None", "True",
    "False", "try", "except", "finally", "raise", "yield", "global", "assert",
    "pass", "break", "continue", "del", "elif",
}


def _say_code(who: str, text: str, attrs: str = "") -> str:
    """复现 renpy/ast.py:915 Say.get_code()：who + attributes + 编码后的台词。

    attributes 必须原样带进来（含 `-` 前缀和 `@` 分隔），否则 md5 对不上。
    """
    parts = []
    if who:
        parts.append(who)
    if attrs:
        # 源码里 attribute 之间可能有多个空格（`mc_thought frown  50`），
        # 但 get_code 是 " ".join(rv)，AST 里存的是已拆好的 tuple，
        # 所以必须按空白切成 token 再用单空格拼回来，否则 md5 全错。
        parts.extend(attrs.split())
    parts.append(_encode_say_string(text))
    return " ".join(parts)


def _tlid(label: Optional[str], code: str) -> str:
    d = hashlib.md5((code + "\r\n").encode("utf-8")).hexdigest()[:8]
    return d if label is None else label.replace(".", "_") + "_" + d


def _strip_comment(line: str) -> str:
    """去掉行尾 `# 注释`，引号内的 `#` 不动。

    没有这一步的话 `"台词"  # 5.625` 这种行 tail 变成 `# 5.625`，
    被当成未知尾巴整行丢弃 —— BlossomOfPleasure 里 20 句歌词就这么丢了。
    """
    out = []
    i = 0
    n = len(line)
    quote = ""
    while i < n:
        c = line[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(line[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in ('"', "'", "`"):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "#":
            # `#` 之前必须有空白或行首才算注释
            if i == 0 or line[i - 1] in " \t":
                break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


def _logical_lines(text: str):
    """把物理行合并成 Ren'Py 的逻辑行，yield (起始行号, 合并后的行)。

    Ren'Py 的 lexer 是「逻辑行」：引号里的换行是合法的，会被
    `re.sub(r"[ \n]+", " ", s)` 折成一个空格（renpy/lexer.py:1022）。
    不支持的话，像
        bildur "If you like the game ... {/size}.
        You get background information, ..."
    这种跨行台词整句丢掉。
    """
    phys = text.split("\n")
    i = 0
    n = len(phys)
    while i < n:
        start = i + 1
        buf = phys[i]
        while _quote_open(buf) and i + 1 < n:
            i += 1
            # 用真换行拼接，后面 *_unescape* 前会按 Ren'Py 规则折叠成单空格
            buf += "\n" + phys[i]
        yield start, buf
        i += 1


def _quote_open(line: str) -> bool:
    """行末是否还有未闭合的引号（跳过注释，但引号内的 # 不算注释）。"""
    quote = ""
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in ('"', "'", "`"):
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        i += 1
    return bool(quote)


def _iter_say_lines(path: Path):
    """只在 label 块内提取 say；menu prompt 与 choice 内的 nointeract 都要正确标记"""
    text = path.read_text(encoding="utf-8", errors="replace")
    # 行号用逻辑行的**起始物理行号**，和 Ren'Py 报的位置一致。
    numbered = list(_logical_lines(text))
    in_label = False
    label_indent = -1
    prev_code = ""
    # menu 可以嵌套（menu 的 choice 块里再开 menu），必须用栈。
    # 只用单变量的话，内层 menu 结束时把外层状态也抹掉了，
    # 后面外层 menu 的 choice 就认不出来 —— 那些选项文本也就不会进 strings 表。
    menu_stack: list = []   # [(menu_indent, choice_indent)]
    # 当前归属的 label。Ren'Py 的 Restructurer 是**顺序**扫描 AST 的：
    # 遇到 ast.Label 就更新 self.label（renpy/translation/__init__.py:437-445），
    # 之后的 say 全归它，直到下一个 Label。而 `menu 名字:` 会被 parser 展开成
    #   ast.Label(名字) + [say_prompt, Menu(...)]      (renpy/parser.py:696-703)
    # 也就是说 **带名字的 menu 会开一个新 label**，块内所有 say 都归它。
    cur_label = ""
    # screen 块内的 `label` 是 screen 控件、`text_color "#fff"` 是样式属性，
    # 都不是脚本语句。不排除的话 updateService.rpy:598 的
    # `label If(u and ...):` 会被当成 label，把后面整段台词的归属全改掉。
    screen_indent = -1

    for i, line in numbered:
        line = _strip_comment(line)
        s = line.strip()
        if not s:
            continue
        indent = len(line) - len(line.lstrip())

        if screen_indent >= 0 and indent <= screen_indent:
            screen_indent = -1
        if re.match(r"^screen\s+[\w\.]+\s*(?:\([^)]*\))?\s*:", s):
            screen_indent = indent
            continue
        if screen_indent >= 0:
            # screen 内部：只有 `_("…")` 之类界面文案有意义，由 _iter_ui_strings 扫。
            continue

        m_lab = re.match(r"^label\s+([\w\.]+)", s)
        if m_lab:
            in_label, prev_code = True, s
            # label_indent 必须取**最外层** label 的缩进：intro.rpy 里
            # `label intro:`(0) 内部又嵌 `label intro_menu_narrator_01:`(4)，
            # 若把 label_indent 覆盖成 4，后面回到 4 缩进的普通语句（`python:`）
            # 就被误判成"离开 label"，整个 intro 后半段 2 万句全丢。
            label_indent = indent if label_indent < 0 else min(label_indent, indent)
            # 只在 label 缩进不深于当前 menu 时才关掉 menu 状态：
            # choice 块内部可以再写 `label xxx:`（dateS3.rpy:204 就是），
            # 那只是块内的一个 label，外层 menu 仍然有效，清了的话
            # 后面同级的其它 choice（`"I'll cover it for us. ($1000)"`）就认不出来。
            while menu_stack and indent <= menu_stack[-1][0]:
                menu_stack.pop()
            cur_label = m_lab.group(1)
            continue
        if in_label and indent <= label_indent and not s.startswith(("$", '"')):
            # 同级的新语句（define/init/screen…）意味着离开 label
            if not re.match(r'^(say|menu|jump|call|return|if|elif|else|while|for|'
                            r'scene|show|hide|with|play|stop|pause|window|nvl|voice|'
                            r'\$|"|[\w\.]+\s+")', s):
                in_label = False
                menu_stack.clear()
                continue
        if not in_label:
            continue

        # `call xxx from NAME` 也会开一个新 label（renpy/parser.py:762-765：
        # rv.append(ast.Label(loc, name, [], None))），之后的 say 全归它。
        # 注意：`from _call_xxx` 这种以 `_` 开头的名字**不改变当前 label**
        # （renpy/translation/__init__.py:437-445 只把它设成 alternate）。
        m_call_from = re.match(r"^call\s+.+?\s+from\s+([\w\.]+)\s*$", s)
        if m_call_from:
            nm = m_call_from.group(1)
            if not nm.startswith("_"):
                cur_label = nm
            prev_code = s
            continue

        if s == "menu:" or s.startswith("menu "):
            # menu 可以带参数括号：`menu story_xxx_date_m_1():`（renpy/parser.py:686-690
            # 是 label_name 后接 parse_arguments）。带括号不带括号都会开新 label。
            m_menu_name = re.match(r"^menu\s+([\w\.]+)\s*(?:\([^)]*\))?\s*:", s)
            if m_menu_name:
                # 带名字的 menu 开一个新 label，块内 say 全归它
                cur_label = m_menu_name.group(1)
            menu_stack.append([indent, -1])
            prev_code = s
            continue
        while menu_stack and indent <= menu_stack[-1][0]:
            menu_stack.pop()
        if menu_stack:
            menu_indent, choice_indent = menu_stack[-1]
            if True:
                # choice 可以带条件：`"台词" if 条件:`（renpy/parser.py:304）。
                # 只认 `"台词":` 的话，带条件的那条不被当成 choice，于是
                # choice_indent 被重置，块内台词全被误判成 menu prompt 而多带
                # nointeract，identifier 全错。
                m_ch = re.match(r'^"((?:[^"\\]|\\.)*)"\s*(?:if\s+.+)?:\s*$', s)
                if m_ch:
                    menu_stack[-1][1] = choice_indent = indent
                    prev_code = s
                    # menu 的选项文本会被 Ren'Py 收进 strings 表（不是 dialogue 块），
                    # 漏了的话菜单还是英文。
                    ctext = _unescape(m_ch.group(1))
                    if ctext.strip():
                        yield i, cur_label, "", ctext, ":", ""
                    continue
                if choice_indent >= 0 and indent <= choice_indent:
                    menu_stack[-1][1] = choice_indent = -1

        # 先按「带属性」的形态试：`mc unsure 25 "台词"`。
        # SAY_RE 只认 `who "台词"`，带属性时 who 后面还有词就整行匹配不上，
        # 于是这些台词被静默丢弃 —— BlossomOfPleasure 里 2.3 万句就是这么丢的。
        m = None
        attrs = ""
        m_attr = _ATTR_SAY_RE.match(line)
        if m_attr:
            cand = (m_attr.group("attrs") or "").strip()
            w_cand = m_attr.group("who") or ""
            if cand and re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", w_cand) \
                    and w_cand not in NON_SAY_KEYWORDS \
                    and not _attr_looks_like_code(cand):
                m, attrs = m_attr, cand
        if m is None:
            m = SAY_RE.match(line)
        if not m:
            prev_code = s
            continue
        who = m.group("who")
        if who in NON_SAY_KEYWORDS:
            prev_code = s
            continue
        if who and not re.fullmatch(r"[A-Za-z_][\w]*(\.[A-Za-z_]\w*)*", who):
            prev_code = s
            continue
        raw = m.group("q")
        # Ren'Py lexer 对双引号字符串做 `re.sub(r"[ \n]+", " ", s)`，
        # 把跨行的换行和多余空白折成单个空格（renpy/lexer.py:1022）。
        # 不折的话 md5 对不上，块就成孤儿。
        # 注意这是**所有**双引号字符串都要做的，不只是跨行的：
        # `Just remember,  your trust` 里两个空格也会被折成一个，不折 md5 就错。
        raw = "\"" + re.sub(r"[ \n]+", " ", raw[1:-1]) + "\""
        tail = (m.group("tail") or "").strip()
        if tail and not re.match(
                r"^(id\s+\w+|with\s+\(?[\w\. ]+\)?|nointeract|\(|@|:)", tail):
            prev_code = s
            continue
        text = _unescape(raw[1:-1])
        # 空台词 `""` 也要收：Ren'Py 会给它生成 translate 块（digest 0ae9bcd0），
        # 缺了 --count 就报 missing。翻译阶段会跳过空白句，回填时用原文占位。
        if not who and not tail and prev_code.startswith("menu"):
            tail = ":"
        elif menu_stack and indent > menu_stack[-1][0] and menu_stack[-1][1] < 0:
            # 只有 menu 的 prompt（紧跟 `menu:` 之后、第一个 choice 之前的那句）
            # 在 AST 里是 interact=False，要带 nointeract 参与 md5。
            # choice 块内部的 say 是普通交互语句（interact=True），绝对不能带，
            # 否则 identifier 全错、生成一堆孤儿翻译块。
            tail = (tail + " nointeract").strip()
        prev_code = s
        yield i, cur_label, who, text, tail, attrs


def _iter_ui_strings(path: Path):
    """收集 screen/say 之外的界面文案：`_("...")` 和 `_p("...")`。

    Ren'Py 把这些也收进 strings 表（screens.rpy 里 Back / Save / Prefs 那些）。
    不收的话菜单栏和存档界面还是英文。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    # 直接在**整个文件**上匹配，而不是逐行：`__(` 后面换行再写字符串很常见，
    #     return characters_fc3_name + __(
    #         " has a message that troubles her. ..."
    #     )
    # `_UI_STRING_RE` 里 `\s*` 已含换行，整文件扫才不会漏。
    pos = 0
    n = len(text)
    while pos < n:
        m = _UI_STRING_RE.search(text, pos)
        if not m:
            break
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        txt = _unescape(raw or "")
        end = m.end()
        # Python / Ren'Py 都支持相邻字符串字面量自动拼接：
        #     __( "前半句"
        #         "后半句" )
        # 只取第一个字面量会让整条 strings 对不上（官方收的是拼起来的结果）。
        while True:
            m2 = _ADJ_STRING_RE.match(text, end)
            if not m2:
                break
            txt += _unescape(m2.group(1) if m2.group(1) is not None else m2.group(2))
            end = m2.end()
        pos = end
        if not txt.strip():
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        if text[line_start:m.start()].lstrip().startswith("#"):
            continue
        yield text.count("\n", 0, m.start()) + 1, "", txt


def _cjk_face_index(path: str) -> Optional[int]:
    """在 .ttc 字体集合里找简体中文（SC）那一张 face 的序号。

    为什么必须指定序号：`NotoSansCJK-Regular.ttc` 是一个装了 10 张 face 的
    字体集合，**index 0 是日文字形（JP）**。Ren'Py 的 `load_face` 默认
    index=0（renpy/text/font.py:606），于是简体中文游戏会拿到一套日文字形：
    「直/值/骨/写」这类字形差异明显，部分汉字在 JP face 里根本不存在对应
    字形，直接渲染成空白。

    Ren'Py 支持 `"2@filename.ttc"` 这种「序号 @ 文件名」写法
    （renpy/text/font.py:608 解析），返回 2 即让它取 SC face。

    探测不到（没有 pillow、不是 ttc、或集合里没有 SC）时返回 None，
    调用方退回原来的无序号写法，行为与修复前一致。
    """
    if not path.lower().endswith(".ttc"):
        return None
    try:
        from PIL import ImageFont
    except Exception:
        return None
    # 优先按 family 名精确匹配；Noto 的 SC face 一般叫
    # "Noto Sans CJK SC"，思源叫 "Source Han Sans SC"。
    want = ("Noto Sans CJK SC", "Noto Serif CJK SC",
            "Source Han Sans SC", "Source Han Serif SC",
            "WenQuanYi Micro Hei", "AR PL UMing CN")
    for i in range(32):
        try:
            ft = ImageFont.truetype(path, 12, index=i)
            fam = (ft.getname() or ("",))[0] or ""
        except Exception:
            break          # 超出 face 数量，后面没有了
        if fam in want:
            return i
    return None


def _find_cjk_font() -> tuple[Optional[str], Optional[str]]:
    cands = [
        ("Noto Sans CJK SC", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ("Source Han Sans SC", "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
        ("WenQuanYi Micro Hei", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        ("Noto Serif CJK SC", "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        ("AR PL UMing", "/usr/share/fonts/truetype/arphic/uming.ttc"),
    ]
    for name, p in cands:
        if os.path.exists(p):
            return name, p
    try:
        out = subprocess.check_output(["fc-match", "-f", "%{file}", ":lang=zh-cn"],
                                      text=True, stderr=subprocess.DEVNULL).strip()
        if out and os.path.exists(out):
            return os.path.basename(out), out
    except Exception:
        pass
    return None, None


class RenPyAdapter(EngineAdapter):
    name = "renpy"
    LANG = "chinese"

    def prepare(self, root: Path, log) -> None:
        game = root / "game"
        if not game.is_dir():
            raise RuntimeError("缺少 game/ 目录，不像是 Ren'Py 游戏")
        # 1) 解 rpa
        rpas = sorted(game.glob("*.rpa"))
        for rpa in rpas:
            if (game / (rpa.name + ".bak")).exists():
                log(f"  跳过（已解过）: {rpa.name}")
                continue
            log(f"  解包 {rpa.name} …")
            cp = subprocess.run([sys.executable, "-m", "unrpa", "-m", "-p", str(game), str(rpa)],
                                check=False, capture_output=True)
            # 解包失败绝不能改 .bak：旧代码不查 returncode 就 rename，unrpa
            # 报错（rpa 损坏 / 依赖缺失）也照样改名，下次运行被「已解过」永久
            # 跳过，解包内容静默丢失且无任何报错。
            if cp.returncode != 0:
                err = (cp.stderr or cp.stdout or b"").decode("utf-8", "replace").strip()
                log(f"  ! 解包失败 {rpa.name}（returncode={cp.returncode}）: {err[:200]}")
                continue
            rpa.rename(game / (rpa.name + ".bak"))
        # 2) 反编译 —— **只反编译没有明文 .rpy 的 rpyc**
        #
        # 很多游戏（distribute 时勾了"包含脚本源码"）的 rpa 里 .rpy 和 .rpyc
        # 是成对出现的。明文 .rpy 是作者原文，反编译结果只是重建，会丢东西：
        # unrpyc 会把
        #     screen overlay_none():
        #         pass
        # 还原成
        #     screen overlay_none()
        # （空 screen 的 pass 语句丢失），Ren'Py 直接报
        #     screen statement expects a non-empty block
        # 整个游戏打不开。所以有明文就绝不动它。
        need = []
        for p in game.rglob("*.rpyc"):
            if p.with_suffix(".rpy").exists():
                continue
            need.append(p)
        skipped = len(list(game.rglob("*.rpyc"))) - len(need)
        if skipped:
            log(f"  {skipped} 个 rpyc 已有同名明文 .rpy，保留原文不反编译")
        if need:
            log(f"  反编译 {len(need)} 个 rpyc …")
            # 逐个传文件而不是传目录：传目录时 unrpyc 会递归处理所有 rpyc，
            # 把我们想跳过的也一起 clobber 掉。
            subprocess.run([sys.executable, str(UNRPYC), "-c", "--try-harder",
                            "-p", str(min(8, os.cpu_count() or 4))] +
                           [str(p) for p in need],
                           check=False, capture_output=True)

    def extract(self, root: Path, log) -> list[dict]:
        game = root / "game"
        if not game.is_dir():
            raise RuntimeError("缺少 game/ 目录")
        items = []
        for p in sorted(game.rglob("*.rpy")):
            parts = set(p.relative_to(game).parts)
            if parts & {"tl", "cache", "saves", "__pycache__"}:
                continue
            rel = str(p.relative_to(root))
            # label 索引
            marks = []
            text = p.read_text(encoding="utf-8", errors="replace")
            for ln, line in enumerate(text.split("\n"), 1):
                m = re.match(r"^\s*label\s+([\w\.]+)", line)
                if m:
                    marks.append((ln, m.group(1)))

            def label_at(ln):
                ans = None
                for l2, name in marks:
                    if l2 <= ln:
                        ans = name
                    else:
                        break
                return ans

            for ln, lab, who, src, tail, attrs in _iter_say_lines(p):
                items.append({
                    "id": f"{rel}:{ln}",
                    "src": src,
                    "who": who or "",
                    "attrs": attrs,
                    # label 必须用解析器顺序推出来的那个（带名 menu 会开新 label），
                    # 不能用「上面最近的 label 行」——那样 menu 块内的台词全挂到外层 label。
                    "label": lab or label_at(ln) or "",
                    "file": rel,
                    "line": ln,
                    "tail": tail,
                    "kind": "string" if tail.startswith(":") else "dialogue",
                })
            # 界面文案 `_("Back")`（screens.rpy 的按钮、gui.rpy 的提示等）。
            # 与 say 行不重叠：`_()` 不会出现在对话语法位置。
            for ln, _who, src in _iter_ui_strings(p):
                items.append({
                    "id": f"{rel}:{ln}#ui",
                    "src": src,
                    "who": "",
                    "label": "",
                    "file": rel,
                    "line": ln,
                    "tail": ":",
                    "kind": "string",
                })
        if not items:
            log("  ! 没提取到文本。可能脚本被混淆，或 .rpyc 没反编译成功")
        # 引擎自带 UI 文案（renpy/common/*.rpy 里的 `_("…")`）。
        # 官方 translate 命令也会把它们收进 tl/<lang>/common.rpy；不收的话
        # --count 会永远报几百条 missing string，且游戏里的引擎提示还是英文。
        common = root / "renpy" / "common"
        if common.is_dir():
            # `.rpym` 也要扫：_errorhandling.rpym 里有 Console / Ignore / Rollback
            # 这些报错界面按钮，只扫 .rpy 会漏 22 条。
            for p in sorted(list(common.glob("*.rpy")) + list(common.glob("*.rpym"))):
                rel = str(p.relative_to(root))
                # 00console.rpy 是开发者控制台（Shift+O 才出来）的内置帮助，
                # 官方 translate 不收集、正式发行也不翻译，跟着跳过。
                if p.name == "00console.rpy":
                    continue
                for ln, _w, src in _iter_ui_strings(p):
                    items.append({
                        "id": f"{rel}:{ln}#ui",
                        "src": src,
                        "who": "",
                        "label": "",
                        "file": rel,
                        "line": ln,
                        "tail": ":",
                        "kind": "string",
                    })
        return items

    def apply(self, root: Path, items: list[dict], log) -> int:
        game = root / "game"
        tl_root = game / "tl" / self.LANG
        # 整目录重建（幂等）：tl/chinese 是生成物，每次按当前译文重写。
        # 用户在 tl/ 里手改过的内容会丢 —— 这是有意的取舍，手改请改 JSON 再回填。
        if tl_root.exists():
            shutil.rmtree(tl_root)
        tl_root.mkdir(parents=True, exist_ok=True)

        used, per_file, strings = set(), {}, []
        string_seen: set[str] = set()
        for it in items:
            dst = (it.get("dst") or "").strip()
            if it["kind"] == "string":
                # 空译文跳过（没翻成的不必写，界面回落英文可接受）；
                # 但 dst == src 要写——"Daisy"、"69"、"<" 这类专名和符号
                # 模型会原样返回，跳过就等于 missing。
                if not dst:
                    continue
                # Ren'Py 的 strings 表按**原文**全局去重：同一个 old "…"
                # 出现两次会在加载时抛 "A translation for X already exists"，
                # 整个翻译表都不生效。实测 Savior 有 26 条 menu prompt 里
                # 6 条都叫 "Sex"，直接写出去游戏就起不来。
                if it["src"] not in string_seen:
                    string_seen.add(it["src"])
                    strings.append(it)
                continue
            who = (it.get("who") or "").strip()
            # 对话块**必须**写出来（哪怕没翻成）：Ren'Py 的 --count / 游戏运行都按
            # identifier 查找，缺块就是 missing，那句台词直接不显示。没译文时用原文
            # 占位，至少游戏能正常跑完，不会出现空白对话框。
            dst = dst or it["src"]
            code = _say_code(who, it["src"], it.get("attrs") or "")
            tail = (it.get("tail") or "").strip()
            m_tail = re.match(r"^(nointeract|with\s+.+)$", tail)
            if m_tail:
                code += " " + m_tail.group(1)
            m_id = re.match(r"id\s+(\w+)", tail)
            ident = m_id.group(1) if m_id else _tlid(it.get("label"), code)
            base, n = ident, 0
            while ident in used:
                n += 1
                ident = f"{base}_{n}"
            used.add(ident)
            per_file.setdefault(it["file"], []).append((it, code, ident, dst))

        total = 0
        for rel, rows in per_file.items():
            out = tl_root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            rows.sort(key=lambda r: r[0]["line"])
            with open(out, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n# galgame-tl\n\n")
                for it, code, ident, dst in rows:
                    f.write(f"# {rel}:{it['line']}\n")
                    f.write(f"translate {self.LANG} {ident}:\n\n")
                    f.write(f"    # {code}\n")
                    who = (it.get("who") or "").strip()
                    # translate 块正文必须是完整的 say 语句，带属性的要原样带上，
                    # 否则 TranslateSay 的 attributes 丢失，说话人表情/立绘变化会失效。
                    f.write(f"    {_say_code(who, dst, it.get('attrs') or '')}\n\n")
                    total += 1
        if strings:
            out = tl_root / "zz_strings.rpy"
            with open(out, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n")
                f.write(f"translate {self.LANG} strings:\n\n")
                for it in strings:
                    f.write(f"    # {it['file']}:{it['line']}\n")
                    f.write(f'    old "{_escape(it["src"])}"\n')
                    f.write(f'    new "{_escape(it["dst"])}"\n\n')
        log(f"  写入 {total} 个对话块 + {len(strings)} 条字符串")
        return total + len(strings)

    def finalize(self, root: Path, log) -> None:
        game = root / "game"
        name, path = _find_cjk_font()
        if not path:
            log("  ! 系统没装 CJK 字体，跳过字体补丁: sudo apt install fonts-noto-cjk")
            return
        tl_root = game / "tl" / self.LANG
        tl_root.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(path)[1]
        shutil.copy2(path, tl_root / ("cjk_font" + ext))

        orig = set()
        for p in game.rglob("*.rpy"):
            if "tl" in p.relative_to(game).parts:
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # 字体名可能带空格（"Hillstown Sans.ttf"），字符类比 \w 放宽。
            # **必须同时注册「原样字符串」和 basename**：游戏里普遍写成
            # "fonts/MetalMania-Regular.ttf"，而 Ren'Py 的
            # config.font_replacement_map 按调用方传入的**原字符串**查表
            # （renpy/text/font.py:678 `t = (fn, bold, italics)`），只注册
            # basename 会整个失配 —— 实测 Betweensalvationandabyss 主菜单
            # 六个按钮全用 "fonts/MetalMania-Regular.ttf"，失配后中文没有字形，
            # 整排按钮文字变成空白，看上去就像「没有开始游戏的选项」。
            for m in re.finditer(r'"([\w/\- \.]+\.(?:ttf|otf|ttc|woff2?))"', txt):
                full = m.group(1)
                orig.add(full)
                orig.add(os.path.basename(full))

        cjk = f"tl/{self.LANG}/cjk_font{ext}"
        # .ttc 是字体集合，index 0 是日文字形；必须显式指定简体中文那一张。
        # Ren'Py 用 `"<index>@<路径>"` 传 face 序号（renpy/text/font.py:608）。
        face_idx = _cjk_face_index(path)
        if face_idx is not None:
            cjk = f"{face_idx}@{cjk}"
            log(f"  cjk 字体取第 {face_idx} 张 face（简体中文字形）")
        with open(tl_root / "zz_font_patch.rpy", "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\n")
            f.write("init 999 python:\n")
            f.write(f'    config.language = "{self.LANG}"\n')
            # **key 必须是三元组 (文件名, bold, italics)**。
            # renpy/text/font.py:674 是 `config.font_replacement_map.get((fn, bold, italics), ...)`，
            # 写成 `map["x.ttf"] = (...)` 永远命中不了，字形替换静默失效。
            # bold/italics 四种组合全注册，否则粗体标题之类会漏。
            f.write("    for _cf in (\n")
            for cf in sorted(orig):
                f.write(f"            {cf!r},\n")
            f.write("    ):\n")
            f.write("        for _b in (False, True):\n")
            f.write("            for _i in (False, True):\n")
            f.write(f"                config.font_replacement_map[(_cf, _b, _i)] = "
                    f"({cjk!r}, False, False)\n")
            f.write("\n")
            f.write(f"translate {self.LANG} python:\n")
            # gui.*font 变量要逐个改。注意 `gui.choice_button_text_font = gui.text_font`
            # 这种在 define 时就求值固化成字符串了，只改 gui.text_font 不会跟着变，
            # 结果选项按钮仍是原字体 → 中文全变方框。
            f.write("    _gtl_font = %r\n" % (cjk,))
            f.write("    for _k in list(vars(gui)):\n")
            f.write("        if 'font' in _k:\n")
            f.write("            try:\n")
            f.write("                setattr(gui, _k, _gtl_font)\n")
            f.write("            except Exception:\n")
            f.write("                pass\n")
            # 直接写字体名的 style 也要改（say_dialogue / choice_button_text 等）
            f.write("    for _sn in ('say_dialogue', 'say_label', 'namebox',\n")
            f.write("                'choice_button_text', 'button_text', 'gui_button_text',\n")
            f.write("                'gui_interface_text', 'input_text'):\n")
            f.write("        try:\n")
            f.write("            style.__dict__[_sn].font = _gtl_font\n")
            f.write("        except Exception:\n")
            f.write("            pass\n")
            f.write("    gui.text_size = int(gui.text_size * 0.95)\n")
            f.write("    gui.name_text_size = int(gui.name_text_size * 0.95)\n")
            # 中文字体常常没有 ▸ ● 这类符号（Noto Sans CJK 缺 U+25B8，
            # 而 skip_indicator 的三角正好是 '▸'），直接整块换成 cjk 会变方框。
            # FontGroup 能按字符回退到原字体：中文走 cjk，符号走 DejaVu。
            f.write("\n")
            f.write("init 1000 python:\n")
            f.write("    try:\n")
            f.write("        _gtl_group = FontGroup()\n")
            f.write("        _gtl_group.add(_gtl_font, None, None)\n")
            f.write("        _gtl_group.add('DejaVuSans.ttf', 0x2000, 0x2BFF)\n")
            f.write("        _gtl_group.add('DejaVuSans.ttf', 0x25A0, 0x25FF)\n")
            f.write("        for _k in list(vars(gui)):\n")
            f.write("            if 'font' in _k and getattr(gui, _k, None) == _gtl_font:\n")
            f.write("                setattr(gui, _k, _gtl_group)\n")
            f.write("        for _sn in ('say_dialogue', 'say_label', 'namebox',\n")
            f.write("                    'choice_button_text', 'button_text', 'gui_button_text',\n")
            f.write("                    'gui_interface_text', 'input_text'):\n")
            f.write("            _st = style.__dict__.get(_sn)\n")
            f.write("            if _st is not None and getattr(_st, 'font', None) == _gtl_font:\n")
            f.write("                _st.font = _gtl_group\n")
            f.write("    except Exception as _e:\n")
            f.write("        pass\n")
        log(f"  中文字体 {name}，替换 {len(orig)} 个原字体（含 bold/italic 变体）")


# ============================================================ RPG Maker MV/MZ
class RPGMakerAdapter(EngineAdapter):
    name = "rpgmaker"

    def _data_dir(self, root: Path) -> Path:
        for c in (root / "www" / "data", root / "data"):
            if (c / "System.json").exists():
                return c
        for p in root.rglob("System.json"):
            if p.parent.name == "data":
                return p.parent
        raise RuntimeError("找不到 data/System.json")

    def prepare(self, root: Path, log) -> None:
        ddir = self._data_dir(root)
        try:
            key = json.loads((ddir / "System.json").read_text(encoding="utf-8")).get("encryptionKey") or ""
        except Exception:
            key = ""
        if key and list(ddir.parent.rglob("*.rpgmvp"))[:1]:
            log(f"  检测到加密资源，正在解密（key={key[:4]}…）")
            self._decode(root, ddir, key[:16], log)

    @staticmethod
    def _xor(buf: bytes, key: str) -> bytes:
        kb = key.encode()
        n = len(kb)
        return bytes(b ^ kb[i % n] for i, b in enumerate(buf))

    def _decode(self, root: Path, ddir: Path, key: str, log) -> int:
        cnt = 0
        for ext, newext in ((".rpgmvp", ".png"), (".rpgmvo", ".ogg"), (".rpgmvm", ".m4a")):
            for p in ddir.parent.rglob(f"*{ext}"):
                if Path(str(p) + ".bak").exists():
                    continue
                raw = p.read_bytes()
                if len(raw) <= 32:
                    continue
                out = p.with_suffix(newext)
                out.write_bytes(self._xor(raw[16:], key))
                p.rename(Path(str(p) + ".bak"))
                cnt += 1
        if cnt:
            log(f"  解密 {cnt} 个资源文件")
        return cnt

    def source_dir(self, root: Path) -> Path:
        """提取时永远读这个目录。

        为什么不是 data/：一旦 apply 过，data/ 里就是中文了。再提取一次会把
        译文当原文送去翻第二遍（结果是「【译】早安，主人」）。所以只要
        `_bak_data/` 存在（apply 时自动建的原始快照），提取就永远从它读。
        第一次运行 `_bak_data` 还没建，就等于读 data/，行为不变。
        """
        ddir = self._data_dir(root)
        bak = ddir.parent / "_bak_data"
        return bak if bak.is_dir() else ddir

    def extract(self, root: Path, log) -> list[dict]:
        ddir = self.source_dir(root)
        if ddir.name == "_bak_data":
            log(f"  从原始快照读取（{ddir.name}），保证不重复翻译已译内容")
        items: list[dict] = []

        # ---- 事件脚本（Map / Troop / CommonEvent）
        def walk_commands(lst, prefix):
            for idx, cmd in enumerate(lst or []):
                if not isinstance(cmd, dict):
                    continue
                code = cmd.get("code")
                params = cmd.get("parameters") or []
                if code == 401 and params and isinstance(params[0], str) and params[0].strip():
                    items.append({"path": f"{prefix}/{idx}/parameters/0", "src": params[0],
                                  "kind": "dialogue", "file": fn, "who": ""})
                elif code == 102 and params and isinstance(params[0], list):
                    for j, s in enumerate(params[0]):
                        if isinstance(s, str) and s.strip():
                            items.append({"path": f"{prefix}/{idx}/parameters/0/{j}",
                                          "src": s, "kind": "string", "file": fn, "who": ""})
                elif code == 405 and params and isinstance(params[0], str) and params[0].strip():
                    items.append({"path": f"{prefix}/{idx}/parameters/0", "src": params[0],
                                  "kind": "dialogue", "file": fn, "who": ""})
                elif code == 402 and len(params) >= 2 and isinstance(params[1], str) and params[1].strip():
                    # 「当选择 X 时」分支头；文字必须与 102 的选项一致，否则菜单与分支对不上
                    items.append({"path": f"{prefix}/{idx}/parameters/1", "src": params[1],
                                  "kind": "choice", "file": fn, "who": ""})

        def rec(o, p):
            if isinstance(o, dict):
                if isinstance(o.get("list"), list):
                    walk_commands(o["list"], p + "/list")
                for k, v in o.items():
                    if k != "list":
                        rec(v, p + "/" + k)
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    rec(v, f"{p}/{i}")

        for fn in sorted(os.listdir(ddir)):
            if not fn.endswith(".json"):
                continue
            fp = ddir / fn
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if fn.startswith(("Map", "Troop")) or fn in ("CommonEvents.json",) or fn.startswith("MapInfos"):
                if isinstance(data, (dict, list)):
                    rec(data, fn)
            elif fn == "Terms.json":
                def rec2(o, p):
                    if isinstance(o, dict):
                        for k, v in o.items():
                            rec2(v, f"{p}/{k}")
                    elif isinstance(o, list):
                        for i, v in enumerate(o):
                            rec2(v, f"{p}/{i}")
                    elif isinstance(o, str) and o.strip():
                        items.append({"path": f"{fn}{p}", "src": o, "kind": "string",
                                      "file": fn, "who": ""})
                rec2(data, "")
            elif fn in ("Actors.json", "Items.json", "Weapons.json", "Armors.json",
                        "Skills.json", "States.json", "Enemies.json", "Classes.json"):
                fields = {"Actors.json": ["name", "nickname", "profile"],
                          "Items.json": ["name", "description"],
                          "Weapons.json": ["name", "description"],
                          "Armors.json": ["name", "description"],
                          "Skills.json": ["name", "description", "message1", "message2"],
                          "States.json": ["name", "message1", "message2", "message3", "message4"],
                          "Enemies.json": ["name"], "Classes.json": ["name"]}.get(fn, [])
                if isinstance(data, list):
                    for i, row in enumerate(data):
                        if not isinstance(row, dict):
                            continue
                        for f in fields:
                            v = row.get(f)
                            if isinstance(v, str) and v.strip():
                                items.append({"path": f"{fn}/{i}/{f}", "src": v,
                                              "kind": "string", "file": fn, "who": ""})
            elif fn == "System.json":
                if isinstance(data.get("gameTitle"), str) and data["gameTitle"].strip():
                    items.append({"path": "System.json/gameTitle", "src": data["gameTitle"],
                                  "kind": "string", "file": fn, "who": ""})
                terms = data.get("terms")
                if isinstance(terms, dict):
                    def rec3(o, p):
                        if isinstance(o, dict):
                            for k, v in o.items():
                                rec3(v, f"{p}/{k}")
                        elif isinstance(o, list):
                            for i, v in enumerate(o):
                                rec3(v, f"{p}/{i}")
                        elif isinstance(o, str) and o.strip():
                            items.append({"path": f"System.json/terms{p}", "src": o,
                                          "kind": "string", "file": fn, "who": ""})
                    rec3(terms, "")

        for i, it in enumerate(items):
            it["id"] = f"{it['file']}::{it['path']}"
            it["label"] = it["file"].replace(".json", "")
        log(f"  抽取 {len(items)} 条（{len({i['src'] for i in items})} 唯一）")
        return items

    @staticmethod
    def _set(obj, path: str, value):
        parts = [p for p in path.split("/") if p != ""]
        cur = obj
        for p in parts[:-1]:
            cur = cur[int(p)] if isinstance(cur, list) else cur[p]
        last = parts[-1]
        if isinstance(cur, list):
            cur[int(last)] = value
        else:
            cur[last] = value

    def apply(self, root: Path, items: list[dict], log) -> int:
        ddir = self._data_dir(root)
        # 同一句原文必须同一种译法（菜单选项与 402 分支头分开翻会错位）
        canon: dict[str, str] = {}
        for it in items:
            d = (it.get("dst") or "").strip()
            if not d:
                continue
            src = it["src"]
            if src not in canon or it.get("kind") == "string":
                canon[src] = d
        for it in items:
            d = (it.get("dst") or "").strip()
            if d and it["src"] in canon:
                it["dst"] = canon[it["src"]]

        bak = ddir.parent / "_bak_data"
        if not bak.exists():
            shutil.copytree(ddir, bak)
            log(f"  已备份 data → {bak.name}")
        else:
            # 已有原始快照：以快照为准重建 data，避免在旧译文上叠加
            shutil.rmtree(ddir)
            shutil.copytree(bak, ddir)
            log(f"  以 data → {bak.name} 为基准重建，避免叠加旧译文")
        per_file: dict[str, list] = {}
        for it in items:
            # 注意：不能对译文做 .strip()。Fungus 这类框架把「换行」也当
            # 排版信息（'Go back\n'），剥掉尾随换行会让菜单项粘连。
            # 空校验用 strip()，实际写入用原值。
            dst = it.get("dst") or ""
            if not dst.strip() or dst == it["src"]:
                continue
            fn = it["file"]
            rest = it["path"][len(fn):].lstrip("/")
            per_file.setdefault(fn, []).append((rest, dst))
        n = 0
        for fn, pairs in per_file.items():
            fp = ddir / fn
            obj = json.loads(fp.read_text(encoding="utf-8"))
            for rest, dst in pairs:
                try:
                    self._set(obj, rest, dst)
                    n += 1
                except Exception as e:
                    log(f"  ! {fn}/{rest}: {e}")
            fp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")
        log(f"  回填 {n} 条 → {len(per_file)} 个文件")
        return n

    def finalize(self, root: Path, log) -> None:
        # MV/MZ 走 nw.js；中文字体靠 CSS。给出提示即可
        css = None
        for c in (root / "www" / "css" / "game.css", root / "css" / "game.css"):
            if c.exists():
                css = c
                break
        name, path = _find_cjk_font()
        if css and path and name:
            txt = css.read_text(encoding="utf-8", errors="replace")
            if "galgame-tl-font" not in txt:
                fonts_dir = css.parent.parent / "fonts"
                fonts_dir.mkdir(parents=True, exist_ok=True)
                ext = os.path.splitext(path)[1]
                target = fonts_dir / ("galgame-tl" + ext)
                shutil.copy2(path, target)
                rel = f"../fonts/galgame-tl{ext}"
                txt += (f'\n/* galgame-tl-font */\n@font-face {{ font-family: "galgame-tl-font"; '
                        f'src: url("{rel}"); }}\n'
                        f'body, .gameText, .messageText {{ font-family: "galgame-tl-font", sans-serif !important; }}\n')
                css.write_text(txt, encoding="utf-8")
                log(f"  已注入中文字体 CSS（{name}）")
        if name is None:
            log("  ! 没找到 CJK 字体，界面中文可能显示成方块")


# ==================================================================== Unity
# TextAsset 行级过滤：判定「这一行像不像要翻的文本」
# 顺序按开销从低到高；任何一条命中就直接丢弃。
_TA_CONFIG_LINE = re.compile(
    r"^(?:[\w.\-]+\s*=\s*[^\s].*"                # key = value
    r"|[\[{<].*[\]}>]\s*$"                      # 整行是 JSON / XML / 括号块
    r"|\d+[\s,;|]+\d+.*"                          # 纯数字表
    r"|[-_=*#~]{3,}\s*$"                          # 分隔线
    r"|\"\s*:\s*[\[{]"                           # "key" : [
    r"|\"[^\"]{0,120}\"\s*,?\s*$"               # "some.value",
    # 配置键必须小写开头、不含空格：'size: 332, 294' 挡掉，
    # 但 'Alice: Good morning.' 这种「名字+冒号」的台词不能挡
    r"|[a-z_][\w.\-]{0,30}:\s*[\w\d\-., ]+$"       # size: 332, 294 / format: RGBA8888
    r"|[a-z_][\w.\-]{0,30}:\s*(?:true|false|-?\d+)\s*$"  # rotate: true
    r")"                                        # 闭合 ^(?: ... )
)
# 文件名 / 资源路径：'BJ park - edit 6.png'
_TA_ASSET_LINE = re.compile(
    r"\.(?:png|jpe?g|gif|bmp|psd|tga|wav|ogg|mp3|mp4|anim|controller|prefab|mat|asset|"
    r"shader|cs|lua|json|xml|txt|bytes|dll|so|meta|unity|fbx)(?:\s|$)",
    re.I)
# 至少要有一个「像单词」的片段：2 个字母以上的英文词，或任意中日文字符
_TA_WORD_RX = re.compile(
    r"[A-Za-z][A-Za-z'\u2019\-]{1,}|[\u3040-\u30ff\u3400-\u9fff]")


def _ta_line_ok(t: str) -> bool:
    """TextAsset 的一行要不要翻。t 是 strip 过的行。"""
    if len(t) < 2 or _TA_CONFIG_LINE.match(t) or _TA_ASSET_LINE.search(t):
        return False
    if not _TA_WORD_RX.search(t):
        return False
    # 单个词（无空格、无换行、无 CJK）：'blush' / 'rotate' 这类字段名
    has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", t))
    if not has_cjk and " " not in t:
        return False
    # 符号占比过高：断行表那种一整串标点
    meaningful = sum(1 for c in t if c.isalnum() or c.isspace() or c in "'’\"-,.!?;:")
    if meaningful / len(t) < 0.6:
        return False
    return True


class UnityAdapter(EngineAdapter):
    """Unity 游戏的文本提取与回填。

    两条通路
    --------
    1. **TextAsset**：文本以纯文本资源存在（.txt / .json / .csv / .yarn）。
       直接读 `m_Script`，按行定位。
    2. **MonoBehaviour 裸字节**：对话框架（Fungus / Yarn / 自研）把文本存
       在组件的字段里，且没有 TypeTree。用 `unity_mb` 扫
       `[int32 长度][UTF-8]` 序列定位，记 (文件, path_id, 字段偏移)。

    为什么后者是主力
    ----------------
    实测 1.2G 的 Unity 游戏（Fungus 框架）：TextAsset 只有 11 个且全是
    Lua 脚本 / 字体表；真正的 3139 段对话全在 MonoBehaviour 里。只做
    TextAsset 等于什么都没翻。

    幂等性
    ------
    回填前把整个文件备份成 `<名字>.orig`（只备份一次）。重复跑时：
    * 提取阶段——若 `.orig` 存在，**从备份读**，保证提取的永远是原文；
    * 回填阶段——从 `.orig` 重新载入再改，避免多次运行叠加。
    """

    name = "unity"

    # 扫描范围。level* 是场景（对话多在这儿），*.assets 是共享资源
    # （prefab 上的文字多见于此）。StreamingAssets 里的 .bundle 也收。
    _PATTERNS = ("*_Data/level*", "*_Data/*.assets", "*_Data/*.unity3d",
                 "*_Data/StreamingAssets/*.bundle", "*.bundle", "*.unity3d")

    def _targets(self, root: Path) -> list[Path]:
        seen: set[str] = set()
        out: list[Path] = []
        for pat in self._PATTERNS:
            for f in sorted(root.glob(pat)):
                if not f.is_file() or f.stat().st_size < 1024:
                    continue
                # .resS / .resource 是纯数据流，没有对象表，跳过
                if f.suffix in (".resS", ".resource", ".manifest"):
                    continue
                # .orig 是我们自己的原文快照，绝不能当扫描目标 ——
                # 否则第二次跑会把快照和正本各扫一遍，条目直接翻倍
                if f.name.endswith(".orig"):
                    continue
                rp = str(f.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                out.append(f)
        return out

    def _source(self, p: Path) -> Path:
        """提取/回填都从 .orig 快照读，保证反复跑不叠加。"""
        bak = p.with_suffix(p.suffix + ".orig")
        return bak if bak.exists() else p

    def _snapshot(self, p: Path, log) -> None:
        bak = p.with_suffix(p.suffix + ".orig")
        if not bak.exists():
            try:
                shutil.copy2(p, bak)
            except Exception as e:
                log(f"  ! 备份 {p.name} 失败: {e}")

    # ---------------------------------------------------------- 提取
    def extract(self, root: Path, log) -> list[dict]:
        try:
            import UnityPy
        except ImportError:
            raise RuntimeError("缺少 UnityPy：uv pip install UnityPy")
        from . import unity_mb

        items: list[dict] = []
        files = self._targets(root)
        log(f"  扫描 {len(files)} 个资源文件…")

        n_ta = n_mb = 0
        for p in files:
            src_path = self._source(p)
            try:
                env = UnityPy.load(str(src_path))
            except Exception as e:
                log(f"  ! 载入 {p.name} 失败: {e}")
                continue
            rel = str(p.relative_to(root))

            for obj in env.objects:
                try:
                    raw = obj.get_raw_data()
                except Exception:
                    continue
                if not raw:
                    continue

                if obj.type.name == "TextAsset":
                    got = self._extract_text_asset(obj, raw, rel)
                    if got:
                        items.extend(got)
                        n_ta += 1
                    continue

                if obj.type.name != "MonoBehaviour":
                    continue

                # 剥掉 MonoScript 头的 12 字节再扫，避免把 m_Script 的
                # GUID 当文本。头上的 m_GameObject / m_Enabled 定长，
                # 但不同版本长度不同，所以直接整段扫 —— 长度校验会兜住。
                fields = unity_mb.scan(raw)
                for f in fields:
                    items.append({
                        "id": f"{rel}::mb:{obj.path_id}:{f.offset}",
                        "src": f.text, "who": "", "label": f"MonoBehaviour",
                        "file": rel, "kind": "mb",
                        "mb_path_id": obj.path_id, "mb_offset": f.offset,
                        "mb_len": f.length,
                    })
                if fields:
                    n_mb += 1

        log(f"  TextAsset {n_ta} 个 / MonoBehaviour {n_mb} 个 → {len(items)} 条文本")
        if not items:
            log("  提示：可能是 IL2CPP 且文本在全局元数据里，静态改不动；"
                "这类需要 BepInEx + XUnity 的运行时方案")
        return items

    @staticmethod
    def _extract_text_asset(obj, raw: bytes, rel: str) -> list[dict]:
        """TextAsset 按行提取。只收像对话的行，跳过脚本/配置。"""
        try:
            d = obj.read()
            name = getattr(d, "m_Name", "") or ""
            script = getattr(d, "m_Script", None)
        except Exception:
            return []
        if script is None:
            return []
        if isinstance(script, str):
            text = script
        else:
            text = None
            for enc in ("utf-8", "utf-16-le", "shift_jis", "cp932", "euc-jp"):
                try:
                    text = bytes(script).decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                return []

        # 脚本类 TextAsset（.lua/.cs/.js）整篇跳过：里面没有要翻的台词，
        # 逐行扫会误抓注释和字符串字面量
        head = text[:400]
        if re.search(r"^\s*(?:--|//|local\s+\w+\s*=|function\s|def\s|#include)",
                     head, re.M):
            return []
        # 整篇是合法 JSON 的（配置 / 版本清单 / 序列化数据）整块跳过，
        # 逐行筛会把每个字符串值都当台词
        stripped = text.strip()
        if stripped[:1] in ("{", "[") or '":' in head or '":' in stripped[:200]:
            try:
                json.loads(stripped)
                return []
            except Exception:
                pass

        lines = text.split("\n")
        hits: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            s = line.rstrip("\r")
            if s.strip() and _ta_line_ok(s.strip()):
                hits.append((i, s))

        # 整篇比例闸：技术性 TextAsset（Lua / atlas 元数据 / 类型清单）
        # 里能"碰巧"像台词的短行很少，比例很低就整篇丢弃。
        # 0.15 是实测阈值：真剧情文件命中率都在 0.5 以上。
        if not hits or len(hits) / max(1, len(lines)) < 0.15:
            return []

        out: list[dict] = []
        for i, s in hits:
            out.append({
                "id": f"{rel}::{name}::{i}", "src": s, "who": "",
                "label": name, "file": rel, "kind": "ta",
                "ta_name": name, "line": i,
            })
        return out

    # ---------------------------------------------------------- 回填
    def apply(self, root: Path, items: list[dict], log) -> int:
        try:
            import UnityPy
        except ImportError:
            raise RuntimeError("缺少 UnityPy")
        from . import unity_mb

        # 按 (文件, path_id) 聚合，每个对象内按偏移降序应用
        by_obj: dict[tuple[str, int], list[tuple[int, int, str, str]]] = {}
        by_ta: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for it in items:
            # 注意：不能对译文做 .strip()。Fungus 这类框架把「换行」也当
            # 排版信息（'Go back\n'），剥掉尾随换行会让菜单项粘连。
            # 空校验用 strip()，实际写入用原值。
            dst = it.get("dst") or ""
            if not dst.strip() or dst == it["src"]:
                continue
            kind = it.get("kind")
            if kind == "ta":
                by_ta.setdefault((it["file"], it["ta_name"]), []).append(
                    (it["line"], dst))
            elif kind == "mb":
                by_obj.setdefault((it["file"], it["mb_path_id"]), []).append(
                    (it["mb_offset"], it["mb_len"], it["src"], dst))
        if not by_obj and not by_ta:
            log("  没有需要回填的条目")
            return 0

        files = sorted({k[0] for k in by_obj} | {k[0] for k in by_ta})
        total = 0
        for rel in files:
            p = root / rel
            if not p.exists():
                log(f"  ! 文件不见了: {rel}")
                continue
            self._snapshot(p, log)
            src_path = self._source(p)     # 从快照重建，避免叠加
            try:
                env = UnityPy.load(str(src_path))
            except Exception as e:
                log(f"  ! 载入 {rel} 失败: {e}")
                continue

            changed = 0
            for obj in env.objects:
                tname = obj.type.name
                if tname == "MonoBehaviour":
                    edits = by_obj.get((rel, obj.path_id))
                    if not edits:
                        continue
                    try:
                        raw = obj.get_raw_data()
                    except Exception:
                        continue
                    newraw, n = unity_mb.patch(raw, edits)
                    if n:
                        obj.set_raw_data(newraw)
                        changed += n
                elif tname == "TextAsset":
                    try:
                        d = obj.read()
                        name = getattr(d, "m_Name", "") or ""
                    except Exception:
                        continue
                    edits = by_ta.get((rel, name))
                    if not edits:
                        continue
                    lines = str(getattr(d, "m_Script", "")).split("\n")
                    hit = 0
                    for ln, dst in edits:
                        if 0 <= ln < len(lines):
                            cr = "\r" if lines[ln].endswith("\r") else ""
                            lines[ln] = dst + cr
                            hit += 1
                    if hit:
                        try:
                            d.m_Script = "\n".join(lines)
                            d.save()
                            changed += hit
                        except Exception as e:
                            log(f"  ! 写回 TextAsset {name} 失败: {e}")

            if not changed:
                continue
            try:
                p.write_bytes(env.file.save())
                total += changed
            except Exception as e:
                log(f"  ! 保存 {rel} 失败，已从快照回滚: {e}")
                try:
                    shutil.copy2(src_path, p)
                except Exception:
                    pass

        log(f"  回填 {total} 处")
        return total

    def prepare(self, root: Path, log) -> None:
        """提前给所有目标文件建 .orig 快照。

        apply 里也会补建，但那是「改到哪个文件才备份哪个」；如果玩家在
        翻译跑完前就点了取消、或者中途某个文件写坏，提前备份能保证任何
        时刻都有原文可回滚。快照只建一次（已存在就跳过），所以反复跑不叠加。
        """
        files = self._targets(root)
        made = 0
        for p in files:
            bak = p.with_suffix(p.suffix + ".orig")
            if bak.exists():
                continue
            try:
                shutil.copy2(p, bak)
                made += 1
            except Exception as e:
                log(f"  ! 备份 {p.name} 失败: {e}")
        if made:
            log(f"  已备份 {made} 个资源文件到 *.orig（原文快照，回滚用）")

    def finalize(self, root: Path, log) -> None:
        """Unity 的字体由游戏自己带（TextMeshPro 字体资源），
        换字体要重打包 SDF，静态改造做不到也不该做。
        这里只做清理：把没人用的 .orig 快照删掉会丢原文，所以保留。"""
        n = len(list(root.glob("*_Data/*.orig")))
        if n:
            log(f"  保留 {n} 个 .orig 原文快照（回滚用，别删）")


ADAPTERS: dict[str, EngineAdapter] = {
    "renpy": RenPyAdapter(),
    "rpgmaker": RPGMakerAdapter(),
    "unity": UnityAdapter(),
}


def get_adapter(engine: str, variant: str = "") -> Optional[EngineAdapter]:
    """按引擎名取适配器；rpgmaker 再按 variant 区分 MV/MZ 与 VX Ace。"""
    v = (variant or "").replace(" ", "").upper()
    if engine == "rpgmaker" and v in ("VXACE", "VX", "XP", "RGSS"):
        return ADAPTERS["rpgmaker_vxace"]
    return ADAPTERS.get(engine)


# ================================================================ RPG Maker VX Ace
class VXAceAdapter(EngineAdapter):
    """RPG Maker VX Ace / VX / XP（RGSS 系列）。

    与 MV/MZ 的区别：
    - 文本在 `Data/*.rvdata2`（Ruby Marshal 字节流），不是 JSON
    - 资源打包在 `Game.rgss3a`，要用 RGSSAD 算法解（v3 = *9+3 得 magic）
    - 事件指令参数是 RubyString，写回时必须保留 `ivars={"E": True}`（UTF-8 标记）
    """

    name = "rpgmaker"

    def can_handle(self, det) -> bool:
        return det.engine == "rpgmaker" and (det.variant or "").replace(" ", "").upper() in (
            "VXACE", "VX", "XP", "RGSS")

    # ---------------------------------------------------------------- 定位
    def _data_dir(self, root: Path) -> Path:
        """找 Data/*.rvdata2 所在目录。

        注意旧版本会解到 `Data/Data/`（归档条目名自带 `Data/` 前缀，又拼了
        一次），所以这里顺带认一下那层多余的嵌套，老产物不用重解。
        """
        cands = [root / "Data", root / "data", root / "Data" / "Data"]
        for c in cands:
            if (c / "System.rvdata2").exists() or (c / "Scripts.rvdata2").exists():
                return c
        for p in sorted(root.rglob("*.rvdata2")):
            return p.parent
        raise RuntimeError("找不到 Data/*.rvdata2（可能还没解包）")

    def source_dir(self, root: Path) -> Path:
        """提取永远读原始快照，避免把译文当原文再翻一遍（同 MV/MZ）。"""
        ddir = self._data_dir(root)
        bak = ddir.parent / "_bak_data"
        return bak if bak.is_dir() else ddir

    def _archive(self, root: Path) -> Path | None:
        from . import vxace
        return vxace.find_archive(root)

    def _target(self, root: Path) -> Path:
        ddir = self._data_dir(root)
        return ddir

    # ---------------------------------------------------------------- 解包
    def prepare(self, root: Path, log) -> None:
        from . import vxace

        arc = self._archive(root)
        if arc:
            # 归档条目名自带顶层目录（`Data/Actors.rvdata2`、`Graphics/...`），
            # 所以必须解到**游戏根目录**，不能解到 Data/ 里 —— 否则会变成
            # Data/Data/Actors.rvdata2，游戏照样读归档里的英文原文。
            n = vxace.unpack(arc, root)
            log(f"  解包 {arc.name} → 游戏根目录（新增 {n} 个，已存在的跳过）")
        else:
            log("  已有 Data/*.rvdata2，跳过解包")

    # ---------------------------------------------------------------- 提取
    def extract(self, root: Path, log) -> list[dict]:
        from vendor.rvdata.marshal_rb import load
        from . import vxace

        ddir = self.source_dir(root)
        if ddir.name == "_bak_data":
            log("  从原始快照读取（_bak_data），保证不重复翻译已译内容")

        maps = {}
        for p in sorted(ddir.glob("*.rvdata2")):
            name = p.stem
            if name in ("Scripts",):
                continue
            try:
                maps[name] = load(p.read_bytes())
            except Exception as e:
                log(f"  ! 读 {p.name} 失败: {e}")
        if not maps:
            return []

        flat = vxace.collect(maps)
        log(f"  读到 {len(maps)} 个数据文件，{len(flat)} 条待翻文本")

        items = []
        skipped = 0
        for loc, src in flat:
            # 脚本标签和 RGSS 代码绝不能送去翻译：实测谷歌会把 `<hide eval>`
            # 翻成 `<隐藏评估>`、把 `$game_party.in_battle and ...` 里的 and
            # 翻成「和」，游戏逻辑当场废掉。这类原地不动。
            if vxace.is_script_text(src):
                skipped += 1
                continue
            kind = "choice" if loc[0] == "opt" else (
                "name" if loc[0] == "db" and loc[3] in ("@name", "@nickname") else "dialogue")
            items.append({
                "path": "|".join(str(x) for x in loc),
                "src": src,
                "dst": "",
                "kind": kind,
                "who": "",
                "file": f"Data/{loc[1]}.rvdata2",
                "loc": list(loc),
            })
        if skipped:
            log(f"  跳过 {skipped} 条脚本标签/RGSS 代码（这些不该翻，翻了会坏逻辑）")
        return items

    # ---------------------------------------------------------------- 回填
    def apply(self, root: Path, items: list[dict], log) -> int:
        from vendor.rvdata.marshal_rb import load, dump
        from . import vxace
        import shutil

        tar = self._target(root)
        bak = tar.parent / "_bak_data"

        # 首次 apply：先把原始数据整份备份，之后每次都从它重建
        if not bak.is_dir():
            shutil.copytree(tar, bak)
            log(f"  已备份原始数据 → {bak.name}/（{len(list(bak.glob('*.rvdata2')))} 个）")

        # 同句同译（菜单选项与正文分开翻会不一致）
        canon: dict[str, str] = {}
        for it in items:
            d = (it.get("dst") or "").strip()
            if d:
                canon.setdefault(it["src"], d)

        byfile: dict[str, dict] = {}
        for it in items:
            loc = it.get("loc")
            dst = (it.get("dst") or "").strip() or canon.get(it["src"], "")
            if not loc or not dst:
                continue
            byfile.setdefault(loc[1], {})[tuple(loc)] = dst

        total = 0
        for fname, done in byfile.items():
            src_p = bak / f"{fname}.rvdata2"
            if not src_p.exists():
                log(f"  ! 缺原始 {fname}.rvdata2，跳过")
                continue
            try:
                obj = load(src_p.read_bytes())
            except Exception as e:
                log(f"  ! 读原始 {fname} 失败: {e}")
                continue
            n = vxace.apply_all({fname: obj}, done)
            if n:
                out = dump(obj)
                (tar / f"{fname}.rvdata2").write_bytes(out)
                total += n
        log(f"  回填 {total} 条（已写回 Data/*.rvdata2）")

        # 关键一步：RGSS3 **优先读 Game.rgss3a**，磁盘上的明文 Data/ 只是
        # 解包产物。不重打包归档，游戏里看到的还是英文原文（这一坑踩过：
        # 回填 8861 条全成功，游戏里一个字都没变）。
        arc = self._archive(root)
        if arc:
            bak_arc = arc.with_suffix(arc.suffix + ".orig")
            if not bak_arc.exists():
                shutil.copy2(arc, bak_arc)
                log(f"  已备份原归档 → {bak_arc.name}")
            ok, msg = vxace.repack(arc, root, out=arc.with_suffix(arc.suffix + ".new"),
                                   log=log)
            if ok:
                os.replace(arc.with_suffix(arc.suffix + ".new"), arc)
                log(f"  已重打包归档 {arc.name}：{msg}")
            else:
                log(f"  ! 重打包归档失败：{msg}（游戏会读到英文原文）")
        return total

    def finalize(self, root: Path, log) -> None:
        log("  VX Ace：文本已写回 Data/*.rvdata2 并重打包 Game.rgss3a，直接进游戏即可")


ADAPTERS["rpgmaker_vxace"] = VXAceAdapter()
