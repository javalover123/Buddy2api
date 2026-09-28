"""上游出站兜底：直连建连失败时，换本机代理重发一次。

排障背景（2026-09-24）：本机到 www.workbuddy.ai（43.160.158.125:443）的 TCP 建连会
成串黑洞 —— 实测连续 60 次 SYN 全部超时、curl 直连返回 000，而同时段 DNS 正常
（p50 3ms）、国内站 p50 0.02s、github 正常。网关日志里的表现就是成串
`[upstream_disconnect] ConnectTimeout` 502（当日 313 条）。同一时刻系统代理
（Clash Verge 写在 Wi-Fi 上的 127.0.0.1:7897）那条出口是通的 —— 用户手动开
WorkBuddy 客户端走的正是它。

所以这里给聊天转发加一层兜底：**直连建连失败**时，换本机代理重发同一个请求。

为什么只兜「建连」：请求体是 bytes（httpx.ByteStream），可以安全重发；一旦上游已经
回包就不能重试 —— 流式正文可能已经下发给客户端，重发会重复执行工具调用。建连类失败
发生在拿到响应之前，所以是安全的。

代理地址候选（都不写死端口，端口常变）：
1. `CB_GATEWAY_UPSTREAM_PROXY`：显式配置；
2. `CODEBUDDY_SERVICE_PROXY_URL`：WorkBuddy 桌面端自己写的服务代理；
3. 系统代理（macOS `scutil --proxy`）。

三者都没有时不做兜底，行为与以前完全一致。

国际站默认一律走代理（2026-09-27）：本机直连出口是运营商 IP，而代理出口在海外。
多个国际版账号在直连出口下被上游按账号拒绝（403 + code 11140，官方客户端同样报错），
逐项排除请求体/模型/token/指纹头后，出站 IP 是唯一未被排除的变量。见 `transport_for`。
用 `CB_GATEWAY_INTL_PROXY=off` 可关掉，退回原来的「直连优先」。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

import httpx

import buddy2api.sites as sites

EXPLICIT_PROXY_VAR = "CB_GATEWAY_UPSTREAM_PROXY"
CLIENT_PROXY_VAR = "CODEBUDDY_SERVICE_PROXY_URL"
# 国际站出站代理开关：设为 off/none/false/0 时退回原来的「直连优先」。
INTL_PROXY_DISABLED_VAR = "CB_GATEWAY_INTL_PROXY"
_DISABLED_VALUES = {"off", "none", "false", "0", ""}

# 建连类失败。注意 httpx 的 ConnectTimeout 继承 TimeoutException 而**不是** ConnectError，
# 两个都要接；ReadTimeout/WriteTimeout 不在此列（那是拿到连接之后的事）。
CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout)

# 同一代理的兜底日志最多每分钟一行：故障窗口里每个请求都会触发，不节流会刷屏。
_REPORT_INTERVAL_SECONDS = 60.0
_last_report_at = 0.0
_fallback_count = 0

_system_proxy_cache: Optional[list[str]] = None


def fallback_count() -> int:
    """累计触发过多少次「直连失败 → 走代理」。"""
    return _fallback_count


def _scutil_value(text: str, key: str) -> str:
    prefix = f"{key} : "
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _system_proxies() -> list[str]:
    """macOS 系统代理（Clash 这类工具会写在这里）。非 macOS 或读不到时返回空列表。"""
    global _system_proxy_cache
    if _system_proxy_cache is not None:
        return _system_proxy_cache
    found: list[str] = []
    try:
        completed = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        )
        text = completed.stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    for kind in ("HTTPS", "HTTP"):
        if f"{kind}Enable : 1" not in text:
            continue
        host = _scutil_value(text, f"{kind}Proxy")
        port = _scutil_value(text, f"{kind}Port")
        if host and port:
            found.append(f"http://{host}:{port}")
    _system_proxy_cache = list(dict.fromkeys(found))
    return _system_proxy_cache


def proxy_for_retry() -> Optional[str]:
    """直连失败时用来兜底的代理地址；没有可用候选时返回 None。"""
    for candidate in (
        os.environ.get(EXPLICIT_PROXY_VAR, "").strip(),
        os.environ.get(CLIENT_PROXY_VAR, "").strip(),
        *_system_proxies(),
    ):
        if candidate:
            return candidate
    return None


def intl_proxy_enabled() -> bool:
    """国际站是否强制走代理（默认是，可用 CB_GATEWAY_INTL_PROXY 关闭）。"""
    value = os.environ.get(INTL_PROXY_DISABLED_VAR)
    if value is None:
        return True
    return value.strip().lower() not in _DISABLED_VALUES


def report_fallback(proxy: str, exc: BaseException) -> None:
    """记录一次兜底（节流到每分钟一行）。"""
    global _last_report_at, _fallback_count
    _fallback_count += 1
    now = time.monotonic()
    if now - _last_report_at < _REPORT_INTERVAL_SECONDS:
        return
    _last_report_at = now
    print(
        f"[upstream] 直连建连失败（{type(exc).__name__}: {exc}），改用代理重试: {proxy}",
        file=sys.stderr,
    )


class FallbackTransport(httpx.AsyncBaseTransport):
    """直连优先；建连失败时改用代理把同一个请求重发一次。

    放在传输层而不是客户端层，是为了让「新建一个 AsyncClient」的写法保持不变 ——
    聊天转发的流式与非流式两条路径都只是换了个 transport。
    """

    def __init__(self, direct: httpx.AsyncBaseTransport, proxy: str):
        self._direct = direct
        self._proxy = proxy
        self._proxy_transport: Optional[httpx.AsyncBaseTransport] = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._direct.handle_async_request(request)
        except CONNECT_FAILURES as exc:
            report_fallback(self._proxy, exc)
        if self._proxy_transport is None:
            self._proxy_transport = httpx.AsyncHTTPTransport(proxy=self._proxy)
        return await self._proxy_transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._direct.aclose()
        if self._proxy_transport is not None:
            await self._proxy_transport.aclose()


def fallback_transport() -> Optional[FallbackTransport]:
    """有代理候选时返回兜底传输层；没有候选返回 None（调用方保持原样直连）。"""
    proxy = proxy_for_retry()
    if proxy is None:
        return None
    return FallbackTransport(httpx.AsyncHTTPTransport(), proxy)


class AlwaysProxyTransport(httpx.AsyncBaseTransport):
    """一律走代理，不做直连。

    给国际站用：本机直连出口是运营商 IP，代理出口在海外。多个国际版账号在直连出口下
    被上游按账号拒绝（403 + code 11140，官方客户端同样报错），而请求体、模型、token、
    指纹头逐项排除后都不成立 —— 出站 IP 是唯一未被排除的变量。

    与 FallbackTransport 的区别是**不先直连**：直连那一次本身就是被拒绝的那一次，
    先试一遍只是白白多一次失败往返。
    """

    def __init__(self, proxy: str):
        self._proxy = proxy
        self._transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(proxy=proxy)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def transport_for(account: Optional[dict] = None) -> Optional[httpx.AsyncBaseTransport]:
    """按账号站点选出站传输层。

    - 国际版账号：有代理候选就一律走代理（见 AlwaysProxyTransport）；
    - 其余（国内站、自定义 relay）：保持直连优先 + 建连失败兜底；
    - 没有代理候选：返回 None，调用方照旧直连（行为与以前完全一致）。
    """
    proxy = proxy_for_retry()
    if proxy is None:
        return None
    if intl_proxy_enabled() and sites.is_intl_domain((account or {}).get("domain")):
        return AlwaysProxyTransport(proxy)
    return FallbackTransport(httpx.AsyncHTTPTransport(), proxy)
