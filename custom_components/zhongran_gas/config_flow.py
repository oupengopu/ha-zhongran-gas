"""配置流程：UI 里填 token / 户号 / 姓名 / mas。"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import GasApiError, ZhongranGasApi
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("access_token"): str,
        vol.Required("user_id"): str,
        vol.Required("cust_code"): str,
        vol.Required("cust_name"): str,
        vol.Required("mas"): str,
        vol.Optional("scan_interval", default=DEFAULT_SCAN_INTERVAL): vol.All(
            int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
        ),
    }
)


class ZhongranGasConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """首次添加。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # 唯一性：同一个户号只能加一次
            await self.async_set_unique_id(user_input["cust_code"])
            self._abort_if_unique_id_configured()

            api = ZhongranGasApi(
                async_get_clientsession(self.hass),
                access_token=user_input["access_token"],
                user_id=user_input["user_id"],
                cust_code=user_input["cust_code"],
                cust_name=user_input["cust_name"],
                mas=user_input["mas"],
            )
            try:
                info = await api.get_cust_info()
            except GasApiError as err:
                _LOGGER.warning("验证失败: %s", err)
                msg = str(err)
                if "token" in msg.lower() or "登录" in msg or "鉴权" in msg:
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
            else:
                # 标题不要带余额 —— 否则 entity_id 会变成
                # sensor.ran_qi_mou_ji_xia_yu_e_180_07_xxx 这种拼音串
                title = f"燃气 {user_input['cust_name']}"
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={
                "howto": "用抓包工具从「中燃在线」小程序抄这几个值"
            },
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """修改刷新间隔 / 更新过期凭据。"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            data: dict[str, Any] = {
                "scan_interval": user_input["scan_interval"]
            }
            # 凭据留空 = 不修改；非空则覆盖（coordinator 里 options 优先于 data）
            for key in ("access_token", "user_id", "mas"):
                new_value = (user_input.get(key) or "").strip()
                old_value = self.config_entry.options.get(key)
                if new_value:
                    data[key] = new_value
                elif old_value:
                    data[key] = old_value
            return self.async_create_entry(title="", data=data)

        cur = self.config_entry.options.get(
            "scan_interval", DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("scan_interval", default=cur): vol.All(
                        int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                    vol.Optional("access_token", default=""): str,
                    vol.Optional("user_id", default=""): str,
                    vol.Optional("mas", default=""): str,
                }
            ),
            description_placeholders={
                "note": "凭据留空表示保持不变；accessToken 过期时只需粘贴新值，保存后自动重载"
            },
        )
