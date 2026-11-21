"""翻译缓存清洗：修复历史缓存里残留的占位符与控制码。

为什么需要它
------------
`llm.protect()` 会把 `\\c[21]` 这种引擎控制码换成 `⟦0⟧` 再送去翻译，模型
经常把闭合符 `⟧`(U+27E7) 抄成形近的 `◧`(U+25E7)，旧版 `restore()` 只做精确
字符串替换，于是占位符永远换不回来，译文里就留下「⟦0◧哈，才怪。」这种串，
直接写进游戏变成乱码。

`restore()` 已经改成形近容错 + 反复展开，但**已经落盘的缓存不会自己变好**：
下一轮跑的时候这些句子命中缓存，脏译文被原样写回游戏。所以要有这个工具，
在跑之前把历史缓存洗一遍。

三种脏数据，处理方式不同
------------------------
1. **可修复**：占位符还在，只是闭合符被抄错。用新 `restore()` 重算即可，
   原文不动，只改译文。
2. **幽灵占位符**：原文里根本没有占位符，译文里却有 `⟦0⟧`。这是模型自己
   吐出来的垃圾（也可能是不等长返回导致的错位），无法修复，只能删掉重翻。
3. **控制码丢失**：原文有 3 个 `\\c[..]`，译文只有 1 个。重翻。

用法
----
    python -m core.cachefix CACHE.json              # 只诊断，不改文件
    python -m core.cachefix CACHE.json --apply      # 就地修复
    python -m core.cachefix CACHE.json --apply --glossary glossary.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile

from .llm import (PROTECT_RULES, protect, restore,
                  mask_glossary, unmask_glossary, _PH_RX, _GL_RX, _SEQ_RX)

# 控制码分两类，严重程度差很远：
#   颜色/图标/变量 —— 丢了会让控制码外泄、显示错色、变量不展开，必须重翻
#   节奏/换行     —— \. \| 只是停顿，模型常换成「……」或逗号，观感损失，不值得重翻
_COLOR_RX = re.compile(r"\\[A-Za-z]+\[[^\[\]]*\]")
_RHYTHM_RX = re.compile(r"\\[.{}|!^$ ]")


def _ctrl_counts(s: str) -> tuple[dict[str, int], dict[str, int]]:
    """返回 (颜色类计数, 节奏类计数)。"""
    color: dict[str, int] = {}
    rhythm: dict[str, int] = {}
    for m in _COLOR_RX.findall(s):
        key = re.sub(r"\[[^\]]*\]", "[]", m)
        color[key] = color.get(key, 0) + 1
    for m in _RHYTHM_RX.findall(s):
        rhythm[m] = rhythm.get(m, 0) + 1
    return color, rhythm


def repair_one(src: str, dst: str, glossary: dict[str, str]
               ) -> tuple[str | None, str]:
    """尝试修复一条缓存。

    返回 `(新译文, 原因)`：
      - 新译文非 None → 修好了，写回
      - 新译文 None   → 修不好，删掉让它重翻
      - 新译文 == dst → 本来就干净，原因 "clean"
    """
    if not isinstance(dst, str) or not dst.strip():
        return None, "empty"

    t, st = protect(src)
    t2, gt = mask_glossary(t, glossary) if glossary else (t, {})
    n_slots = len(st) + len(gt)

    fixed = unmask_glossary(restore(dst, st), gt)

    # 序号协议上线后，历史缓存里理论上不会带 ⟦S n⟧（那是发送时的运输前缀），
    # 但模型偶尔会惯性把 ⟦S0⟧ 一起吐进译文，落进缓存后应剥掉，别写进游戏。
    if _SEQ_RX.search(fixed):
        fixed = _SEQ_RX.sub("", fixed)
        if not fixed.strip():
            return None, "phantom"

    # 修完还残留占位符 → 看是原文本来就没有（幽灵），还是编号越界
    residual = _PH_RX.findall(fixed) + _GL_RX.findall(fixed)
    if residual:
        if n_slots == 0:
            return None, "phantom"        # 原文无占位符，模型自己吐的
        return None, "residual"           # 有占位符但对不上，不敢猜

    # 颜色/图标/变量控制码数量对不上 → 译文丢了排版控制或控制码外泄，重翻。
    # 节奏类（\. \|）不参与判定：模型把它们换成「……」是合理的本地化处理，
    # 本作 \. 有 14153 个，全按丢失算会误删 1300+ 条好译文。
    ca, _ra = _ctrl_counts(src)
    cb, _rb = _ctrl_counts(fixed)
    if ca != cb:
        return None, "ctrl_lost"

    if fixed == dst:
        return dst, "clean"
    return fixed, "repaired"


def diagnose(cache: dict, glossary: dict[str, str] | None = None) -> dict:
    """只统计，不改 cache。返回 {原因: 条数} 和样本。"""
    glossary = glossary or {}
    stats: dict[str, int] = {}
    samples: dict[str, list] = {}
    for src, dst in cache.items():
        _, reason = repair_one(src, dst, glossary)
        stats[reason] = stats.get(reason, 0) + 1
        if reason not in ("clean",) and len(samples.setdefault(reason, [])) < 5:
            samples[reason].append((src, dst))
    return {"stats": stats, "samples": samples,
            "total": len(cache)}


def clean(cache: dict, glossary: dict[str, str] | None = None,
          log=None) -> tuple[dict, dict]:
    """返回 (修好的 cache, 统计)。不改入参。"""
    glossary = glossary or {}
    out: dict[str, str] = {}
    stats: dict[str, int] = {}
    for src, dst in cache.items():
        new, reason = repair_one(src, dst, glossary)
        stats[reason] = stats.get(reason, 0) + 1
        if new is not None:
            out[src] = new
        # new 为 None 的条目不写回 → 下一轮会当未翻译重翻
    if log:
        for k in ("repaired", "phantom", "residual", "ctrl_lost", "empty"):
            if stats.get(k):
                log(f"  {k}: {stats[k]}")
        log(f"  保留 {len(out)}/{len(cache)} 条，待重翻 {len(cache) - len(out)} 条")
    return out, stats


def _save(cache: dict, path: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    path = argv[0]
    do_apply = "--apply" in argv
    gloss_path = ""
    if "--glossary" in argv:
        i = argv.index("--glossary")
        if i + 1 < len(argv):
            gloss_path = argv[i + 1]
    elif os.path.exists("glossary.json"):
        gloss_path = "glossary.json"

    if not os.path.exists(path):
        print(f"找不到缓存文件: {path}")
        return 2
    cache = json.load(open(path, encoding="utf-8"))
    glossary = {}
    if gloss_path and os.path.exists(gloss_path):
        try:
            glossary = json.load(open(gloss_path, encoding="utf-8"))
        except Exception as e:
            print(f"术语表读不了（{e}），按空表继续")

    print(f"缓存 {len(cache)} 条，术语表 {len(glossary)} 条")
    if not do_apply:
        d = diagnose(cache, glossary)
        print("\n=== 诊断 ===")
        for k, v in sorted(d["stats"].items(), key=lambda x: -x[1]):
            print(f"  {k:10s} {v:7d}")
        for reason, items in d["samples"].items():
            if reason == "clean":
                continue
            print(f"\n--- {reason} 样本 ---")
            for src, dst in items[:3]:
                print(f"  EN: {src[:65]!r}")
                print(f"  ZH: {dst[:65]!r}")
        print("\n加 --apply 就地修复")
        return 0

    cleaned, stats = clean(cache, glossary, log=print)
    if stats.get("clean") == len(cache):
        print("缓存是干净的，没有改动")
        return 0
    _save(cleaned, path)
    print(f"\n已写入 {path}")
    print(f"  保留 {len(cleaned)} 条，删除 {len(cache) - len(cleaned)} 条（下次运行会重翻）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
