"""第三道兜底：谷歌翻译（Google 免费端点）。

用于 WorkBuddy 和 NVIDIA Nemotron 都翻不出来的句子 —— 实测 The Last Sovereign
剩余 60 条极度露骨的性描写，两边都拒，只有谷歌照翻（它不过滤文学性描写）。

实测要点（2026-11-21，本机 FlClash 代理 127.0.0.1:7890）：
- `translate.googleapis.com/translate_a/single`（client=gtx）**429 限流**，
  重试也没用。
- `clients5.google.com/translate_a/t`（client=dict-chrome-ex）**可用**，
  支持多 `q` 参数批量（4 句 1.88s），返回 `["译文1","译文2",...]`。
- 占位符 `⟨G0⟩`/`⟨G1⟩` 原样保留，人名可以继续走术语表对照。
- 必须走代理：本机直连谷歌全超时。代理从环境变量取，取不到就探 7890。

它是机器翻译，质量低于大模型，所以只在最后一道用，而且**只用它自己翻**，
不覆盖前两道已有的结果。
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

# 端点优先级：实测 clients5 稳，gtx 会 429
ENDPOINTS = [
    ("https://clients5.google.com/translate_a/t", "dict-chrome-ex"),
    ("https://translate.googleapis.com/translate_a/single", "gtx"),
]

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 单次请求的字符上限（GET URL 长度考虑，保守一点）
MAX_CHARS_PER_REQUEST = 3000
MAX_LINES_PER_REQUEST = 20

_PROXY_CACHE: str | None | bool = False   # False = 没探过


def detect_proxy() -> str:
    """找可用的 HTTP 代理。优先环境变量，其次探本机常见端口。"""
    global _PROXY_CACHE
    if _PROXY_CACHE is not False:
        return _PROXY_CACHE or ""

    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(var, "").strip()
        if v:
            if v.startswith("socks"):
                continue
            _PROXY_CACHE = v
            return v

    # 环境里没有就探本机代理端口（FlClash 默认 7890）
    for port in (7890, 7891, 1080, 10808, 10809, 8080, 8118, 8888):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.35):
                _PROXY_CACHE = f"http://127.0.0.1:{port}"
                return _PROXY_CACHE
        except OSError:
            continue
    _PROXY_CACHE = None
    return ""


def _opener(proxy: str) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def _parse(raw: str, n: int) -> list[str]:
    """把谷歌五花八门的返回摊平成 n 条译文。"""
    try:
        d = json.loads(raw)
    except Exception:
        return []

    # clients5 / dict-chrome-ex: ["译1", "译2", ...]
    if isinstance(d, list) and all(isinstance(x, str) for x in d):
        return d

    # translate_a/single: [ [["译1","原1",...], ["译2",...]], ... ]
    if isinstance(d, list) and d and isinstance(d[0], list):
        segs = []
        for group in d:
            if isinstance(group, list):
                for seg in group:
                    if isinstance(seg, list) and seg and isinstance(seg[0], str):
                        segs.append(seg[0])
                    elif isinstance(seg, str):
                        segs.append(seg)
        return ["".join(segs)] if segs else []

    # 单句对象形式 {"sentences": [{"trans": "..."}]}
    if isinstance(d, dict):
        out = []
        for s in d.get("sentences") or []:
            if isinstance(s, dict) and isinstance(s.get("trans"), str):
                out.append(s["trans"])
        if out:
            return ["".join(out)]
    return []


def translate(lines: list[str], src_lang: str = "en", dst_lang: str = "zh-CN",
              timeout: int = 25, retries: int = 3, log=None) -> list[str]:
    """批量翻译。返回与 lines 等长的列表，失败的位留空串。"""
    if not lines:
        return []

    proxy = detect_proxy()
    opener = _opener(proxy)
    out: list[str] = [""] * len(lines)

    # 按字符数和条数切片，避免 URL 过长
    chunks: list[tuple[int, int]] = []
    start = 0
    while start < len(lines):
        end, used = start, 0
        while end < len(lines) and end - start < MAX_LINES_PER_REQUEST:
            ln = len(lines[end]) + 1
            if end > start and used + ln > MAX_CHARS_PER_REQUEST:
                break
            used += ln
            end += 1
        chunks.append((start, end))
        start = end

    for ci, (a, b) in enumerate(chunks):
        seg = lines[a:b]
        got: list[str] = []
        for attempt in range(retries):
            for base, client in ENDPOINTS:
                params = [("client", client), ("sl", src_lang), ("tl", dst_lang)]
                if client == "gtx":
                    params.append(("dt", "t"))
                params += [("q", s) for s in seg]
                url = base + "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                })
                try:
                    with opener.open(req, timeout=timeout) as r:
                        raw = r.read().decode("utf-8", "replace")
                    got = _parse(raw, len(seg))
                    if got:
                        break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        continue          # 这个端点限流，换下一个
                    if log and attempt == retries - 1:
                        log(f"  [谷歌] HTTP {e.code}")
                    continue
                except Exception as e:
                    if log and attempt == retries - 1:
                        log(f"  [谷歌] {type(e).__name__}: {str(e)[:80]}")
                    continue
            if got:
                break
            time.sleep(1.2 * (attempt + 1))

        if not got:
            if log:
                log(f"  [谷歌] 第 {ci+1}/{len(chunks)} 片（{len(seg)} 句）没拿到译文")
            continue

        for i, txt in enumerate(got[:len(seg)]):
            out[a + i] = txt.strip()
        if len(got) < len(seg) and log:
            log(f"  [谷歌] 第 {ci+1}/{len(chunks)} 片只回 {len(got)}/{len(seg)} 条")

    return out
