"""Config flow for BWT AQA Perla BLE."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from .const import DOMAIN, UUID_SERVICE, RECO_BWT

_LOGGER = logging.getLogger(__name__)


def _is_bwt_device(discovery_info: BluetoothServiceInfoBleak) -> bool:
    """
    Vérifie qu'il s'agit bien d'un BWT AQA Perla.

    Le BWT s'annonce comme iBeacon Apple (manufacturer_id=0x004C=76), SANS nom.
    Le payload iBeacon contient RECO_BWT = b"BestWaterTechno\0" comme UUID.

    Trois critères (OR) :
      1. Manufacturer data (company 0x004C) contient RECO_BWT
      2. Nom contient "BWT" (appareils futurs ou firmware différent)
      3. UUID service BWT présent (si annoncé)
    """
    # Critère 1 : iBeacon Apple contenant RECO_BWT (cas normal)
    if discovery_info.manufacturer_data:
        for data in discovery_info.manufacturer_data.values():
            if RECO_BWT in data:
                return True

    # Critère 2 : nom contient "BWT"
    nom = (discovery_info.name or "").upper()
    if "BWT" in nom:
        return True

    # Critère 3 : UUID service BWT
    if any(
        UUID_SERVICE.lower() in str(u).lower()
        for u in discovery_info.service_uuids
    ):
        return True

    return False


class BwtConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BWT AQA Perla BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Déclenché automatiquement par HA quand local_name matche les filtres."""
        _LOGGER.info(
            "Appareil BLE détecté : nom=%r  adresse=%s  RSSI=%s  services=%s",
            discovery_info.name,
            discovery_info.address,
            getattr(discovery_info, "rssi", "?"),
            list(discovery_info.service_uuids),
        )

        # Validation stricte avant de proposer l'ajout
        if not _is_bwt_device(discovery_info):
            _LOGGER.debug(
                "Appareil %s (%r) ignoré — ne correspond pas aux critères BWT",
                discovery_info.address, discovery_info.name,
            )
            return self.async_abort(reason="not_supported")

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or f"BWT AQA Perla BLE ({discovery_info.address})"

        _LOGGER.info(
            "BWT AQA Perla BLE confirmé : %s (%s)",
            self._name, self._address,
        )
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Demande confirmation à l'utilisateur."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={"address": self._address, "name": self._name},
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name":    self._name,
                "address": self._address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ajout manuel (fallback si l'auto-découverte n'a pas fonctionné)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input["address"].strip().upper()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"BWT AQA Perla BLE ({address})",
                data={"address": address, "name": f"BWT AQA Perla BLE ({address})"},
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("address"): str}),
            errors=errors,
        )
