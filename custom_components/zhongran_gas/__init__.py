"""集成入口。"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import GasCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]

type GasConfigEntry = ConfigEntry[GasCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: GasConfigEntry) -> bool:
    """配置项加载。"""
    coordinator = GasCoordinator(hass, entry)
    await coordinator.async_load_storage()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 选项变了（比如改了刷新间隔）就重载
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GasConfigEntry) -> bool:
    """卸载。"""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_options(hass: HomeAssistant, entry: GasConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
