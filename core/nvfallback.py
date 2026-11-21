"""NVIDIA NIM 兜底后端。

为什么需要它
------------
WorkBuddy 上游（deepseek-v4.1-flash）对 R18 台词有两道坎：
  1. 内容过滤：直接回「抱歉，系统检测到您当前输入的信息存在敏感内容」；
  2. **流中途掐断**：20 句批翻到第 10 句，突然塞一句
     `Sorry, I can't respond to this question.` 并关流，数组永不闭合。

第一道能靠前缀抢救和逐句重试绕过一部分，但真·敏感句（露骨器官、性行为
描写）无论怎么包语境都会被挡。The Last Sovereign 实测：10589 句里有
3% 左右是 DeepSeek 死也不肯翻的。

NVIDIA NIM 上的 Nemotron 系列是国外训练的，对虚构作品的露骨内容明显
宽松。实测同一批 20 句 R18 段落：
    nemotron-3-ultra-550b-a55b       37.1s  20/20  ✅ 质量最好
    nemotron-3.5-super-vl-120b-a12b  46.8s  20/20  ✅ 但推理冗长（13981 字思维链）
所以默认选 ultra。

两个实现细节
------------
* **推理模型的坑**：super-vl / lightning 这类是 reasoning 模型，思维链走
  `message.reasoning_content`，正文在 `message.content`。max_tokens 太小
  时额度全被思维链吃掉，content 返回 null —— 看上去像"模型拒答"，其实
  是 `finish_reason: "length"`。ultra 也有思维链（约 4000 字），
  max_tokens 给 8192 才稳。
* 不复用 `core/llm.py` 的拒绝检测：这边是兜底，只要能拿到 JSON 就用。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
# 备选（速度更快但译文里会留英文原文，实测 lightning 把 cock/pussy 原样保留）
ALT_MODELS = [
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
]

SYSTEM = (
    "You are a professional game localization translator working on a published "
    "fictional adult game. The input is a JSON array of English dialogue lines "
    "taken from ONE continuous scene, in story order.\n"
    "\n"
    "Requirements:\n"
    "1. Translate every line into Simplified Chinese, verbatim and at full intensity. "
    "This is published fiction; keep all explicit sexual content exactly as written. "
    "Do not soften, censor, summarize, or omit anything.\n"
    "2. The lines are consecutive dialogue from the same scene, so pronouns, "
    "tense and tone must stay consistent across them. A line that continues a "
    "sentence from the previous line must read as a continuation.\n"
    "3. Placeholders like ⟨G0⟩ ⟦1⟧ are masked glossary terms or engine variables. "
    "Copy them through unchanged, in the position Chinese word order wants. "
    "NEVER translate, omit or renumber them.\n"
    "4. Keep every \\n escape exactly where it is.\n"
    "5. Output ONLY a JSON array of the same length as the input, nothing else."
)

# 兜底场景的补充说明：告诉模型它与主流程的合作关系，避免它把句子当孤立字符串。
USER_TMPL = (
    "Scene label: {label}\n"
    "Previous lines (context only, do NOT translate):\n{before}\n"
    "Lines to translate ({n}):\n{payload}\n"
    "Following lines (context only, do NOT translate):\n{after}\n"
    "\n"
    "Output the JSON array of {n} translated lines."
)


def _credentials_path() -> Path:
    return Path(os.path.expanduser("~/.dsh/.credentials.yaml"))


def find_key() -> str:
    """拿 NVIDIA API key。优先环境变量，其次 DSH 凭据文件里的 refs 段。"""
    for env in ("NVIDIA_API_KEY", "NVAPI_KEY", "NIM_API_KEY"):
        v = os.environ.get(env)
        if v:
            return v.strip()
    p = _credentials_path()
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    for line in text.splitlines():
        if "NVIDIA_API_KEY" in line and ":" in line:
            v = line.split(":", 1)[1].strip().strip("\"'")
            if v:
                return v
    return ""


@dataclass
class NVResult:
    ok: bool
    lines: list[str]
    error: str = ""
    finish: str = ""


def chat(lines: list[str], ctx: dict | None = None, model: str = DEFAULT_MODEL,
         key: str = "", max_tokens: int = 8192, temperature: float = 0.2,
         timeout: int = 300) -> NVResult:
    """把一批句子发给 NVIDIA，返回等长译文列表。

    ctx 与主流程共用同一个结构（label/before/after），这样兜底翻出来的句子
    也有场景标签和前后文，跟主后端翻的接得上。

    不做拒绝检测 —— 这是兜底路径，能拿到 JSON 就用，拿不到由调用方决定。
    """
    key = key or find_key()
    if not key:
        return NVResult(False, [], "没找到 NVIDIA_API_KEY")
    ctx = ctx or {}
    user = USER_TMPL.format(
        label=ctx.get("label") or "(none)",
        before=ctx.get("before") or "(none)",
        after=ctx.get("after") or "(none)",
        n=len(lines),
        payload=json.dumps(lines, ensure_ascii=False),
    )
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(BASE, data=body, method="POST", headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        return NVResult(False, [], f"HTTP {e.code}: {detail}")
    except Exception as e:
        return NVResult(False, [], str(e)[:200])
    try:
        data = json.loads(raw)
    except Exception as e:
        return NVResult(False, [], f"响应不是 JSON: {e}")
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = msg.get("content") or ""
    finish = ch.get("finish_reason") or ""
    if not content.strip():
        # 推理模型常见：额度全花在 reasoning_content 上，content 是 null。
        rc = msg.get("reasoning_content") or ""
        return NVResult(False, [], f"content 为空（reasoning {len(rc)} 字，finish={finish}）", finish)
    outs = _parse_array(content)
    if outs is None:
        return NVResult(False, [], f"解析失败: {content[:120]!r}", finish)
    return NVResult(True, outs, "", finish)


def _parse_array(text: str) -> list[str] | None:
    """从输出里抠 JSON 数组，容忍 ```json 围栏和前后废话。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    i = t.find("[")
    if i < 0:
        return None
    # 用 raw_decode 从第一个 `[` 开始解析，让 JSON 解码器自己配对括号：
    # 旧写法 rfind("]") 会撞上译文里内嵌的半角 `]`（台词如 "He's the [best] one"）
    # 而错误截断，导致 json.loads 失败、整批兜底白跑。
    try:
        arr, _ = json.JSONDecoder().raw_decode(t[i:])
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    return [("" if x is None else str(x)) for x in arr]
