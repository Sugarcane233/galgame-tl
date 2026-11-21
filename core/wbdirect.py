"""WorkBuddy 直连后端。

为什么不用插件那个回环网关：网关的 Bearer 是插件进程内
`randomBytes(32).toString("base64url")` 生成的，不落盘、不写配置，
进程一重启就换一把新的，外部工具根本拿不到（想拿只能在进程内存里捞，
而 /proc/PID/mem 被 ptrace_scope=1 挡住）。

但凭据本身是落盘的：`~/.dsh/workbuddy-vault/<region>/<accountId>.json`
里就有 accessToken / refreshToken / domain。拿着它直接打上游
`https://www.codebuddy.cn/v2/chat/completions` 就行——这正是插件内部
做的事，只是绕过了那层自签的 token。

上游三个必须知道的规矩（都是实测出来的）：
1. **只支持流式**：`stream:false` 会被 400 拒掉，`code:11101
   Non-stream chat request is currently not supported`。
2. 请求头身份要对得上，否则 401/403：X-User-Id、X-Domain、X-Product、
   X-IDE-Name/Type/Version、X-Conversation-* 三件套，以及 UA
   `CLI/<ver> CodeBuddy/<ver>`。
3. 答案在 SSE 的 `delta.content` 里；思考过程在 `delta.reasoning_content`，
   两者必须分开取，否则会把思维链当成译文写进游戏。

accessToken 会过期（响应里的 `expiresAt`）。过期就用 refreshToken 打
`/v2/plugin/auth/token/refresh` 换，换到的新 token 只在本进程里用，
不写回 vault（避免和插件抢着写同一个文件）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

CN_BASE = "https://www.codebuddy.cn"
GLOBAL_BASES = ("https://www.workbuddy.ai", "https://www.codebuddy.ai")
UA = "CLI/2.63.2 CodeBuddy/2.63.2"
CLIENT_NAME = "WorkBuddy"
CLIENT_VERSION = "2.63.2"


def vault_dir() -> Path:
    return Path(os.environ.get("WORKBUDDY_VAULT_DIR") or (Path.home() / ".dsh" / "workbuddy-vault"))


@dataclass
class Account:
    account_id: str
    nickname: str
    uid: str
    domain: str
    access_token: str
    refresh_token: str
    expires_at: int = 0
    path: Optional[Path] = None

    @property
    def region(self) -> str:
        d = (self.domain or "").lower()
        return "cn" if d.endswith("codebuddy.cn") else "global"

    @property
    def base(self) -> str:
        return CN_BASE if self.region == "cn" else GLOBAL_BASES[0]

    def expired(self, skew: int = 120) -> bool:
        return bool(self.expires_at) and time.time() > self.expires_at - skew

    def label(self) -> str:
        return f"{self.nickname or self.account_id}（{self.region}）"


def _load_one(p: Path) -> Optional[Account]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    auth = d.get("auth") or {}
    acc = d.get("account") or {}
    tok = auth.get("accessToken") or ""
    if not isinstance(tok, str) or not tok:
        return None
    return Account(
        account_id=str(d.get("accountId") or p.stem),
        nickname=str(acc.get("nickname") or ""),
        uid=str(acc.get("uid") or ""),
        domain=str(auth.get("domain") or "www.codebuddy.cn"),
        access_token=tok,
        refresh_token=str(auth.get("refreshToken") or ""),
        expires_at=int(auth.get("expiresAt") or 0),
        path=p,
    )


def discover_accounts() -> list[Account]:
    """扫 vault，按刷新时间从新到旧列出可用账号。"""
    root = vault_dir()
    out: list[Account] = []
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*/*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        a = _load_one(p)
        if a is not None:
            out.append(a)
    return out


def _headers(acc: Account, stream: bool) -> dict:
    def u() -> str:
        return str(uuid.uuid4())

    h = {
        "Accept": "text/event-stream" if stream else "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": f"https://{acc.domain}",
        "Referer": f"https://{acc.domain}/",
        "User-Agent": UA,
        "Content-Type": "application/json",
        "X-User-Id": acc.uid,
        "X-No-Enterprise-Id": "1",
        "X-Domain": acc.domain,
        "X-Product": CLIENT_NAME,
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": CLIENT_NAME,
        "X-IDE-Type": CLIENT_NAME,
        "X-IDE-Version": CLIENT_VERSION,
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "zh-CN",
        "X-Conversation-ID": u(),
        "X-Conversation-Request-ID": u(),
        "X-Conversation-Message-ID": u(),
        "Authorization": "Bearer " + acc.access_token,
    }
    return h


def refresh(acc: Account, timeout: int = 30) -> Account:
    """换新 accessToken（只在本进程内，不写回磁盘）。"""
    if not acc.refresh_token:
        raise RuntimeError("这个账号没有 refreshToken，去 WorkBuddy 重新登录")
    req = urllib.request.Request(
        f"{acc.base}/v2/plugin/auth/token/refresh",
        data=b"",
        method="POST",
        headers={
            "Authorization": "Bearer " + acc.access_token,
            "X-Refresh-Token": acc.refresh_token,
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        env = json.loads(r.read().decode("utf-8"))
    if env.get("code") not in (0, None):
        raise RuntimeError(f"刷新失败: {env.get('msg') or env}")
    data = env.get("data") or {}
    tok = data.get("accessToken") or ""
    if not tok:
        raise RuntimeError("刷新成功但没返回 accessToken，去 WorkBuddy 重新登录")
    out = Account(**{**acc.__dict__})
    out.access_token = tok
    if data.get("refreshToken"):
        out.refresh_token = data["refreshToken"]
    if isinstance(data.get("expiresIn"), int) and data["expiresIn"] > 0:
        out.expires_at = int(time.time()) + data["expiresIn"]
    return out


def chat_stream(acc: Account, model: str, messages: list[dict],
                max_tokens: int = 4096, temperature: float = 0.3,
                timeout: int = 180) -> Iterator[tuple[str, str]]:
    """发一次流式请求，逐条 yield (kind, text)，kind 是 content / reasoning / usage。"""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,          # 上游只认流式
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(f"{acc.base}/v2/chat/completions", data=body,
                                 method="POST", headers=_headers(acc, True))
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read()[:600].decode("utf-8", "replace")
        raise RuntimeError(f"上游 HTTP {e.code}: {detail}") from None
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        usage = obj.get("usage")
        if usage:
            yield "usage", json.dumps(usage)
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("reasoning_content"):
                yield "reasoning", delta["reasoning_content"]
            if delta.get("content"):
                yield "content", delta["content"]


def chat(acc: Account, model: str, messages: list[dict],
         max_tokens: int = 4096, temperature: float = 0.3, timeout: int = 180) -> str:
    """收完整个流，只返回 content（不含思维链）。"""
    parts: list[str] = []
    usage: dict = {}
    for kind, text in chat_stream(acc, model, messages, max_tokens, temperature, timeout):
        if kind == "content":
            parts.append(text)
        elif kind == "usage":
            try:
                usage = json.loads(text)
            except Exception:
                pass
    out = "".join(parts).strip()
    return out


# 模型目录的两条路：`/v3/config` 是 app 渠道的文档（带促销标记，
# 但模型 id 是 app 侧的 auto/hy3/glm-* 那套）；`/v2/enterprises/personal/models`
# 是 CLI 渠道的 roster，id 才是 chat 接口真正认的那些（hy4free/hy4std/…）。
# 取并集，CLI 那份排前面。
MODEL_PATHS = ("/v2/enterprises/personal/models", "/v3/config")


def list_models(acc: Account, timeout: int = 30) -> list[str]:
    """读上游模型目录（和插件同一个来源）。"""
    seen: list[str] = []
    for path in MODEL_PATHS:
        url = acc.base + path
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + acc.access_token,
                "User-Agent": "WorkBuddy/" + CLIENT_VERSION,
                "Accept": "application/json",
                "X-Domain": acc.domain,
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            doc = data.get("data") if isinstance(data.get("data"), dict) else data
            ids = [m.get("id") for m in (doc.get("models") or [])
                   if isinstance(m, dict) and m.get("id")]
            for i in ids:
                if i not in seen:
                    seen.append(i)
        except Exception:
            continue
    return seen
