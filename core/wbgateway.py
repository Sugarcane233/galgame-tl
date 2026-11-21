"""自动发现 workbuddy 本地网关（dsh-connect-workbuddy 的 shim）。

为什么需要这个
--------------
插件用 `server.listen(0)` 拿到内核随机分配的端口，每次启动都变，没有固定
base_url 可以写死。但网关有三条固定特征：
  * 只监听回环
  * `GET /healthz` 返回 {"ok": true}
  * 任何其他路由缺 Bearer 时返回
    {"error":{"message":"missing or invalid Authorization bearer", ...}}

于是扫描「本进程可见的回环端口 + 拒绝体特征」就能定位它。
Bearer 是进程内随机生成的（randomBytes(32)），拿不到；但 DSH 自己知道，
所以这里只负责找到端口 + 提示用户去插件设置里取 token，或者退一步用
任意 OpenAI 兼容端点（DeepSeek / ollama 等）。

实测本机：33861 / 41935 两个端口都符合特征（插件为国内区域各起一个）。
"""
from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request

WB_HINT = "missing or invalid Authorization bearer"


def _listen_ports() -> list[int]:
    """读 /proc 找回环监听端口（比扫 65535 个端口快得多）"""
    ports = set()
    try:
        out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                addr = parts[3]
                m = re.search(r"(?:127\.0\.0\.1|\[::1\]|localhost):(\d+)$", addr)
                if m:
                    ports.add(int(m.group(1)))
    except Exception:
        pass
    if not ports:
        for p in range(30000, 65000, 1):
            pass   # 不做全扫，太慢
    return sorted(ports)


def probe(port: int, timeout: float = 0.6) -> dict | None:
    """判断该端口是不是 workbuddy 网关；是则返回 {"port":..., "models":[...]}"""
    base = f"http://127.0.0.1:{port}"
    # 1) /healthz 不带 token 应 401（有 token 才 200），所以看 /v1/models 的拒绝体更准
    try:
        req = urllib.request.Request(base + "/v1/models")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(200).decode("utf-8", "replace")
            # 没设 token 却成功了，说明不是这个插件（或有开放代理）
            if '"object"' in body and '"data"' in body:
                return {"port": port, "models": [], "open": True}
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace")
        if WB_HINT in body:
            return {"port": port, "models": [], "open": False}
    except Exception:
        return None
    return None


def discover(with_models: bool = False) -> list[dict]:
    found = []
    for port in _listen_ports():
        if port in (3080, 18787, 8078, 8188, 631, 53, 9222):
            continue
        hit = probe(port)
        if hit:
            found.append(hit)
    return found


if __name__ == "__main__":
    hits = discover()
    if not hits:
        print("没找到 workbuddy 网关。可能 dsh-connect-workbuddy 没启用，或它正在重启。")
    for h in hits:
        print(f"找到: http://127.0.0.1:{h['port']}/v1"
              + ("  (未鉴权，可直接用)" if h.get("open") else "  (需要 Bearer token)"))
