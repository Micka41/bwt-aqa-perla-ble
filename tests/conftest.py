"""Fixtures et stubs pour tester bwt_aqa_perla_ble sans installer Home Assistant.

Injecte des modules factices dans sys.modules AVANT l'import du composant,
afin que coordinator.py / sensor.py s'importent tels quels.
"""
from __future__ import annotations

import dataclasses
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Horloge contrôlable ──────────────────────────────────────────────────────

class FakeClock:
    """Horloge pilotable : remplace dt_util.now() dans les tests."""

    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.moment

    def set(self, **kwargs) -> None:
        self.moment = self.moment.replace(**kwargs)

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


CLOCK = FakeClock()


# ── Stubs des modules externes ───────────────────────────────────────────────

def _install_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    def mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    # -- bleak --
    bleak = mod("bleak")
    class BleakClient:  # noqa: D401
        def __init__(self, *a, **kw): ...
    bleak.BleakClient = BleakClient

    bleak_exc = mod("bleak.exc")
    class BleakError(Exception): ...
    bleak_exc.BleakError = BleakError
    bleak.exc = bleak_exc

    brc = mod("bleak_retry_connector")
    brc.establish_connection = AsyncMock()

    # -- homeassistant --
    mod("homeassistant")

    ha_core = mod("homeassistant.core")
    class HomeAssistant: ...
    class ServiceCall: ...
    class SupportsResponse:
        ONLY = "only"
    ha_core.HomeAssistant = HomeAssistant
    ha_core.ServiceCall = ServiceCall
    ha_core.SupportsResponse = SupportsResponse

    ha_const = mod("homeassistant.const")
    ha_const.PERCENTAGE = "%"
    class _Unit(str): ...
    class UnitOfMass:
        KILOGRAMS = "kg"
    class UnitOfVolume:
        LITERS = "L"
    class Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
    ha_const.UnitOfMass = UnitOfMass
    ha_const.UnitOfVolume = UnitOfVolume
    ha_const.Platform = Platform

    ha_ce = mod("homeassistant.config_entries")
    class ConfigEntry: ...
    class ConfigFlow:
        def __init_subclass__(cls, **kw): ...
    ha_ce.ConfigEntry = ConfigEntry
    ha_ce.ConfigFlow = ConfigFlow

    # helpers
    mod("homeassistant.helpers")

    ha_uc = mod("homeassistant.helpers.update_coordinator")
    class UpdateFailed(Exception): ...
    class DataUpdateCoordinator:
        def __init__(self, hass, logger, name=None, update_interval=None):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None
        def __class_getitem__(cls, item):
            return cls
    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator
        def __class_getitem__(cls, item):
            return cls
    ha_uc.UpdateFailed = UpdateFailed
    ha_uc.DataUpdateCoordinator = DataUpdateCoordinator
    ha_uc.CoordinatorEntity = CoordinatorEntity

    ha_dr = mod("homeassistant.helpers.device_registry")
    ha_dr.CONNECTION_BLUETOOTH = "bluetooth"
    class DeviceInfo(dict): ...
    ha_dr.DeviceInfo = DeviceInfo

    ha_store = mod("homeassistant.helpers.storage")
    class Store:
        """Stockage en mémoire ; _backing simule le fichier .storage."""
        _backing: dict = {}

        def __init__(self, hass, version, key, **kw):
            self.key = key
        async def async_load(self):
            return Store._backing.get(self.key)
        async def async_save(self, data):
            Store._backing[self.key] = data
        def async_delay_save(self, fn, delay=0):
            Store._backing[self.key] = fn()
        async def async_remove(self):
            Store._backing.pop(self.key, None)
        @classmethod
        def reset(cls):
            cls._backing = {}
    ha_store.Store = Store

    ha_ent = mod("homeassistant.helpers.entity")
    class EntityCategory:
        DIAGNOSTIC = "diagnostic"
        CONFIG = "config"
    ha_ent.EntityCategory = EntityCategory

    ha_ep = mod("homeassistant.helpers.entity_platform")
    ha_ep.AddEntitiesCallback = object

    # util.dt → branché sur l'horloge contrôlable
    mod("homeassistant.util")
    ha_dt = mod("homeassistant.util.dt")
    ha_dt.now = lambda: CLOCK.now()
    ha_dt.utcnow = lambda: CLOCK.now()
    sys.modules["homeassistant.util"].dt = ha_dt

    # components.bluetooth
    mod("homeassistant.components")
    ha_bt = mod("homeassistant.components.bluetooth")
    ha_bt.async_ble_device_from_address = MagicMock(return_value=object())
    ha_bt.async_discovered_service_info = MagicMock(return_value=[])
    class BluetoothServiceInfoBleak: ...
    ha_bt.BluetoothServiceInfoBleak = BluetoothServiceInfoBleak

    ha_sensor = mod("homeassistant.components.sensor")
    class SensorDeviceClass:
        WEIGHT = "weight"; WATER = "water"; DATE = "date"; TIMESTAMP = "timestamp"
    class SensorStateClass:
        MEASUREMENT = "measurement"; TOTAL = "total"; TOTAL_INCREASING = "total_increasing"
    class SensorEntity:
        _attr_has_entity_name = True

    # Doit se comporter comme une dataclass : le composant applique
    # @dataclass(frozen=True) sur ses sous-classes.
    @dataclasses.dataclass(frozen=True)
    class SensorEntityDescription:
        key: str = ""
        translation_key: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: str | None = None
        state_class: str | None = None
        icon: str | None = None
        entity_category: str | None = None
        entity_registry_enabled_default: bool = True

    ha_sensor.SensorDeviceClass = SensorDeviceClass
    ha_sensor.SensorStateClass = SensorStateClass
    ha_sensor.SensorEntity = SensorEntity
    ha_sensor.SensorEntityDescription = SensorEntityDescription

    ha_bs = mod("homeassistant.components.binary_sensor")
    class BinarySensorDeviceClass:
        PROBLEM = "problem"
    class BinarySensorEntity: ...
    @dataclasses.dataclass(frozen=True)
    class BinarySensorEntityDescription:
        key: str = ""
        translation_key: str | None = None
        device_class: str | None = None
        icon: str | None = None
        entity_category: str | None = None
        entity_registry_enabled_default: bool = True
    ha_bs.BinarySensorDeviceClass = BinarySensorDeviceClass
    ha_bs.BinarySensorEntity = BinarySensorEntity
    ha_bs.BinarySensorEntityDescription = BinarySensorEntityDescription

    ha_dc = mod("homeassistant.data_entry_flow")
    class FlowResult(dict): ...
    ha_dc.FlowResult = FlowResult


_install_stubs()

# Le composant doit être importable comme package
# Racine du dépôt : rend importable custom_components.bwt_aqa_perla_ble
COMPONENT_ROOT = Path(__file__).resolve().parent.parent
if str(COMPONENT_ROOT) not in sys.path:
    sys.path.insert(0, str(COMPONENT_ROOT))


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def clock():
    """Horloge contrôlable, réinitialisée à chaque test."""
    CLOCK.moment = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    return CLOCK


@pytest.fixture
def store():
    """Stockage persistant simulé, vidé avant chaque test."""
    from homeassistant.helpers.storage import Store
    Store.reset()
    return Store


@pytest.fixture
def coordinator(clock, store):
    """Coordinator neuf, sans BLE ni état persistant."""
    from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
    return BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")


# ── Helpers de construction de trames ────────────────────────────────────────

def make_broadcast(
    qte_sel_g: int = 36400,
    capa_kg: int = 52,
    idx_quart: int = 1000,
    idx_jour: int = 500,
    vol_rege: int = 1980,
    alarme: bool = False,
    loop_jour: bool = False,
    loop_quart: bool = False,
    version: tuple[int, int] = (1, 21),
    length: int = 15,
) -> bytes:
    """Construit une trame BROADCAST valide.

    qte_sel_g est la valeur RÉELLE souhaitée ; pour V2.x elle est multipliée
    par 4 dans la trame (le décodeur divisera).
    """
    raw = qte_sel_g * 4 if version[0] >= 2 else qte_sel_g
    flags = (
        (0x01 if alarme else 0)
        | (0x02 if loop_quart else 0)
        | (0x04 if loop_jour else 0)
    )
    buf = bytearray(length)
    buf[0] = raw & 0xFF
    buf[1] = (raw >> 8) & 0xFF
    buf[2] = (raw >> 16) & 0xFF
    buf[3] = (raw >> 24) & 0xFF
    buf[4] = idx_quart & 0xFF
    buf[5] = (idx_quart >> 8) & 0xFF
    buf[6] = idx_jour & 0xFF
    buf[7] = (idx_jour >> 8) & 0xFF
    buf[8] = vol_rege & 0xFF
    buf[9] = (vol_rege >> 8) & 0xFF
    buf[10] = capa_kg & 0xFF
    buf[11] = (capa_kg >> 8) & 0xFF
    buf[12] = flags
    buf[13] = version[0]
    buf[14] = version[1]
    return bytes(buf)


def make_notification(values: list[int], index: int = 0) -> bytes:
    """Construit une trame de notification (20 octets : séquence + 9 mots).

    `index` est le numéro de séquence de la trame dans son bloc (0, 1, 2…),
    encodé en little-endian ; les données qui suivent sont en big-endian.
    """
    buf = bytearray(20)
    buf[0] = index & 0xFF
    buf[1] = (index >> 8) & 0xFF
    for i, word in enumerate(values[:9]):
        buf[2 + i * 2] = (word >> 8) & 0xFF
        buf[3 + i * 2] = word & 0xFF
    for i in range(len(values), 9):
        buf[2 + i * 2] = 0xFF
        buf[3 + i * 2] = 0xFF
    return bytes(buf)


def quart_word(litres: int, rege: bool = False) -> int:
    """Encode un mot du buffer quart (litres au litre près + bit rege)."""
    return (litres & 0x03FF) | (0x0800 if rege else 0)


def jour_word(litres: int, regens: int = 0) -> int:
    """Encode un mot du buffer journalier (litres par 10 + nb régénérations)."""
    return ((litres // 10) & 0x07FF) | ((regens & 0x03) << 12)
