"""Binary sensor platform for BWT AQA Perla BLE."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, KEY_SALT_ALARM
from .coordinator import BwtCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BWT AQA Perla BLE binary sensors from a config entry."""
    coordinator: BwtCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BwtAlarmBinarySensor(coordinator, entry)])


class BwtAlarmBinarySensor(CoordinatorEntity[BwtCoordinator], BinarySensorEntity):
    """
    Alarme sel — device_class: problem.
    is_on = True  → alarme active (problème)
    is_on = False → OK
    """

    _attr_has_entity_name = True
    _attr_name            = "Alarme sel"
    _attr_device_class    = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: BwtCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id   = f"{coordinator.address}_{KEY_SALT_ALARM}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name=entry.data.get("name", f"BWT AQA Perla BLE ({coordinator.address})"),
            manufacturer="BWT",
            model="AQA Perla",
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.get(KEY_SALT_ALARM))

    @property
    def icon(self) -> str:
        return "mdi:alert" if self.is_on else "mdi:check-circle"
