"""Sensor platform for BWT AQA Perla BLE."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    KEY_SALT_PCT,
    KEY_SALT_KG,
    KEY_SALT_TOTAL_KG,
    KEY_SALT_ALARM,
    KEY_CONSUMPTION_TODAY,
    KEY_CONSUMPTION_YESTERDAY,
    KEY_CONSUMPTION_WEEK,
    KEY_REGEN_TODAY,
    KEY_REGEN_YESTERDAY,
    KEY_SALT_AUTONOMY_DAYS,
    KEY_SALT_AUTONOMY_WEEKS,
    KEY_AVG_DAILY_30D,
    KEY_LAST_SYNC,
    KEY_FIRMWARE,
)
from .coordinator import BwtCoordinator


@dataclass(frozen=True)
class BwtSensorEntityDescription(SensorEntityDescription):
    """Describes a BWT sensor entity."""


SENSORS: tuple[BwtSensorEntityDescription, ...] = (
    BwtSensorEntityDescription(
        key=KEY_SALT_PCT,
        name="Niveau de sel",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:shaker-outline",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_SALT_KG,
        name="Sel restant",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        icon="mdi:weight-kilogram",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_SALT_TOTAL_KG,
        name="Capacité sel",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        icon="mdi:weight-kilogram",
        entity_registry_enabled_default=False,
    ),
    BwtSensorEntityDescription(
        key=KEY_CONSUMPTION_TODAY,
        name="Consommation aujourd'hui",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        icon="mdi:water",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    BwtSensorEntityDescription(
        key=KEY_CONSUMPTION_YESTERDAY,
        name="Consommation hier",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        icon="mdi:water-outline",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_CONSUMPTION_WEEK,
        name="Consommation 7 jours",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        icon="mdi:chart-bar",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_REGEN_TODAY,
        name="Régénérations aujourd'hui",
        native_unit_of_measurement="régénérations",
        icon="mdi:refresh-circle",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_REGEN_YESTERDAY,
        name="Régénérations hier",
        native_unit_of_measurement="régénérations",
        icon="mdi:refresh",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_SALT_AUTONOMY_DAYS,
        name="Autonomie sel estimée (jours)",
        native_unit_of_measurement="j",
        icon="mdi:calendar-today",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_SALT_AUTONOMY_WEEKS,
        name="Autonomie sel estimée",
        native_unit_of_measurement="sem",
        icon="mdi:calendar-clock",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_AVG_DAILY_30D,
        name="Consommation moyenne (30 jours)",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        icon="mdi:chart-bell-curve",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    BwtSensorEntityDescription(
        key=KEY_LAST_SYNC,
        name="Dernière synchronisation",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:bluetooth-connect",
    ),
    BwtSensorEntityDescription(
        key=KEY_FIRMWARE,
        name="Firmware",
        icon="mdi:chip",
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BWT AQA Perla BLE sensors from a config entry."""
    coordinator: BwtCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BwtSensor(coordinator, entry, desc) for desc in SENSORS])


class BwtSensor(CoordinatorEntity[BwtCoordinator], SensorEntity):
    """Representation of a BWT AQA Perla sensor."""

    entity_description: BwtSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BwtCoordinator,
        entry: ConfigEntry,
        description: BwtSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id   = f"{coordinator.address}_{description.key}"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.key)


def _device_info(coordinator: BwtCoordinator, entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        name=entry.data.get("name", f"BWT AQA Perla BLE ({coordinator.address})"),
        manufacturer="BWT",
        model="AQA Perla",
    )
