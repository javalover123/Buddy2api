"""Per-channel supplier model catalogs.

Fetch+parse of each source's list is separate from persist and from chat I/O.
Live WorkBuddy lists are also copied to the legacy `models` setting so older
readers stay in sync. Manual extras work the same way on every channel.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Awaitable, Callable

import buddy2api.database as db

CATALOG_SETTING = "channel_catalogs"
REFRESH_SETTING = "channel_catalog_refresh"
EXTRAS_SETTING = "channel_catalog_extras"

Fetcher = Callable[[dict], Awaitable[list[dict]]]


class CatalogError(ValueError):
    """Invalid channel or model id for a catalog write."""


def _load_map(key: str) -> dict:
    try:
        value = db.get_setting(key, {}) or {}
    except sqlite3.OperationalError:
        return {}
    return value if isinstance(value, dict) else {}


def stored_catalog(channel: str) -> list[dict] | None:
    items = _load_map(CATALOG_SETTING).get(channel)
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict) and item.get("id")]
    return None


def save_catalog(channel: str, models: list[dict]) -> None:
    catalogs = _load_map(CATALOG_SETTING)
    catalogs[channel] = models
    db.set_setting(CATALOG_SETTING, catalogs)


def normalize_models(rows: Any) -> list[dict]:
    if not isinstance(rows, list):
        return []
    models: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
            name = mid
            description = ""
        elif isinstance(row, dict):
            mid = str(
                row.get("id")
                or row.get("model_id")
                or row.get("model_name")
                or row.get("name")
                or ""
            ).strip()
            name = str(row.get("name") or row.get("display_id") or row.get("display_name") or mid)
            description = str(row.get("description") or "")
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item = {"id": mid, "name": name or mid}
        if description:
            item["description"] = description
        models.append(item)
    return models


def _raw_extras(channel: str) -> list:
    items = _load_map(EXTRAS_SETTING).get(channel)
    return list(items) if isinstance(items, list) else []


def _removal_markers(items: list) -> list[dict]:
    """从原始 extras 里取出「已删除」标记项（官方模型被用户删掉时留下的墓碑）。"""
    return [
        {"id": str(item.get("id")), "removed": True}
        for item in items
        if isinstance(item, dict) and item.get("removed") and item.get("id")
    ]


def extras_for(channel: str) -> list[dict]:
    """用户手动添加的模型；「已删除」标记项不算模型，单独处理。"""
    items = [
        item
        for item in _raw_extras(channel)
        if isinstance(item, dict) and not item.get("removed")
    ]
    return normalize_models(items)


def removed_ids(channel: str) -> set[str]:
    return {item["id"] for item in _removal_markers(_raw_extras(channel))}


def save_extras(channel: str, items: list) -> None:
    extras = _load_map(EXTRAS_SETTING)
    extras[channel] = list(items)
    db.set_setting(EXTRAS_SETTING, extras)


def _merge_models(base: list[dict], extra: list[dict]) -> list[dict]:
    return normalize_models(list(base) + list(extra))


def _visible_models(channel: str, base: list[dict]) -> list[dict]:
    """官方/回退目录 + 手动项，并剔除用户删掉的（带墓碑的）模型。"""
    hidden = removed_ids(channel)
    if hidden:
        base = [
            item
            for item in base
            if str((item.get("id") if isinstance(item, dict) else item) or "") not in hidden
        ]
    return _merge_models(base, extras_for(channel))


def models_for(channel: str, fallback: list[dict]) -> list[dict]:
    stored = stored_catalog(channel)
    base = stored if stored else list(fallback)
    return _visible_models(channel, base)


def _with_manual(channel: str, models: list[dict]) -> list[dict]:
    extra_ids = {str(item.get("id")) for item in extras_for(channel)}
    annotated = []
    for item in models:
        row = dict(item)
        row["manual"] = str(row.get("id") or "") in extra_ids
        annotated.append(row)
    return annotated


def _hidden_count(channel: str) -> int:
    """被用户删除（打上墓碑）的官方模型数量，UI 用它显示「已删除 N」。"""
    return len(removed_ids(channel))


def _normalize_model_id(channel: str, model_id: str) -> str:
    mid = str(model_id or "").strip()
    prefix = f"{channel}/"
    if mid.startswith(prefix):
        mid = mid[len(prefix) :].strip()
    return mid


def _require_enabled_channel(channel: str) -> str:
    import buddy2api.providers as providers

    value = str(channel or "").strip()
    if not value or not providers.is_channel_enabled(value):
        raise CatalogError("unknown or disabled channel")
    return value


def current_models(channel: str) -> list[dict]:
    import buddy2api.providers as providers

    provider = providers.get_provider(channel)
    if provider is not None:
        return list(provider.list_models())
    if channel == "workbuddy":
        return workbuddy_fallback_models()
    return extras_for(channel)


def upsert_model(channel: str, model_id: str, name: str = "") -> dict:
    channel = _require_enabled_channel(channel)
    mid = _normalize_model_id(channel, model_id)
    if not mid:
        raise CatalogError("model id is required")
    label = str(name or "").strip() or mid
    raw = _raw_extras(channel)
    markers = _removal_markers(raw)
    manual = extras_for(channel)

    # 之前被删除的官方模型：重新添加即恢复（去掉墓碑），而不是变成手动项。
    if any(marker["id"] == mid for marker in markers):
        markers = [marker for marker in markers if marker["id"] != mid]
        save_extras(channel, markers + manual)
        visible = {
            str(item.get("id"))
            for item in current_models(channel)
            if isinstance(item, dict)
        }
        if mid not in visible:
            # 上游目录已经不提供它了：作为手动项保底，否则这次「恢复」等于什么都没做。
            manual = [item for item in manual if item.get("id") != mid]
            manual.append({"id": mid, "name": label})
            save_extras(channel, markers + manual)
        models = current_models(channel)
        return {
            "channel": channel,
            "id": mid,
            "name": label,
            "count": len(models),
            "models": _with_manual(channel, models),
            "updated": True,
            "restored": True,
        }

    extra_ids = {str(item.get("id")) for item in manual}
    current_ids = {
        str(item.get("id")) for item in current_models(channel) if isinstance(item, dict)
    }
    if mid in current_ids and mid not in extra_ids:
        raise CatalogError("model already exists in this channel")
    found = False
    for item in manual:
        if item.get("id") == mid:
            item["name"] = label
            found = True
            break
    if not found:
        manual.append({"id": mid, "name": label})
    save_extras(channel, markers + manual)
    models = current_models(channel)
    return {
        "channel": channel,
        "id": mid,
        "name": label,
        "count": len(models),
        "models": _with_manual(channel, models),
        "updated": found,
    }


def remove_model(channel: str, model_id: str) -> dict:
    """删除模型：手动项直接移除，官方项留「墓碑」——一键读取也不会把它带回来。"""
    channel = _require_enabled_channel(channel)
    mid = _normalize_model_id(channel, model_id)
    if not mid:
        raise CatalogError("model id is required")
    raw = _raw_extras(channel)
    markers = _removal_markers(raw)
    manual = extras_for(channel)
    manual_ids = {str(item.get("id")) for item in manual}
    visible_ids = {
        str(item.get("id")) for item in current_models(channel) if isinstance(item, dict)
    }
    official_ids = visible_ids - manual_ids

    if mid in manual_ids:
        manual = [item for item in manual if item.get("id") != mid]
    if mid in official_ids or any(marker["id"] == mid for marker in markers):
        markers.append({"id": mid, "removed": True})
    elif mid not in manual_ids:
        raise CatalogError("model not found")

    deduped: list[dict] = []
    seen: set[str] = set()
    for marker in markers:
        if marker["id"] in seen:
            continue
        seen.add(marker["id"])
        deduped.append(marker)
    save_extras(channel, deduped + manual)
    models = current_models(channel)
    return {
        "channel": channel,
        "id": mid,
        "count": len(models),
        "models": _with_manual(channel, models),
    }


def workbuddy_fallback_models() -> list[dict]:
    import buddy2api.proxy as proxy

    try:
        models = db.get_setting("models", proxy.DEFAULT_MODELS)
    except sqlite3.OperationalError:
        return list(proxy.DEFAULT_MODELS)
    if isinstance(models, list) and models:
        return models
    return list(proxy.DEFAULT_MODELS)


def _fallback_models(channel: str, provider) -> list[dict]:
    if provider is not None:
        try:
            return list(provider.list_models())
        except Exception:
            pass
    if channel == "workbuddy":
        return workbuddy_fallback_models()
    return extras_for(channel)


def _status_row(
    channel: str,
    *,
    mode: str,
    models: list[dict],
    message: str = "",
    display_name: str = "",
) -> dict:
    return {
        "channel": channel,
        "display_name": display_name or channel,
        "mode": mode,
        "message": message,
        "count": len(models),
        "hidden_count": _hidden_count(channel),
        "models": _with_manual(channel, models),
        "updated_at": int(time.time()),
    }


async def _pick_account(provider) -> dict | None:
    if provider is None:
        return None
    picker = getattr(provider, "pick_account_with_fallback", None)
    if picker is None:
        return None
    return await picker()


async def _catalog_accounts(provider) -> list[dict]:
    """刷新目录时采样哪些账号。

    默认只取一个（与历史行为一致）。通道可以覆盖成「全部活跃账号」—— 国内站与国际站
    的模型集几乎不重叠，只采样一个账号会让另一个站的模型整体从 /v1/models 里消失，
    客户端也就再也请求不到它们了。
    """
    if provider is None:
        return []
    hook = getattr(provider, "catalog_accounts", None)
    if hook is not None:
        accounts = [a for a in await hook() if isinstance(a, dict)]
        if accounts:
            return accounts
    account = await _pick_account(provider)
    return [account] if account else []


async def _fetch_qclaw(account: dict) -> list[dict]:
    from buddy2api.providers.qclaw.jprx import fetch_supplier_models

    return await fetch_supplier_models(account)


async def _fetch_traework(account: dict) -> list[dict]:
    from buddy2api.providers.traework.models import fetch_supplier_models

    return await fetch_supplier_models(account)


async def _fetch_qwenwork(account: dict) -> list[dict]:
    from buddy2api.providers.qwenwork.models import fetch_supplier_models

    return await fetch_supplier_models(account)


async def _fetch_workbuddy(account: dict) -> list[dict]:
    from buddy2api.providers.workbuddy.models import fetch_supplier_models

    return await fetch_supplier_models(account)


LIVE_FETCHERS: dict[str, Fetcher] = {
    "qclaw": _fetch_qclaw,
    "traework": _fetch_traework,
    "qwenwork": _fetch_qwenwork,
    "workbuddy": _fetch_workbuddy,
}


async def refresh_one(channel: str) -> dict:
    import buddy2api.providers as providers

    provider = providers.get_provider(channel)
    display_name = getattr(provider, "display_name", channel) if provider else channel
    fallback = _fallback_models(channel, provider)
    fetcher = LIVE_FETCHERS.get(channel)
    if fetcher is None:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="no supplier-list API",
            display_name=display_name,
        )
    accounts = await _catalog_accounts(provider)
    if not accounts:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="no usable account",
            display_name=display_name,
        )

    fetched: list[dict] = []
    failures: list[str] = []
    for account in accounts:
        try:
            account_models = normalize_models(await fetcher(account))
        except Exception as exc:
            failures.append(str(exc)[:120])
            continue
        if not account_models:
            continue
        # 把「这个账号能服务哪些模型」写回路由层：混装国内/国际账号时，这是把请求
        # 发给正确账号的唯一依据（模型目录本身是并集，看不出账号差异）。
        record = getattr(provider, "record_account_models", None)
        if record is not None:
            record(account, [item["id"] for item in account_models])
        fetched = _merge_models(fetched, account_models)

    if not fetched:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="; ".join(failures)[:240] or "empty supplier list",
            display_name=display_name,
        )
    save_catalog(channel, fetched)
    if channel == "workbuddy":
        db.set_setting("models", fetched)
    return _status_row(
        channel,
        mode="live",
        models=_visible_models(channel, fetched),
        message="",
        display_name=display_name,
    )


async def refresh_supplier_catalogs() -> dict:
    import buddy2api.providers as providers

    sources = []
    for channel in providers.enabled_provider_ids():
        sources.append(await refresh_one(channel))
    db.set_setting(
        REFRESH_SETTING,
        {
            item["channel"]: {
                "mode": item["mode"],
                "message": item["message"],
                "count": item["count"],
                "updated_at": item["updated_at"],
            }
            for item in sources
        },
    )
    return {"sources": sources}


def catalog_snapshot() -> dict:
    import buddy2api.providers as providers

    refresh = _load_map(REFRESH_SETTING)
    sources = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        if provider is not None:
            models = list(provider.list_models())
        elif channel == "workbuddy":
            models = workbuddy_fallback_models()
        else:
            models = []
        meta = refresh.get(channel) if isinstance(refresh.get(channel), dict) else {}
        sources.append(
            {
                "channel": channel,
                "display_name": getattr(provider, "display_name", channel) if provider else channel,
                "mode": meta.get("mode") or ("fallback" if channel not in LIVE_FETCHERS else "static"),
                "message": meta.get("message") or "",
                "count": len(models),
                "hidden_count": _hidden_count(channel),
                "models": _with_manual(channel, models),
                "updated_at": meta.get("updated_at"),
            }
        )
    return {"sources": sources}
