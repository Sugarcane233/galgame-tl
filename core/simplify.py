"""简体化清洗：把模型偶尔输出的繁体、港台字形/词汇统一成大陆简体。

为什么需要：deepseek-v4.1-flash 在中文长句里偶尔会滑到繁体（「開始」「發出」
「輕柔」），实测 The Last Sovereign 8483 条译文里 **1047 条（12.3%）** 混了
繁体字。不改不影响阅读，但同一段对话里简繁交错很难看。

做法：用 `zhconv` 的 `zh-cn` 档（不只转字形，也转词汇：软体→软件、网路→网络）。
本机没有 opencc，`pip` 也被裁掉了 venv 里的副本，装包走 `uv pip install`。

安全边界：
- 只处理汉字，ASCII（`\\V[1]`、`%1`、`\\n`、`[name]`、`{i}`）原样保留。
- 已 unmask 的译文里不该再有 `⟨G0⟩` 占位符；万一有也照样保留（非汉字）。
- 空串、纯 ASCII 直接跳过，不做无谓处理。
"""
from __future__ import annotations

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_conv = None
_conv_err = ""


def _get():
    """惰性加载 zhconv。装不上就返回 None，调用方降级。"""
    global _conv, _conv_err
    if _conv is not None or _conv_err:
        return _conv
    try:
        import zhconv as _z
        _conv = _z
    except Exception as e:
        _conv_err = str(e)[:120]
        _conv = None
    return _conv


def available() -> tuple[bool, str]:
    """(能用吗, 说明)。GUI/CLI 用它在启动时提示。"""
    z = _get()
    if z is None:
        return False, f"缺 zhconv（{_conv_err}）；装法：uv pip install zhconv"
    return True, "zhconv " + getattr(z, "__version__", "")


def has_cjk(s: str) -> bool:
    return bool(s) and bool(_CJK.search(s))


def to_simplified(s: str) -> str:
    """单条繁→简。没有汉字或没装库时原样返回。"""
    if not s or not has_cjk(s):
        return s
    z = _get()
    if z is None:
        return s
    try:
        return z.convert(s, "zh-cn")
    except Exception:
        return s


def clean_mapping(mapping: dict[str, str], log=None) -> tuple[dict[str, str], int]:
    """批量清洗 {原文: 译文}，返回 (新字典, 改了几条)。

    只改**译文**，原文永远不动 —— 原文是回填时的 key，动了就对不上游戏数据。
    """
    out: dict[str, str] = {}
    changed = 0
    for k, v in mapping.items():
        if not isinstance(v, str):
            out[k] = v
            continue
        nv = to_simplified(v)
        if nv != v:
            changed += 1
        out[k] = nv
    if log and changed:
        log(f"  简体化清洗：{changed}/{len(mapping)} 条含繁体，已转换")
    return out, changed
