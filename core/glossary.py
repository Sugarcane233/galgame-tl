# -*- coding: utf-8 -*-
"""术语表自动生成。

要解决的具体问题：同一个角色名在不同批次里被翻成「艾蕾娜 / 埃琳娜 / 艾莲娜」，
同一个招式名这回叫「斩击」下回叫「切裂」——读者一眼看出是机翻。术语表把这些词
钉死，配合 llm.mask_glossary 的占位符替换（翻译时先把术语换成 ⟨G0⟩，翻完再换回
固定译名），全篇只会出现一种写法。

两条互补的路子，缺一不可：

1. **静态扫描**（零成本、最可靠）
   Ren'Py 的 `define e = Character("Eileen")` 是作者的权威定义，直接抄。
   但只覆盖「有 Character 定义的人名」，地名、组织名、招式名它没有。

2. **语料统计 + 模型确认**
   从台词统计片假名串、带敬称的汉字姓名、拉丁名，按频次排。
   高频且不是常见词的，大概率是专有名词。把候选连同样本台词一起交给模型，
   让它挑出真名词并给统一译名。
   只看统计会瞎猜（模型爱把 レベル 也叫术语），所以必须让它**从候选里挑**，
   而不是自由发挥，这样漏收和误收都可控。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------- 候选识别

# 片假名串（含长音符）。2 个字符起，日文外来语/人名的主要形态。
# **必须在敬称前断开**：`リリスちゃん` 整串会被贪婪匹配成一个词，导致真正的
# 人名「リリス」反而抓不到。这里用先行断言在敬称/助词处截断。
KANA_RE = re.compile(
    r"[ァ-ヺヽヾー]{2,}?"
    r"(?=さん|ちゃん|君|くん|様|さま|殿|先生|先輩|先辈|っち"
    r"|は|が|を|に|で|と|も|の|へ|や|か|ね|よ|な|だ|です|ます|$|[。、！？…\s「」『』（）])"
)
# 片假名人名 + 敬称：「リリスちゃん」「エレナさん」这类是明确的人名信号，
# 出现 2 次就够，不该被 min_freq=3 卡掉。
KANA_NAME_RE = re.compile(
    r"([ァ-ヺヽヾー]{2,})(?=さん|ちゃん|君|くん|様|さま|殿|先生|先輩|先辈|っち)")
# 汉字姓名 + 敬称。必须带敬称才算，否则「今日」「本当」这种全被当成名字。
KANJI_NAME_RE = re.compile(
    r"([一-龥]{2,4})(?=さん|ちゃん|君|くん|様|さま|殿|先生|先輩|先辈|っち)")
# 拉丁名：首字母大写，至少 3 个字母，前后不是字母。
LATIN_NAME_RE = re.compile(r"(?<![A-Za-z])([A-Z][a-z]{2,15})(?![A-Za-z])")
# 任意拉丁词（含全小写），用来统计大小写形态。
_LATIN_WORD_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]{3,20}(?![A-Za-z])")
# 普通名词在句中会小写出现（"the king"），专有名词基本不会（"King Simon"）。
# 实测 The Last Sovereign 11882 条台词：12 个真名的小写形态占比全是 0%，
# 而 Master 62.5% / Lust 77% / Goddess 82.5% / Aura 43.5% / Strike 31.6% /
# King 8.0% / Lady 3.8% 这类普通词都在 3% 以上。用 3% 一刀切正好分开，
# 同时保住 Emperor(0%) / Synergy(1.2%) / Incubus(1.0%) / Cumdump(0.8%) 这些
# 真称号——它们只在特指某位时才大写，小写出现是极个别笔误。
LATIN_LOWER_CUT = 0.03
# 汉字术语：2-6 个汉字。光靠正则判断不了词性（「今日」「本当」也匹配），
# 所以不按 n-gram 硬扫（那样 150 个候选里 140 个是废话），只认两个更强信号：
# ① 被引号/书名号括起来（作者在强调它）；② 整段汉字连写且反复出现。
QUOTED_HANJI_RE = re.compile(r"[「『\"“]([一-龥]{2,8})[」』\"”]")
# 连续汉字串。日文里名词基本被假名隔开，所以「最长的连续汉字段」通常就是一个词：
# 「聖剣の力」→「聖剣」「力」，「魔導書を開け」→「魔導書」。
KANJI_RUN_RE = re.compile(r"[一-龥]{2,6}")

# 常见外来语/界面词：长得像术语但其实是通用词。
# 收进表里反而有害——它们本来就该按上下文灵活翻。
STOP_KANA = {
    "レベル", "スキル", "ダメージ", "アイテム", "ゲーム", "セーブ", "ロード",
    "メニュー", "ウィンドウ", "テキスト", "ボタン", "クリック", "スタート",
    "オプション", "コンフィグ", "バック", "タイトル", "システム", "ヒント",
    "エラー", "データ", "ファイル", "フォルダ", "マウス", "キーボード",
    "ショートカット", "スペース", "キー", "タイプ", "モード", "ページ",
    "サイズ", "カラー", "フォント", "ボイス", "サウンド", "ミュージック",
    "ストーリー", "キャラ", "キャラクター", "シーン", "イベント", "マップ",
    "ステージ", "ポイント", "コイン", "ゴールド", "エネミー", "モンスター",
    "ボス", "パーティ", "クエスト", "ミッション", "スピード", "パワー",
    "ハート", "ラブ", "キス", "エッチ", "セックス", "オーガズム", "クリトリス",
    "ペニス", "ヴァギナ", "アナル", "フェラ", "パイズリ", "手コキ", "中出し",
    "ロリ", "ショタ", "オナニー", "ヌード", "ランジェリー", "コンドーム",
    "バイブ", "ローター", "ローション", "スワップ", "プレイ", "エロ", "アダルト",
    "シーン", "ストーリー", "エンド", "バッド", "グッド", "トゥルー", "ルート",
}

# 常用汉字词（不当姓名）。姓名通常是「姓+名」或罕见组合，但没语料没法判断，
# 所以只排除最确定的几个高频通用词。
STOP_KANJI = {
    "今日", "明日", "昨日", "本当", "大丈", "自分", "相手", "気持", "気分",
    "時間", "場所", "人間", "世界", "学校", "会社", "仕事", "家族", "友達",
    "先生", "先輩", "後輩", "彼女", "彼氏", "主人", "奥様", "旦那", "子供",
    "部屋", "電話", "番号", "名前", "顔面", "身体", "普通", "全部", "半分",
}
# 注：上面「先生」「先輩」「主人」出现在敬称正则时会带出前 2-4 个汉字，
# 已由 STOP_KANJI 挡掉；真正的姓名如「田中さん」→ 抽到「田中」，不在表里。

STOP_LATIN = {
    "The", "And", "But", "You", "She", "Her", "His", "Him", "They", "Them",
    "What", "When", "Where", "Why", "How", "Who", "Yes", "Not", "Now", "All",
    "This", "That", "There", "Then", "With", "Have", "Will", "Can", "Are",
    "Was", "Were", "For", "From", "About", "Just", "Like", "Look", "Come",
    "Don", "Did", "Does", "Get", "Got", "Let", "May", "Might", "Must", "Need",
    "Okay", "Well", "Yeah", "Ohh", "Ahh", "Hmm", "Mmm", "God", "Oh",
}

# Ren'Py 的 Character("显示名", ...) 定义。`Character(_("名字"))` 这种也吃。
_CHAR_DEF_RE = re.compile(
    r'\bCharacter\s*\(\s*(?:_\s*\(\s*)?["\']([^"\']{1,40})["\']')
# `define ds1 = "Daisy"` 这种把显示名赋给变量的写法（Savior 里满地都是）
_DEF_NAME_RE = re.compile(
    r'^\s*define\s+\w+\s*=\s*["\']([^"\']{1,24})["\']\s*$', re.M)


def scan_character_defs(root: Path, engine: str) -> list[str]:
    """从源码里抄专有名词的权威定义。

    只在 Ren'Py 上有意义（Character 是引擎概念）。RPG Maker 的人名在
    Actors.json 里，已经随 extract 进 items 了，这里不重复处理。
    """
    if engine != "renpy":
        return []
    game = root / "game"
    if not game.is_dir():
        return []
    found: list[str] = []
    seen: set[str] = set()
    for p in sorted(game.rglob("*.rpy")):
        parts = set(p.relative_to(game).parts)
        if parts & {"tl", "cache", "saves", "__pycache__"}:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _CHAR_DEF_RE.finditer(txt):
            name = m.group(1).strip()
            # 排除把变量名/插值当名字的写法，如 Character("[mc]")
            if not name or "[" in name or "%" in name or len(name) > 20:
                continue
            if name not in seen:
                seen.add(name)
                found.append(name)
        for m in _DEF_NAME_RE.finditer(txt):
            name = m.group(1).strip()
            if len(name) < 2 or "[" in name or "%" in name:
                continue
            if name not in seen:
                seen.add(name)
                found.append(name)
    return found


def collect_candidates(items: list[dict], min_freq: int = 3,
                       top_n: int = 150) -> list[tuple[str, int, str]]:
    """统计专有名词候选。返回 [(原文, 频次, 类型)]，按可信度+频次降序。

    只看台词（dialogue），不看 UI 文案——UI 里的 "Back"/"Save" 不是术语。
    """
    freq: dict[str, int] = {}
    kind: dict[str, str] = {}
    lower: dict[str, int] = {}          # 小写形态出现次数，判普通名词用

    def bump(word: str, k: str):
        if len(word) < 2 or len(word) > 20:
            return
        freq[word] = freq.get(word, 0) + 1
        # 已有的类型不降级（汉字姓名比片假名更可信）
        if word not in kind or _rank(k) < _rank(kind[word]):
            kind[word] = k

    for it in items:
        if it.get("kind") != "dialogue":
            continue
        src = it.get("src") or ""
        if not src:
            continue
        # 先统一次大小写形态。英文台词里普通名词会小写出现，专有名词不会。
        for m in _LATIN_WORD_RE.finditer(src):
            w = m.group(0)
            cap = w[:1].upper() + w[1:].lower()
            if w[:1].islower():
                lower[cap] = lower.get(cap, 0) + 1
        for m in KANJI_NAME_RE.finditer(src):
            w = m.group(1)
            if w not in STOP_KANJI:
                bump(w, "kanji")
        # 引号强调的汉字词：作者明确在标记它，可靠性仅次于 Character 定义
        for m in QUOTED_HANJI_RE.finditer(src):
            w = m.group(1)
            if w not in STOP_KANJI:
                bump(w, "quoted")
        # 连续汉字串：只收出现 3 次以上的（「聖剣」「魔導書」这类专有名词会反复
        # 出现，「本当」「今日」在停用表里，剩下的偶发词频次过不了门槛）
        for m in KANJI_RUN_RE.finditer(src):
            w = m.group(0)
            if w not in STOP_KANJI:
                bump(w, "kanji_run")
        for m in KANA_NAME_RE.finditer(src):
            w = m.group(1)
            if w not in STOP_KANA:
                bump(w, "kana_name")
        for m in KANA_RE.finditer(src):
            w = m.group(0)
            if w not in STOP_KANA:
                bump(w, "kana")
        for m in LATIN_NAME_RE.finditer(src):
            w = m.group(1)
            if w not in STOP_LATIN:
                bump(w, "latin")

    # 带敬称的姓名（汉字或片假名）出现 2 次就够——「リリスちゃん」这种是
    # 明确的人名信号，不该被通用门槛卡掉。
    rows = [(w, c, kind[w]) for w, c in freq.items()
            if c >= min_freq or (kind[w] in ("kanji", "kana_name") and c >= 2)]
    # 拉丁词再过一道大小写检查：小写形态占比过高的直接踢掉，
    # 「King」「Lady」「Master」「Lust」这类普通称谓/名词就进不了候选。
    rows = [r for r in rows
            if kind[r[0]] != "latin" or not _too_lower(r[0], r[1], lower)]
    rows.sort(key=lambda r: (_rank(r[2]), -r[1]))
    return rows[:top_n]


def _too_lower(word: str, cap: int, lower: dict[str, int]) -> bool:
    """小写形态占比超过阈值 → 当成普通名词（不是专有名词）。"""
    lo = lower.get(word, 0)
    total = cap + lo
    if total <= 0:
        return False
    return (lo / total) > LATIN_LOWER_CUT


def _rank(k: str) -> int:
    """越小越可信。char=Character定义，quoted=引号强调，姓名带敬称最可靠。"""
    return {"char": 0, "quoted": 1, "kanji": 2, "kana_name": 3,
            "kanji_run": 4, "kana": 5, "latin": 6}.get(k, 9)


# ---------------------------------------------------------------- 模型确认

ANALYST_SYSTEM = (
    "你是游戏本地化团队的术语管理员。任务是给一款日文游戏建立术语表，"
    "确保全篇译名统一。\n"
    "\n"
    "给你的是一批从台词里统计出来的高频词候选，以及若干条样本台词做语境参考。\n"
    "你要判断每个候选是不是「需要统一译名的专有名词」，是的话给出简体中文译名。\n"
    "\n"
    "算专有名词（要收）：\n"
    "- 人名、昵称、称呼（エレナ / 田中さん / 先輩 这类指人的）\n"
    "- 地名、店名、学校名、组织名\n"
    "- 作品内独有的招式、道具、称号、种族、专有设定\n"
    "\n"
    "不算（不要收）：\n"
    "- 普通名词、动词、形容词、感叹词、拟声词\n"
    "- 日常外来语（レベル / スキル / ダメージ / ゲーム 这类通用词）\n"
    "- 界面按钮文字（セーブ / ロード / バック 这类）\n"
    "- 单独一个假名或一个汉字的词\n"
    "- 普通称谓和普通名词的大写形式：英文里句首、称呼、强调都会大写，\n"
    "  像 King / Lady / Master / Goddess / Lust 这种在句中也会小写出现的词，\n"
    "  不是专有名词，不要收。只有当它明确是某个角色的固定称号时才收。\n"
    "- 人名的复数形式（Simons / Yarras 这类），译名跟着本名走就行\n"
    "\n"
    "译名要求：\n"
    "1. 音译人名用常见汉字，不用生僻字；\n"
    "2. 已有通用译法的用通用译法；\n"
    "3. 同一个词只出一条，不要重复；\n"
    "4. 译名要短，不要把解释写进去。\n"
    "\n"
    '只输出 JSON：{"terms":[{"src":"原文","dst":"中文","kind":"人名|地名|组织|招式|道具|称呼|设定"}]}\n'
    "没有合适的就输出 {\"terms\":[]}。不要解释，不要 markdown 代码块。"
)


def _pick_samples(items: list[dict], limit: int = 40) -> list[str]:
    """挑样本台词：覆盖尽量多的说话人，各取前几句。"""
    by_who: dict[str, list[str]] = {}
    for it in items:
        if it.get("kind") != "dialogue":
            continue
        src = (it.get("src") or "").strip()
        if len(src) < 4:
            continue
        who = it.get("who") or "_"
        lst = by_who.setdefault(who, [])
        if len(lst) < 6:
            lst.append(src)
    out: list[str] = []
    # 轮转取样，保证不被人数多的角色占满
    for i in range(6):
        for who in by_who:
            if i < len(by_who[who]):
                out.append(by_who[who][i])
                if len(out) >= limit:
                    return out
    return out


def _extract_json_obj(text: str) -> dict:
    """从模型输出里抠 JSON 对象。容忍 ```json 包裹和前后废话。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        obj = json.loads(t, strict=False)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def build(backend, items: list[dict], root: Optional[Path] = None,
          engine: str = "", existing: Optional[dict] = None,
          log: Optional[Callable[[str], None]] = None,
          min_freq: int = 3, top_n: int = 120) -> dict[str, str]:
    """生成术语表。返回 {原文: 译文}。

    existing 里的词会被保留、不覆盖——手工维护的优先级永远高于自动生成。
    """
    log = log or (lambda *a: None)
    existing = existing or {}
    out: dict[str, str] = {}

    # 静态定义的人名先行收下（但译名还得靠模型，这里只保证不漏词）
    chars = scan_character_defs(root, engine) if root else []
    cands = collect_candidates(items, min_freq=min_freq, top_n=top_n)
    cand_words = [c[0] for c in cands]
    for c in chars:
        if c not in cand_words:
            cand_words.append(c)

    # 已经在表里的不必再问模型
    todolist = [w for w in cand_words if w not in existing]
    log(f"[术语] 候选 {len(cand_words)} 个（其中 {len(cand_words) - len(todolist)} 个已在表里）")
    if not todolist:
        return dict(existing)

    samples = _pick_samples(items)
    # 候选太多就分片，每片不超过 60 个，避免模型漏答
    chunks = [todolist[i:i + 60] for i in range(0, len(todolist), 60)]
    got = 0
    for idx, chunk in enumerate(chunks, 1):
        user = _build_user_prompt(chunk, samples)
        try:
            raw = backend.chat(ANALYST_SYSTEM, user, temperature=0.1)
        except Exception as e:
            log(f"[术语] 第 {idx} 片调用失败：{e}")
            continue
        terms = _extract_json_obj(raw).get("terms") or []
        n0 = len(out)
        for t in terms:
            if not isinstance(t, dict):
                continue
            src = str(t.get("src") or "").strip()
            dst = str(t.get("dst") or "").strip()
            if not src or not dst or src == dst:
                continue
            if src not in chunk:      # 模型自己加的词不收，防幻觉
                continue
            if src in existing:
                continue
            out[src] = dst
        got += len(out) - n0
        log(f"[术语] 第 {idx}/{len(chunks)} 片：确认 {len(out) - n0} 条")

    merged = dict(existing)
    merged.update(out)
    return merged






def _build_user_prompt(cands: list[str], samples: list[str]) -> str:
    lines = ["候选词（每行一个）："]
    lines.extend(f"- {c}" for c in cands)
    if samples:
        lines.append("")
        lines.append("样本台词（只是语境参考，不要翻译它们）：")
        lines.extend(f"- {s}" for s in samples[:40])
    lines.append("")
    lines.append("请判断哪些是专有名词并给统一中文译名，输出 JSON。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 存取

def load(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in obj.items() if k and v}
    except Exception:
        return {}


def save(path: str | Path, terms: dict[str, str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(terms, ensure_ascii=False, indent=2,
                            sort_keys=True), encoding="utf-8")


def merge(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    """base（手工维护的）优先，extra 只补空缺。"""
    out = dict(base)
    for k, v in extra.items():
        if k not in out:
            out[k] = v
    return out
