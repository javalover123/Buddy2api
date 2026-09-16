"""按模型配置站点偏好。

同一个模型在国内站与国际站的计费不同（实测 deepseek-v4.1-flash 国际站免费、
国内站扣额度），所以「优先用哪边的账号」必须能按模型配置，不能写死在选号逻辑里。

设置存在 `settings.model_site_preference`：

    {"default": "international", "models": {"deepseek-v4.1-flash": "international"}}

两组取值是 `sites.SITE_GROUPS`（international / domestic）；模型值为空串表示该模型
不区分站点。`default` 只作用于没有单独配置的模型。

路由侧只把它当优先级用（见 `auth_manager.pick_account`）：偏好站点有可用账号就先用，
没有或都试过了就退回其余账号，绝不会因为偏好配置而让请求无账号可用。
"""

from __future__ import annotations

import sites


class SitePreferenceError(ValueError):
    """站点偏好取值非法。"""


def clean(raw) -> dict:
    """把任意输入规范成 {default: str, models: {模型: str}}，非法值抛错。

    与 `auth_manager.model_site_preference()` 的区别：那个是读取侧的容错版本（配置写坏
    也不能让路由崩），这个是写入侧的严格版本（管理页/API 传错要明确报 400）。
    """
    if not isinstance(raw, dict):
        raise SitePreferenceError("site preference must be an object")
    unknown = set(raw) - {"default", "models"}
    if unknown:
        raise SitePreferenceError(f"unsupported keys: {', '.join(sorted(unknown))}")

    default = ""
    if "default" in raw:
        default = _site(raw["default"], "default")

    models_raw = raw.get("models", {})
    if not isinstance(models_raw, dict):
        raise SitePreferenceError("models must be an object")
    models: dict[str, str] = {}
    for key, value in models_raw.items():
        model = str(key or "").strip()
        if not model:
            raise SitePreferenceError("model id is required")
        models[model] = _site(value, f"models.{model}")

    return {"default": default, "models": models}


def _site(value, field: str) -> str:
    text = str(value or "").strip().lower()
    if text == "":
        return ""
    if text not in sites.SITE_GROUPS:
        raise SitePreferenceError(
            f"{field} must be one of: {', '.join(sites.SITE_GROUPS)} (or empty)"
        )
    return text


def snapshot() -> dict:
    """给管理页用：当前生效的偏好 + 可选值。"""
    import auth_manager

    current = auth_manager.model_site_preference()
    return {
        "default": current["default"],
        "models": current["models"],
        "site_groups": list(sites.SITE_GROUPS),
    }
