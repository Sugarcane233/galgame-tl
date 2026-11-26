"""Unity MonoBehaviour 里的裸字符串提取与回填。

为什么需要这一层
----------------
Unity 游戏把对话放哪儿，取决于它用了什么框架：

* 纯 TextAsset（`.txt` / `.json` / `.csv`）—— 好办，直接读字符串。
* 对话框架（Fungus / Yarn / Ink / Dialogue System）—— 文本存在
  **MonoBehaviour 的字段里**，序列化成一段裸字节。

第二种占绝大多数。这些 MonoBehaviour 没有 TypeTree（Unity 打包时把
类型信息剥掉了），UnityPy 的 `read_typetree()` 会直接报
`Expected to read N bytes, but only read 32 bytes` —— 不知道字段布局就
没法按字段读。所以这里不走 TypeTree，而是**直接扫裸字节里的 string**。

Unity 的序列化 string 格式极简：
    [int32 长度(LE)][UTF-8 字节，不补 \0]
所以只要在字节流里找「长度字段 + 合法 UTF-8 + 像人话」的片段，就拿到了
文本，且 `(偏移, 长度)` 足以精确定位 —— 回填时按偏移原地替换即可。

判断「像人话」是这套方案唯一的启发式，规则全部集中在 `looks_like_text`：
排除 Unity/System 的类名、dll 名、URL、占位模板（`{xxx}` / `$xxx`）、
纯标识符串；要求字母占比够高、有空格或换行、至少两个词。
实测在 1.2G 的 Unity 游戏（Fungus 框架）上 2956 个字段、8697 行、
22.8 万字符，抽查无语义噪音。

回填的两条硬约束
----------------
1. **偏移会漂移**：改了前面一个字段的长度，后面字段的偏移全变。所以
   同一对象里必须按偏移**从后往前**改。
2. **必须校验再改**：文件可能被别的工具动过。替换前核对
   「该偏移前面的 int32 == 原长度」且「原内容 == 提取时记的 src」，
   任一条不符就跳过并记账 —— 宁可少翻一句，也不能把二进制写烂。
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

# 一条 MonoBehaviour 的字段长度上限。对话框架存的是整章剧本，
# 实测最大 8KB 左右；给到 200KB 会开始把纹理数据当文本扫。
MAX_FIELD = 200_000

# 短于一行的不翻：'OK' 'Yes' 这种在 UI 上出现，翻了反而破坏热键提示
MIN_CHARS = 4

# 技术字符串特征。命中即排除 —— 这些是从 Resources / Shader / 字体
# 元数据里扫出来的，翻了对游戏毫无影响，只会污染术语表。
_TECH = re.compile(
    rb"^(?:UnityEngine|UnityEditor|System|Microsoft|Mono|TMPro|Newtonsoft|"
    rb"Fungus|DG\.Tweening|Spine|Facebook|Google|Firebase)\b"
)

# 看起来像命名空间 / 全限定类型名的：Word.Word.Word
_TYPENAME = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*){1,}$")

# 文件路径 / 资源名 / URL
#
# 判斜杠前必须先剥富文本标记：`{i}fixed{/i}`、`{color=yellow}…{/color}`、
# `</b>` 里的 `/` 不是路径分隔符。少了这一步，一句 734 字符的整段对话
# （十几句台词）会因为一个斜体闭合标记被整块判成「资源路径」丢掉 ——
# 实测 Together Again 有 10 处这种，后果是玩家看到后面大段大段还是英文。
_PATHY = re.compile(r"\.(?:dll|asset|mat|prefab|png|jpg|jpeg|gif|wav|ogg|mp3|"
                    r"shader|cs|lua|txt|json|xml|bytes|so|exe)$", re.I)

# 富文本标记：Fungus / TextMeshPro 都吃 `{i}` `{b}` `{color=..}` 这一套，
# Unity 自带的富文本是 `<b>` `</b>`。
_RICHTEXT = re.compile(r"\{[^{}]{0,40}\}|</?[a-zA-Z][a-zA-Z0-9]{0,12}(?:=[^<>]{0,40})?>")


def _strip_richtext(s: str) -> str:
    """去掉富文本标记，只留正文。判路径 / 判符号占比之前都要先过一遍。"""
    return _RICHTEXT.sub("", s)


def _looks_like_path(s: str) -> bool:
    """是不是文件路径 / URL，而不是「含斜杠的台词」。

    不能简单地「含 `/` 就算路径」：选项文本 `Cruel/Kind`、
    `Easy/Medium chance` 遍地都是，一刀切会把整句丢掉。
    真正的路径至少满足一条：
      * 以 / 或 \\ 开头（绝对路径）、或含协议头；
      * 以已知资源扩展名结尾；
      * 斜杠分隔的段里有一段带扩展名（`assets/Resources/icon.png`）。
    """
    if "://" in s:
        return True
    if s.lstrip().startswith(("/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return True
    # 字面量转义 `\n` `\t`（Fungus 常见）不是路径
    if "\\" in s and not re.search(r"[A-Za-z0-9_]\\[A-Za-z0-9_]", s.replace("\\n", "").replace("\\t", "")):
        if not re.search(r"\\\\|\\[A-Za-z0-9_]+\.[A-Za-z]{2,4}\b", s):
            return False
    if _PATHY.search(s):
        return True
    for part in re.split(r"[/\\]", s):
        if _PATHY.search(part.strip()):
            return True
    return False

# 纯标识符：没有空格、没有标点，且是驼峰/下划线风格
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 占位符 / 编辑器默认值：对话框架的模板串，游戏运行时会被替换掉，
# 或者根本不会显示。翻了纯属浪费配额，还会把「New Text」写进游戏。
_PLACEHOLDERS = {
    "New Text", "New Block", "Character Name", "Story text",
    "Enter text here", "Insert text", "Text", "Empty",
    "New Story", "New Say", "New Menu", "New Choice", "New Item",
}

def _repeaty(s: str) -> bool:
    r"""整串是不是同一小段反复重复（Fungus 的示例文本）。

    典型值：'Story text Story text Story text'，重复段之间甚至可能不带空格。
    用正则反向引用做这件事不行 —— `^(\w[\w ]{1,20}?)\s*\1{2,}$` 这类
    写法会因为可选空格的分支回溯而匹配不上真值（实测返回 None）。
    改成按词判定：归一化空白后切成词，看词序列是不是周期性的。

    只排除「词组（≥2 词）重复 3 次以上」或「单词重复 4 次以上」的情况，
    免得把 'Yes! Yes! Yes!' 这种真台词也误杀。
    """
    t = s.strip()
    if len(t) < 12:
        return False
    words = t.split()
    n = len(words)
    if n < 3:
        return False
    for p in range(1, n // 2 + 1):
        if any(words[i] != words[i % p] for i in range(n)):
            continue
        reps = n // p
        if reps >= 3 and (p >= 2 or reps >= 4):
            return True

    # 词级判定抓不到「两次重复被拼接、中间空格丢了」的畸形值，比如
    # 'Story text Story text Story textStory text Story text'。
    # 去掉空白后按字符级周期性再判一次。
    flat = "".join(t.split())
    m = len(flat)
    if m >= 18:
        for q in range(2, m // 3 + 1):
            if m % q == 0 and flat[:q] * (m // q) == flat:
                return True
    return False

# 占位模板：'{playerName}' / '{$herName} - Timid' / '%s'。
# 带 {$var} 的其实是真文本（游戏会替换变量），所以不能一概排除 ——
# 只有「整串就是一个占位符」才排除。
_ONLY_PLACEHOLDER = re.compile(r"^[\s\W]*[{$%][\w.:]+[}\s\W]*$")


@dataclass
class TextField:
    """MonoBehaviour 裸字节里的一个字符串字段。"""
    offset: int    # 长度字段的偏移
    length: int    # 字节长度
    text: str


def looks_like_text(s: str) -> bool:
    """这条字符串值不值得翻译。"""
    if len(s) < MIN_CHARS or len(s) > 60_000:
        return False
    if _TECH.match(s.encode("utf-8", "ignore")):
        return False
    # 判路径前先剥富文本：`{i}fixed{/i}` 里的斜杠不是路径分隔符
    plain = _strip_richtext(s)
    if _TYPENAME.match(s) or _looks_like_path(plain):
        return False
    if _ONLY_PLACEHOLDER.match(s):
        return False
    if s.strip() in _PLACEHOLDERS:
        return False
    if _repeaty(s):
        return False
    # 单行且是纯标识符：PlayerHealth / m_Script / kMaxSpeed
    if "\n" not in s and _IDENT.match(s):
        return False

    # 含 NUL / 控制字符的一定是二进制残留：真文本里不会有。
    # 少了这条，「跨字段误读」（把上一个字段的尾巴当长度）会被当成文本
    # —— 实测就抓到过 '\x00\x00\x00\x00\n\x00\x00\x00endingSave'。
    if any(c == "\x00" or (ord(c) < 32 and c not in "\n\r\t") for c in s):
        return False

    # 含 CJK 的直接算文本：日文/中文原文本来就没有空格，
    # 用「至少两个词」的规则会把整片对话漏掉。
    has_cjk = any(
        "\u3040" <= c <= "\u30ff"      # 平假名 / 片假名
        or "\u3400" <= c <= "\u9fff"   # CJK 统一汉字
        or "\udc00" <= c <= "\udc7f"   # 兼容区
        or "\uff66" <= c <= "\uff9f"   # 半角片假名
        for c in s
    )
    cjk_cnt = sum(
        1 for c in s
        if "\u3040" <= c <= "\u30ff" or "\u3400" <= c <= "\u9fff"
    )
    if has_cjk and cjk_cnt >= 1:
        # 只要有一个中日文字符就算文本。
        # 不能要求占比或字数：译文常是「【译】Day: 0」这种中英混排，
        # 里面只有 1 个汉字，按比例/按个数都会把刚写进去的译文再判成
        # 「不是文本」，复检时整片扫不出来 —— 实测踩过。
        return True

    # 符号占比：按**字母占非空白字符的比例**判。
    # 不能直接除以 len(s)：台词里大量省略号、引号、换行 ——
    # `'I'm a mess.....'` 字母只占 46.7%，会被 0.5 阈值挡掉（实测漏了这句）。
    # 也不能要求「至少两个词」：`'What?'` / `'Okay.'` / `'Thanks.'` 这种
    # 单句台词在对话里遍地都是。
    dense = re.sub(r"\s", "", plain)
    if not dense:
        return False
    letters = sum(c.isalpha() for c in dense)
    if letters / len(dense) < 0.5:
        return False            # 符号、数字为主：数字串 / 二进制残留
    # 至少要有一个真词（2 个以上字母，或含撇号的名字如 I'm / don't）
    if not re.search(r"[A-Za-z]{2,}|[A-Za-z]'[A-Za-z]", plain):
        return False
    return True


def scan(raw: bytes) -> list[TextField]:
    """扫出一段 MonoBehaviour 裸字节里所有像样的字符串字段。

    从前往后扫，命中一个就整体跳过它的内容（避免把 UTF-8 正文里的
    4 字节序列误判成新的长度字段）。
    """
    out: list[TextField] = []
    i, n = 0, len(raw)
    while i + 4 <= n:
        (ln,) = struct.unpack_from("<i", raw, i)
        if MIN_CHARS <= ln <= MAX_FIELD and i + 4 + ln <= n:
            try:
                s = raw[i + 4:i + 4 + ln].decode("utf-8")
            except UnicodeDecodeError:
                i += 1
                continue
            if looks_like_text(s):
                out.append(TextField(i, ln, s))
                i += 4 + ln
                continue
        i += 1
    return out


def patch(raw: bytes, edits: list[tuple[int, int, str, str]]) -> tuple[bytes, int]:
    """把 edits 应用到 raw，返回 (新字节, 实际替换数)。

    `edits` 每项是 `(offset, 原字节长度, 原文本, 新文本)`。
    内部按 offset 降序处理 —— 改后面的字段不会让前面的偏移失效。
    校验不通过的条目跳过（文件被改过 / 提取后内容变了），
    不抛异常：一句没翻上比写坏文件好。
    """
    orig_len = len(raw)
    ok = 0
    for offset, length, old, new in sorted(edits, key=lambda e: e[0], reverse=True):
        if offset < 0 or offset + 4 + length > len(raw):
            continue
        (cur,) = struct.unpack_from("<i", raw, offset)
        if cur != length:
            continue
        if raw[offset + 4:offset + 4 + length] != old.encode("utf-8"):
            continue
        nb = new.encode("utf-8")
        raw = raw[:offset] + struct.pack("<i", len(nb)) + nb + raw[offset + 4 + length:]
        ok += 1
    if not ok:
        return raw, 0
    # 缩短之后必须把对象补齐回原长度，否则游戏直接判定文件损坏。
    #
    # 实测（Together Again level0 / pid=40，一条 187 字节的免责声明缩到
    # 170 字节）：Unity 启动报
    #   `The file '.../level0' is corrupted! [Position out of bounds!]`
    # 把同一份字节原样补 0 回 316 字节（对象原长）之后，游戏正常加载。
    #
    # 原因：UnityPy 的 SerializedFile.save() 会按对象重排数据区，把每个
    # 对象的长度写进对象表；但 Unity 运行时还会用**打包时的原始对齐**校验
    # 对象范围。变长（尤其变短）会让后续对象的落点整体前移，越过文件尾。
    # 变长（170→187 那种 +1 字节）实测不报错，但缩短一律炸——所以统一补齐。
    #
    # 补 0 是安全的：多出来的尾部字节落在字符串数据之后，Unity 读字段时
    # 按长度前缀取值，不会读到它们。
    if len(raw) < orig_len:
        raw = raw + b"\x00" * (orig_len - len(raw))
    return raw, ok
