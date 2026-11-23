"""同步状态二元传感器。"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GasCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GasCoordinator = entry.runtime_data
    async_add_entities([GasSyncSensor(coordinator, entry)])


class GasSyncSensor(CoordinatorEntity[GasCoordinator], BinarySensorEntity):
    """上一次拉取是否成功。"""

    _attr_has_entity_name = False
    _attr_name = "同步状态"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator: GasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_sync_ok"
        self.entity_id = "binary_sensor.gas_sync_ok"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "中国燃气",
            "model": "中燃在线",
        }

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success
