"""BWT AQA Perla BLE integration for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .const import DOMAIN
from .coordinator import BwtCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_GET_TOTAL         = "get_total_consumption"
SERVICE_GET_HISTORY_CONSO = "get_history_consumption"
SERVICE_GET_HISTORY_REGEN = "get_history_regenerations"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BWT AQA Perla BLE from a config entry."""
    coordinator = BwtCoordinator(hass, entry.data["address"])
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_total(call: ServiceCall) -> dict:
        return await coordinator.service_total_consumption()

    async def handle_history_conso(call: ServiceCall) -> dict:
        return await coordinator.service_history_consumption()

    async def handle_history_regen(call: ServiceCall) -> dict:
        return await coordinator.service_history_regenerations()

    for service, handler in (
        (SERVICE_GET_TOTAL,         handle_total),
        (SERVICE_GET_HISTORY_CONSO, handle_history_conso),
        (SERVICE_GET_HISTORY_REGEN, handle_history_regen),
    ):
        hass.services.async_register(
            DOMAIN, service, handler, supports_response=SupportsResponse.ONLY
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        for service in (SERVICE_GET_TOTAL, SERVICE_GET_HISTORY_CONSO, SERVICE_GET_HISTORY_REGEN):
            hass.services.async_remove(DOMAIN, service)
    return unload_ok
