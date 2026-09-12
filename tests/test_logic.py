"""Tests de la logique métier du coordinator (sans BLE)."""
from datetime import date, timedelta

import pytest

from conftest import make_broadcast
from custom_components.bwt_aqa_perla_ble.coordinator import _decode_broadcast


def jours_fictifs(clock, nb: int, litres: int = 200, regens_tous_les: int = 5):
    """Génère nb jours consécutifs finissant hier."""
    hier = clock.now().date() - timedelta(days=1)
    return [
        {
            "date": (hier - timedelta(days=nb - 1 - i)).isoformat(),
            "litres": litres,
            "rege": 1 if i % regens_tous_les == 0 else 0,
        }
        for i in range(nb)
    ]


# ── Autonomie sel ────────────────────────────────────────────────────────────

class TestAutonomie:
    def test_calcul_nominal(self, coordinator, clock):
        bcast = _decode_broadcast(make_broadcast(qte_sel_g=36400, vol_rege=1980))
        jours = jours_fictifs(clock, 30, regens_tous_les=5)  # 6 régénérations / 30 j
        coordinator._calculer_autonomie(bcast, jours)
        # sel/j = 6*1980/30 = 396 g → 36400/396 ≈ 92 j
        assert coordinator._autonomie_jours == 92
        assert coordinator._autonomie_semaines == 13
        assert coordinator._autonomie_date is not None

    def test_indisponible_sans_regeneration(self, coordinator, clock):
        bcast = _decode_broadcast(make_broadcast())
        jours = [{**j, "rege": 0} for j in jours_fictifs(clock, 30)]
        coordinator._calculer_autonomie(bcast, jours)
        assert coordinator._autonomie_jours is None
        assert coordinator._autonomie_semaines is None

    def test_indisponible_si_vol_rege_nul(self, coordinator, clock):
        bcast = _decode_broadcast(make_broadcast(vol_rege=0))
        coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 30))
        assert coordinator._autonomie_jours is None

    def test_indisponible_si_historique_trop_court(self, coordinator, clock):
        bcast = _decode_broadcast(make_broadcast())
        coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 1))
        assert coordinator._autonomie_jours is None

    def test_date_figee_entre_regenerations(self, coordinator, clock):
        """La date ne doit pas glisser d'un jour à l'autre sans régénération."""
        bcast = _decode_broadcast(make_broadcast())
        jours = jours_fictifs(clock, 30)
        coordinator._regens_jour_stable = 0
        coordinator._calculer_autonomie(bcast, jours)
        premiere = coordinator._autonomie_date

        for _ in range(3):
            clock.advance(days=1)
            coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 30))
            assert coordinator._autonomie_date == premiere

    def test_date_recalculee_apres_regeneration(self, coordinator, clock):
        bcast = _decode_broadcast(make_broadcast())
        coordinator._regens_jour_stable = 0
        coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 30))
        premiere = coordinator._autonomie_date

        clock.advance(days=1)
        coordinator._regens_jour_stable = 1          # régénération détectée
        coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 30))
        assert coordinator._autonomie_date != premiere

    def test_regeneration_manquee_apres_minuit(self, coordinator, clock):
        """Une régénération entre le dernier cycle complet et minuit est invisible."""
        bcast = _decode_broadcast(make_broadcast())
        coordinator._regens_jour_stable = 2
        coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 30))
        avant = coordinator._autonomie_date

        clock.advance(days=1)
        coordinator._regens_jour_stable = 1   # reset minuit puis 1 régénération
        coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 30))
        assert coordinator._autonomie_date != avant

    def test_date_reinitialisee_si_calcul_impossible(self, coordinator, clock):
        """Cohérence : si jours devient None, la date ne doit pas rester obsolète."""
        bcast_ok = _decode_broadcast(make_broadcast())
        coordinator._calculer_autonomie(bcast_ok, jours_fictifs(clock, 30))
        assert coordinator._autonomie_date is not None

        bcast_ko = _decode_broadcast(make_broadcast(vol_rege=0))
        coordinator._calculer_autonomie(bcast_ko, jours_fictifs(clock, 30))
        assert coordinator._autonomie_jours is None
        assert coordinator._autonomie_date is None, "date obsolète conservée"


# ── Consolidation hier / semaine ─────────────────────────────────────────────

class TestConsolidation:
    def _dict(self, jours):
        return {j["date"]: j for j in jours}

    def test_hier_consolide(self, coordinator, clock):
        clock.set(hour=6)
        jours = jours_fictifs(clock, 8, litres=150)
        coordinator._mettre_a_jour_hier_semaine(self._dict(jours))
        assert coordinator._conso_hier_stable == 150
        assert coordinator._date_hier_stable != ""

    def test_semaine_somme_sept_jours(self, coordinator, clock):
        clock.set(hour=6)
        jours = jours_fictifs(clock, 10, litres=100)
        coordinator._mettre_a_jour_hier_semaine(self._dict(jours))
        assert coordinator._conso_semaine_stable == 700

    def test_avant_4h_valeur_zero_ne_consolide_pas(self, coordinator, clock):
        """Avant 04h00, 0 L signifie "pas encore consolidé" → on conserve l'ancienne."""
        clock.set(hour=2)
        coordinator._conso_hier_stable = 300
        jours = jours_fictifs(clock, 8, litres=0)
        jours[-1]["litres"] = 0
        coordinator._mettre_a_jour_hier_semaine(self._dict(jours))
        assert coordinator._conso_hier_stable == 300

    def test_conso_faible_apres_4h_devient_disponible(self, coordinator, clock):
        """Régression : < 10 L/j est stocké comme 0 mais reste une valeur valide."""
        clock.set(hour=6)
        jours = jours_fictifs(clock, 8, litres=0)
        coordinator._mettre_a_jour_hier_semaine(self._dict(jours))
        assert coordinator._date_hier_stable != "", "capteurs bloqués sur indisponible"
        assert coordinator._conso_hier_stable == 0

    def test_valeur_provisoire_si_hier_absent(self, coordinator, clock):
        """Hier manquant → on retombe sur la dernière valeur non nulle."""
        clock.set(hour=2)
        jours = jours_fictifs(clock, 8, litres=250)
        d = self._dict(jours)
        hier = (clock.now().date() - timedelta(days=1)).isoformat()
        del d[hier]
        coordinator._mettre_a_jour_hier_semaine(d)
        assert coordinator._conso_hier_stable == 250


# ── Historique debug ─────────────────────────────────────────────────────────

class TestDebugHistory:
    def test_stocke_la_trame(self, coordinator, clock):
        coordinator._store_broadcast_debug(make_broadcast())
        assert len(coordinator._debug_broadcast_history) == 1
        assert "[15B]:" in coordinator._debug_broadcast_history[0]

    def test_plafonne_a_dix(self, coordinator, clock):
        for _ in range(15):
            coordinator._store_broadcast_debug(make_broadcast())
        assert len(coordinator._debug_broadcast_history) == 10

    def test_etat_sous_255_caracteres(self, coordinator, clock):
        """Limite HA : l'état d'une entité ne peut dépasser 255 caractères."""
        for _ in range(10):
            coordinator._store_broadcast_debug(make_broadcast(length=20))
        result = coordinator._build_result(_decode_broadcast(make_broadcast()))
        from custom_components.bwt_aqa_perla_ble.const import KEY_DEBUG_BROADCAST
        assert len(str(result[KEY_DEBUG_BROADCAST])) <= 255

    def test_trames_completes_dans_attributs(self, coordinator, clock):
        for _ in range(3):
            coordinator._store_broadcast_debug(make_broadcast())
        result = coordinator._build_result(_decode_broadcast(make_broadcast()))
        assert len(result["debug_broadcast_frames"]) == 3

    def test_sans_donnee(self, coordinator, clock):
        result = coordinator._build_result(_decode_broadcast(make_broadcast()))
        from custom_components.bwt_aqa_perla_ble.const import KEY_DEBUG_BROADCAST
        assert result[KEY_DEBUG_BROADCAST] == "No data"


# ── Construction du résultat ─────────────────────────────────────────────────

class TestBuildResult:
    def test_toutes_les_cles_presentes(self, coordinator, clock):
        from custom_components.bwt_aqa_perla_ble import sensor as sensor_mod
        result = coordinator._build_result(_decode_broadcast(make_broadcast()))
        for desc in sensor_mod.SENSORS:
            assert desc.key in result, f"clé manquante : {desc.key}"

    def test_hier_none_avant_consolidation(self, coordinator, clock):
        from custom_components.bwt_aqa_perla_ble.const import KEY_CONSUMPTION_YESTERDAY, KEY_CONSUMPTION_WEEK
        result = coordinator._build_result(_decode_broadcast(make_broadcast()))
        assert result[KEY_CONSUMPTION_YESTERDAY] is None
        assert result[KEY_CONSUMPTION_WEEK] is None

    def test_conversion_grammes_en_kg(self, coordinator, clock):
        from custom_components.bwt_aqa_perla_ble.const import KEY_SALT_KG, KEY_SALT_TOTAL_KG
        result = coordinator._build_result(
            _decode_broadcast(make_broadcast(qte_sel_g=36400, capa_kg=52))
        )
        assert result[KEY_SALT_KG] == 36.4
        assert result[KEY_SALT_TOTAL_KG] == 52.0

    def test_date_autonomie_est_un_objet_date(self, coordinator, clock):
        """Régression : device_class DATE exige un objet date, pas une chaîne."""
        from custom_components.bwt_aqa_perla_ble.const import KEY_SALT_AUTONOMY_DATE
        coordinator._calculer_autonomie(
            _decode_broadcast(make_broadcast()), jours_fictifs(clock, 30)
        )
        valeur = coordinator._build_result(_decode_broadcast(make_broadcast()))[
            KEY_SALT_AUTONOMY_DATE
        ]
        assert isinstance(valeur, date), f"type {type(valeur)} au lieu de date"


# ── Persistance entre redémarrages ───────────────────────────────────────────

class TestPersistance:
    """La date de fin d'autonomie doit survivre à un redémarrage de HA."""

    @pytest.mark.asyncio
    async def test_date_ecrite_dans_le_stockage(self, coordinator, clock, store):
        coordinator._calculer_autonomie(
            _decode_broadcast(make_broadcast()), jours_fictifs(clock, 30)
        )
        enregistre = store._backing[coordinator._store.key]
        assert enregistre["autonomy_date"] == coordinator._autonomie_date.isoformat()
        assert enregistre["autonomy_days"] == coordinator._autonomie_jours

    @pytest.mark.asyncio
    async def test_date_restauree_au_demarrage(self, coordinator, clock, store):
        from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
        from unittest.mock import MagicMock

        coordinator._calculer_autonomie(
            _decode_broadcast(make_broadcast()), jours_fictifs(clock, 30)
        )
        attendue = coordinator._autonomie_date

        # Redémarrage : un coordinator neuf sur la même adresse
        neuf = BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")
        assert neuf._autonomie_date is None
        await neuf.async_load_stored_data()
        assert neuf._autonomie_date == attendue
        assert neuf._autonomie_jours == coordinator._autonomie_jours

    @pytest.mark.asyncio
    async def test_date_ne_glisse_pas_apres_redemarrage(self, coordinator, clock, store):
        """Le scénario qui motive la persistance : redémarrer ne doit pas décaler la date."""
        from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
        from unittest.mock import MagicMock

        bcast = _decode_broadcast(make_broadcast())
        coordinator._calculer_autonomie(bcast, jours_fictifs(clock, 30))
        attendue = coordinator._autonomie_date

        clock.advance(days=3)                     # trois jours passent
        neuf = BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")
        await neuf.async_load_stored_data()
        neuf._calculer_autonomie(bcast, jours_fictifs(clock, 30))
        assert neuf._autonomie_date == attendue, "date recalculée après redémarrage"

    @pytest.mark.asyncio
    async def test_demarrage_sans_stockage(self, coordinator, store):
        await coordinator.async_load_stored_data()
        assert coordinator._autonomie_date is None

    @pytest.mark.asyncio
    async def test_date_corrompue_ignoree(self, coordinator, store):
        store._backing[coordinator._store.key] = {"autonomy_date": "pas-une-date"}
        await coordinator.async_load_stored_data()
        assert coordinator._autonomie_date is None

    @pytest.mark.asyncio
    async def test_stockage_par_appareil(self, clock, store):
        """Deux adoucisseurs ne doivent pas partager leur état."""
        from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
        from unittest.mock import MagicMock

        a = BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")
        b = BwtCoordinator(MagicMock(), "AA:BB:CC:DD:EE:FF")
        assert a._store.key != b._store.key
