"""Tests du décodage protocole BLE : BROADCAST, notifications, commandes."""
import pytest

from conftest import make_broadcast, make_notification, quart_word, jour_word
from custom_components.bwt_aqa_perla_ble.coordinator import (
    _build_break_cmd,
    _build_read_cmd,
    _decode_broadcast,
    _decode_notification,
    _get_word_from,
    _get_word_le,
)


# ── Helpers bas niveau ───────────────────────────────────────────────────────

def test_get_word_le():
    assert _get_word_le(bytes([0x34, 0x12]), 0) == 0x1234
    assert _get_word_le(bytes([0xFF, 0xFF]), 0) == 65535
    assert _get_word_le(bytes([0x00, 0x00]), 0) == 0


def test_get_word_from_both_orders():
    buf = bytes([0x12, 0x34])
    assert _get_word_from(buf, 0, first_min=True) == 0x3412
    assert _get_word_from(buf, 0, first_min=False) == 0x1234


# ── BROADCAST ────────────────────────────────────────────────────────────────

def test_decode_broadcast_nominal():
    b = make_broadcast(qte_sel_g=36400, capa_kg=52, idx_quart=1963, idx_jour=920)
    r = _decode_broadcast(b)
    assert r["qte_sel_restant"] == 36400
    assert r["capa_total_sel"] == 52000
    assert r["index_tab_quart"] == 1963
    assert r["index_tab_jour"] == 920
    assert r["pourcentage_sel"] == 70
    assert r["version"] == "A22X V1.21"


def test_decode_broadcast_too_short_raises():
    from homeassistant.helpers.update_coordinator import UpdateFailed
    with pytest.raises(UpdateFailed):
        _decode_broadcast(bytes(14))


def test_decode_broadcast_flags():
    assert _decode_broadcast(make_broadcast(alarme=True))["alarme"] is True
    assert _decode_broadcast(make_broadcast(alarme=False))["alarme"] is False
    assert _decode_broadcast(make_broadcast(loop_jour=True))["loop_jour"] is True


def test_decode_broadcast_pct_clamped_and_safe():
    # Capacité nulle → pas de division par zéro
    assert _decode_broadcast(make_broadcast(capa_kg=0))["pourcentage_sel"] == 0
    # Sel > capacité → borné à 100
    r = _decode_broadcast(make_broadcast(qte_sel_g=99000, capa_kg=52))
    assert r["pourcentage_sel"] == 100


# ── Issue #4 : firmware V2.x ─────────────────────────────────────────────────

def test_firmware_v2_divides_by_four():
    """V2.21 encode 4x plus haut : la trame porte 145600, on attend 36400."""
    b = make_broadcast(qte_sel_g=36400, capa_kg=52, version=(2, 21), length=20)
    r = _decode_broadcast(b)
    assert r["qte_sel_restant"] == 36400
    assert r["version"] == "A22X V2.21"


def test_firmware_v1_not_divided():
    r = _decode_broadcast(make_broadcast(qte_sel_g=36400, version=(1, 21)))
    assert r["qte_sel_restant"] == 36400


def test_issue4_real_frame():
    """Trame réelle remontée dans l'issue #4 (firmware V2.21, 20 octets)."""
    raw = bytes.fromhex("7c360200ab079803bc07340012021554000000 00".replace(" ", ""))
    r = _decode_broadcast(raw)
    assert r["qte_sel_restant"] == 36255      # 145020 / 4
    assert r["capa_total_sel"] == 52000
    assert r["index_tab_quart"] == 1963
    assert r["index_tab_jour"] == 920
    assert r["vol_sel_rege"] == 1980
    assert r["version"] == "A22X V2.21"
    assert r["pourcentage_sel"] == 69


@pytest.mark.parametrize(
    "step,raw_value",
    [(30, 210_000), (22, 154_000), (15, 105_000), (8, 56_000)],
)
def test_issue4_calibration_series(step, raw_value):
    """Série de calibration 4 points : capacité réelle 52,5 kg."""
    buf = bytearray(make_broadcast(version=(2, 21), length=20))
    buf[0] = raw_value & 0xFF
    buf[1] = (raw_value >> 8) & 0xFF
    buf[2] = (raw_value >> 16) & 0xFF
    buf[3] = (raw_value >> 24) & 0xFF
    attendu = int(52_500 * step / 30)
    assert _decode_broadcast(bytes(buf))["qte_sel_restant"] == attendu


# ── Notifications ────────────────────────────────────────────────────────────

def test_decode_notification_quart():
    n = make_notification([quart_word(120), quart_word(0, rege=True), quart_word(45)])
    idx, entries = _decode_notification(n, is_quart=True)
    assert entries[0] == {"litres": 120, "rege": False}
    assert entries[1] == {"litres": 0, "rege": True}
    assert entries[2] == {"litres": 45, "rege": False}


def test_decode_notification_jour_multiplies_by_ten():
    n = make_notification([jour_word(120), jour_word(2500, regens=1)])
    _, entries = _decode_notification(n, is_quart=False)
    assert entries[0] == {"litres": 120, "rege": 0}
    assert entries[1] == {"litres": 2500, "rege": 1}


def test_decode_notification_stops_at_sentinel():
    """Un mot > 32767 (0xFFFF) marque la fin des données valides."""
    n = make_notification([quart_word(10), quart_word(20)])
    _, entries = _decode_notification(n, is_quart=True)
    assert len(entries) == 2


def test_decode_notification_too_short():
    idx, entries = _decode_notification(bytes(19), is_quart=True)
    assert idx == -1 and entries == []


def test_jour_resolution_is_ten_liters():
    """Régression : conso < 10 L/j est stockée comme 0 (résolution du buffer)."""
    _, entries = _decode_notification(make_notification([jour_word(8)]), is_quart=False)
    assert entries[0]["litres"] == 0


# ── Commandes ────────────────────────────────────────────────────────────────

def test_build_read_cmd():
    assert _build_read_cmd(0x1234, 180, 20) == bytes([0x02, 0x34, 0x12, 0xB4, 0x00, 0x14, 0x00])


def test_build_break_cmd():
    assert _build_break_cmd() == bytes([0x03, 0x00, 0x00])
