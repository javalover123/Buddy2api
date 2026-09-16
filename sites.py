"""站点判定：一个账号属于哪个上游站点。

国内版与国际版的凭证、模型集都互不通用（实测国际站 18 个模型、国内站 29 个，
交集只有 3 个），所以「这个账号属于哪个站」必须只有一处定义。

历史上这里分叉过：`auth_manager.backend_url_for` 按 `.cn` 后缀选上游域名，
而 `fingerprint.origin_for` 按域名里有没有 "workbuddy" 选 Origin/Referer。
两者对 `www.workbuddy.cn` 给出不同答案 —— 请求发往国内站，请求头却自称国际站。
现在两处都走这里，不会再各自漂移。

客户端写进 auth 文件的国内域名不止一个（实测 www.workbuddy.cn、www.codebuddy.cn），
所以按 `.cn` 后缀整体判定，将来多一个国内域名也不会漏。
"""

from __future__ import annotations

_CN_SUFFIX = ".cn"

# 站点分组名。路由偏好（settings.model_site_preference）用这两个值表达「优先用哪边的
# 账号」，所以字符串必须只有一处定义。
SITE_INTERNATIONAL = "international"
SITE_DOMESTIC = "domestic"
SITE_GROUPS = (SITE_INTERNATIONAL, SITE_DOMESTIC)


def normalize_domain(value) -> str:
    """把 auth 文件里的 domain 归一成裸主机名。

    容忍大小写、协议头、路径与尾斜杠：`HTTPS://WWW.CodeBuddy.CN/` → `www.codebuddy.cn`。
    """
    text = str(value or "").strip().lower().rstrip("/")
    text = text.removeprefix("https://").removeprefix("http://")
    return text.split("/")[0]


def _host_only(host: str) -> str:
    """只看主机名，去掉端口。IPv6 字面量（含多个冒号）原样返回。"""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_cn_domain(domain) -> bool:
    """是否国内站点。

    必须是完整的 `.cn` 后缀，因此 `foocn.com`、`www.workbuddy.cn.evil.com`
    都不会被误判成国内站。
    """
    return _host_only(normalize_domain(domain)).endswith(_CN_SUFFIX)


def site_group(domain) -> str:
    """账号属于哪一组站点。

    只看域名后缀，不看模型：同一个模型在两边的计费不同（实测 deepseek-v4.1-flash
    国际站免费、国内站扣额度），所以「这个账号要花钱吗」等价于「它属于哪一组」。
    domain 为空（非 WorkBuddy 通道、或 auth 文件没写 domain）时算国际站，与
    `backend_url_for` 的回退方向保持一致。
    """
    return SITE_DOMESTIC if is_cn_domain(domain) else SITE_INTERNATIONAL


def site_url(domain) -> str | None:
    """账号所属站点的完整 URL；非国内站返回 None，由调用方决定默认值。

    只对国内后缀做特判、不动国际版账号，这样自定义 relay 对默认站点依然生效。
    """
    host = normalize_domain(domain)
    if not is_cn_domain(host):
        return None
    return f"https://{host}"
