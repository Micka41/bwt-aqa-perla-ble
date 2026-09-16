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
    assert entries[0] == {"litres": 120, "rege": False, "coupure": False}
    assert entries[1] == {"litres": 0, "rege": True, "coupure": False}
    assert entries[2] == {"litres": 45, "rege": False, "coupure": False}


def test_decode_notification_jour_multiplies_by_ten():
    n = make_notification([jour_word(120), jour_word(2500, regens=1)])
    _, entries = _decode_notification(n, is_quart=False)
    assert entries[0] == {"litres": 120, "rege": 0, "coupure": False}
    assert entries[1] == {"litres": 2500, "rege": 1, "coupure": False}


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


# ── Datation par index absolu (issue #9) ─────────────────────────────────────

class TestDatation:
    """Les dates viennent de l'index dans le buffer, pas de la position en liste.

    Issue #9 : une entrée manquante décalait tout le calendrier d'un cran, et la
    consommation d'hier alternait entre deux valeurs à chaque cycle complet.
    """

    def _entrees(self, indices):
        return [{"idx": i, "litres": i * 10} for i in indices]

    def test_la_plus_recente_recoit_lancre(self):
        from datetime import date, timedelta
        from custom_components.bwt_aqa_perla_ble.coordinator import _dater_entrees

        hier = date(2026, 9, 13)
        result = _dater_entrees(self._entrees([97, 98, 99]), 100, 1825,
                                hier, timedelta(days=1))
        assert result[-1]["date"] == hier
        assert result[-2]["date"] == hier - timedelta(days=1)
        assert result[0]["date"] == hier - timedelta(days=2)

    def test_une_entree_manquante_ne_decale_rien(self):
        """Le cœur de l'issue #9."""
        from datetime import date, timedelta
        from custom_components.bwt_aqa_perla_ble.coordinator import _dater_entrees

        hier = date(2026, 9, 13)
        complet = _dater_entrees(self._entrees([97, 98, 99]), 100, 1825,
                                 hier, timedelta(days=1))
        # L'entrée 98 n'a pas été décodée (sentinelle 0xFFFF)
        troue = _dater_entrees(self._entrees([97, 99]), 100, 1825,
                               hier, timedelta(days=1))

        dates_completes = {e["idx"]: e["date"] for e in complet}
        dates_trouees = {e["idx"]: e["date"] for e in troue}
        for idx, d in dates_trouees.items():
            assert d == dates_completes[idx], f"index {idx} mal daté après troncature"

    def test_wrap_du_buffer(self):
        """L'index repasse par zéro sans casser l'ordre chronologique."""
        from datetime import date, timedelta
        from custom_components.bwt_aqa_perla_ble.coordinator import _dater_entrees

        hier = date(2026, 9, 13)
        # idx_courant = 1 : l'entrée 0 est la plus récente, 1824 la précède
        result = _dater_entrees(self._entrees([1823, 1824, 0]), 1, 1825,
                                hier, timedelta(days=1))
        par_idx = {e["idx"]: e["date"] for e in result}
        assert par_idx[0] == hier
        assert par_idx[1824] == hier - timedelta(days=1)
        assert par_idx[1823] == hier - timedelta(days=2)

    def test_pas_de_quinze_minutes(self):
        from datetime import datetime, timedelta
        from custom_components.bwt_aqa_perla_ble.coordinator import _dater_entrees

        ancre = datetime(2026, 9, 14, 9, 0)
        result = _dater_entrees(self._entrees([498, 499]), 500, 2880,
                                ancre, timedelta(minutes=15))
        assert result[-1]["date"] == ancre
        assert result[0]["date"] == ancre - timedelta(minutes=15)

    @pytest.mark.parametrize("idx_courant", [1000, 365, 5, 0, 1824])
    def test_lecture_complete_inchangee(self, idx_courant):
        """Non-régression : sans entrée manquante, les dates sont celles d'avant.

        L'ancienne implémentation déduisait la date de la position dans la
        liste. Le passage à l'index absolu ne doit rien changer quand la
        lecture est complète — y compris au passage par zéro du buffer.
        """
        from datetime import date, timedelta
        from custom_components.bwt_aqa_perla_ble.coordinator import _dater_entrees

        taille, nb = 1825, 365
        debut = (idx_courant - nb) % taille
        entrees = [{"idx": (debut + k) % taille} for k in range(nb)]
        hier = date(2026, 9, 13)

        obtenu = [e["date"] for e in _dater_entrees(
            entrees, idx_courant, taille, hier, timedelta(days=1)
        )]
        attendu = [hier - timedelta(days=(nb - 1 - i)) for i in range(nb)]
        assert obtenu == attendu


def test_pourcentage_aberrant_vaut_zero():
    """Conforme à l'application d'origine : > 50000 % signale une trame incohérente.

    Afficher « plein » sur une donnée manifestement fausse serait trompeur ;
    l'application BWT retourne 0 dans ce cas.
    """
    # Capacité 1 kg, sel 4 294 967 g → ratio délirant
    buf = bytearray(make_broadcast(capa_kg=1))
    buf[0] = buf[1] = buf[2] = buf[3] = 0xFF
    assert _decode_broadcast(bytes(buf))["pourcentage_sel"] == 0


def test_pourcentage_borne_haute_normale():
    """Un dépassement modéré reste plafonné à 100, pas remis à zéro."""
    r = _decode_broadcast(make_broadcast(qte_sel_g=60000, capa_kg=52))
    assert r["pourcentage_sel"] == 100


# ── Coupure d'eau (bit décodé par clsListe) ──────────────────────────────────

def test_coupure_quart():
    """Sur les quarts, la coupure est portée par le bit 0x0400."""
    n = make_notification([quart_word(50) | 0x0400, quart_word(50)])
    _, entries = _decode_notification(n, is_quart=True)
    assert entries[0]["coupure"] is True
    assert entries[1]["coupure"] is False


def test_coupure_jour():
    """Sur les jours, c'est le bit 0x0800 — les masques diffèrent entre buffers."""
    n = make_notification([jour_word(100) | 0x0800, jour_word(100)])
    _, entries = _decode_notification(n, is_quart=False)
    assert entries[0]["coupure"] is True
    assert entries[1]["coupure"] is False


def test_coupure_nempeche_pas_la_lecture_des_litres():
    """Le bit de coupure ne doit pas empiéter sur la valeur en litres."""
    n = make_notification([quart_word(999) | 0x0400])
    _, entries = _decode_notification(n, is_quart=True)
    assert entries[0]["litres"] == 999


def test_coupure_et_regeneration_simultanees():
    n = make_notification([quart_word(0) | 0x0400 | 0x0800])
    _, entries = _decode_notification(n, is_quart=True)
    assert entries[0]["coupure"] is True and entries[0]["rege"] is True
