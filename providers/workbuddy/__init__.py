"""WorkBuddy provider facade. Implementation stays in proxy.py / auth_manager.py."""

from __future__ import annotations

from typing import Optional

import auth_manager
import database as db
import proxy
from providers.protocol import ChannelId


class WorkBuddyProvider:
    id: ChannelId = "workbuddy"
    display_name = "WorkBuddy / CodeBuddy"
    checkin_supported = True

    def list_models(self) -> list[dict]:
        import catalog

        return catalog.models_for(self.id, catalog.workbuddy_fallback_models())

    def alias_map(self) -> dict[str, str]:
        import aliases

        return aliases.merged_map(self.id)

    def accepts_model(self, inner: str) -> bool:
        ids = {str(item.get("id")) for item in self.list_models() if isinstance(item, dict)}
        return inner in ids or inner in self.alias_map()

    def translate_model(self, model: str) -> str:
        import aliases

        return aliases.resolve(self.id, model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None, model: str | None = None
    ) -> Optional[dict]:
        return await auth_manager.pick_account_with_fallback(
            exclude_ids, provider=self.id, model=model
        )

    async def catalog_accounts(self) -> list[dict]:
        """刷新模型目录时采样本通道全部活跃账号。

        国内站与国际站暴露的模型集几乎不重叠（实测国际站 18 个、国内站 29 个，
        交集只有 3 个）。只采样一个账号会让另一个站的模型整体从 /v1/models 里消失，
        客户端也就请求不到它们了。
        """
        return [
            account
            for account in db.list_accounts(provider=self.id)
            if account.get("status") == "active"
        ]

    def record_account_models(self, account: dict, model_ids) -> None:
        """把账号的模型能力写回路由层，供 pick_account 按模型过滤候选账号。"""
        aid = account.get("id") if isinstance(account, dict) else None
        if aid is None:
            return
        auth_manager.record_account_models(int(aid), model_ids)

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        log_model = None
        info = api_key_info
        if isinstance(api_key_info, dict) and "_log_model" in api_key_info:
            log_model = api_key_info.get("_log_model")
            info = {k: v for k, v in api_key_info.items() if k != "_log_model"} or None
        return await proxy.proxy_chat_completions(payload, info, log_model=log_model)


PROVIDER = WorkBuddyProvider()
