"""TraeWork check-in and official credit usage."""

from __future__ import annotations

import httpx

from buddy2api.providers.protocol import QuotaSnapshot
from buddy2api.providers.traework.constants import (
    CHANNEL_ID,
    CHECKIN_CLAIM_PATH,
    CHECKIN_STATUS_PATH,
    REQ_SOURCE,
    UG_API,
    USAGE_PATH,
)
from buddy2api.providers.traework.token import auth_headers, extra_of


def _host(account: dict) -> str:
    extra = extra_of(account)
    return str(extra.get("host") or UG_API).rstrip("/") or UG_API


def _checkin_row(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    claimed: bool = False,
    already_claimed: bool = False,
    credit: float = 0,
    today_checked_in: bool | None = None,
    extra: dict | None = None,
) -> dict:
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": True,
        "today_checked_in": already_claimed if today_checked_in is None else today_checked_in,
        "today_credit": credit,
        "channel": CHANNEL_ID,
        **(extra or {}),
    }


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    url = f"{_host(account)}{CHECKIN_STATUS_PATH}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers=auth_headers(account), json={})
    except httpx.HTTPError as exc:
        return _checkin_row(account, ok=False, message=str(exc)[:240])
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        return _checkin_row(
            account,
            ok=False,
            status_code=response.status_code,
            message=str((data or {}).get("message") or f"HTTP {response.status_code}")[:240],
        )
    checked = bool(data.get("checked_in") or data.get("checkedIn"))
    credit = float(data.get("credits") or data.get("credit") or 0)
    return _checkin_row(
        account,
        ok=True,
        status_code=response.status_code,
        already_claimed=checked,
        today_checked_in=checked,
        credit=credit,
        message=str(data.get("message") or "success"),
        extra={"enable": bool(data.get("enable", True))},
    )


async def claim_checkin(account: dict) -> dict:
    status = await fetch_checkin(account, force=True)
    if not status.get("ok"):
        return status
    if status.get("already_claimed") or status.get("today_checked_in"):
        status["already_claimed"] = True
        status["message"] = "今日已领取"
        return status
    url = f"{_host(account)}{CHECKIN_CLAIM_PATH}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=auth_headers(account), json={})
    except httpx.HTTPError as exc:
        return _checkin_row(account, ok=False, message=str(exc)[:240])
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        return _checkin_row(
            account,
            ok=False,
            status_code=response.status_code,
            message=str((data or {}).get("message") or f"HTTP {response.status_code}")[:240],
        )
    credit = float(data.get("credits") or data.get("credit") or status.get("credit") or 0)
    return _checkin_row(
        account,
        ok=True,
        status_code=response.status_code,
        claimed=True,
        credit=credit,
        message=str(data.get("message") or "success"),
    )


async def fetch_quota(account: dict) -> QuotaSnapshot:
    url = f"{_host(account)}{USAGE_PATH}"
    body = {"require_usage": True, "req_source": REQ_SOURCE}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=auth_headers(account), json=body)
    except httpx.HTTPError as exc:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            message=str(exc)[:240],
        )
    if response.status_code >= 400:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            message=f"HTTP {response.status_code}",
        )
    try:
        data = response.json()
    except ValueError:
        data = {}
    usage = data.get("usage_summary") if isinstance(data.get("usage_summary"), dict) else {}
    total = usage.get("total_amount")
    consumed = usage.get("consumed_amount")
    remaining = None
    if isinstance(total, (int, float)):
        remaining = float(total) - float(consumed or 0)
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=int(account.get("id") or 0),
        unit="credit",
        remaining=remaining,
        extra={"consumed": consumed, "total": total},
        unsupported=remaining is None,
        message="" if remaining is not None else "quota unit unknown",
    )
