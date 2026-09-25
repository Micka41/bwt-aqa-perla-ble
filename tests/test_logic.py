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

class TestHierSemaine:
    """Hier et 7 jours, reconstitués depuis les quarts d'heure (issue #10).

    Ils ne dépendent plus de l'heure à laquelle l'adoucisseur change de case
    journalière — 4 h chez l'un, 9 h 30 chez l'autre.
    """

    @staticmethod
    def _quarts(clock, jours_avant: int, litres_par_jour: dict[int, int]):
        """Quarts datés de J-jours_avant à maintenant ; litres répartis par jour."""
        from datetime import timedelta
        maintenant = clock.now()
        debut = maintenant.replace(hour=0, minute=0) - timedelta(days=jours_avant)
        quarts, t = [], debut
        while t < maintenant:
            jour = (maintenant.date() - t.date()).days
            quarts.append({
                "debut": t, "date": t.date().isoformat(),
                "litres": litres_par_jour.get(jour, 0) // 96, "rege": False,
            })
            t += timedelta(minutes=15)
        return quarts

    def test_hier_au_litre_pres(self, coordinator, clock):
        clock.set(hour=10)
        q = self._quarts(clock, 2, {1: 96 * 3})
        coordinator._calculer_hier_semaine(q, clock.now())
        assert coordinator._conso_hier_stable == 288

    def test_semaine_sept_jours_clos(self, coordinator, clock):
        """7 jours pleins, de J-7 à J-1 ; la journée en cours n'en fait pas partie."""
        clock.set(hour=10)
        q = self._quarts(clock, 8, {j: 96 * j for j in range(0, 9)})
        coordinator._calculer_hier_semaine(q, clock.now())
        assert coordinator._conso_semaine_stable == 96 * sum(range(1, 8))

    def test_valide_une_fois_par_jour(self, coordinator, clock):
        clock.set(hour=10)
        coordinator._calculer_hier_semaine(self._quarts(clock, 2, {}), clock.now())
        assert coordinator._jour_hier_semaine == clock.now().date().isoformat()

    def test_non_valide_avant_00h20(self, coordinator, clock):
        """Juste après minuit, le dernier quart de la veille peut manquer encore."""
        clock.set(hour=0, minute=10)
        coordinator._calculer_hier_semaine(self._quarts(clock, 2, {1: 96}), clock.now())
        assert coordinator._conso_hier_stable == 96          # calculé…
        assert coordinator._jour_hier_semaine == ""          # …mais à refaire

    def test_nouvelle_installation(self, coordinator, clock):
        """Moins de 7 jours de quarts : on somme ce qui existe."""
        clock.set(hour=10)
        q = self._quarts(clock, 3, {j: 96 for j in range(0, 4)})
        coordinator._calculer_hier_semaine(q, clock.now())
        assert coordinator._conso_semaine_stable == 96 * 3

    def test_indisponible_sans_quarts(self, coordinator, clock):
        coordinator._calculer_hier_semaine([], clock.now())
        assert coordinator._conso_hier_stable is None
        assert coordinator._conso_semaine_stable is None

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


class TestCoupures:
    """Comptage des coupures d'eau du jour."""

    def test_transitions_comptees_une_fois(self, coordinator, clock):
        """Une coupure qui dure plusieurs quarts compte pour un seul événement."""
        aujourd_hui = clock.now().date().isoformat()
        quarts = [
            {"date": aujourd_hui, "litres": 10, "rege": False, "coupure": c}
            for c in (False, True, True, True, False, False, True, False)
        ]
        coupures, prev = 0, False
        for q in quarts:
            if q["coupure"] and not prev:
                coupures += 1
            prev = q["coupure"]
        assert coupures == 2, "deux épisodes distincts attendus"

    def test_expose_dans_le_resultat(self, coordinator, clock):
        from custom_components.bwt_aqa_perla_ble.const import KEY_CUTOFF_TODAY
        coordinator._coupures_jour_stable = 3
        result = coordinator._build_result(_decode_broadcast(make_broadcast()))
        assert result[KEY_CUTOFF_TODAY] == 3


@pytest.mark.asyncio
async def test_suppression_de_l_integration_efface_l_etat(coordinator, clock, store):
    """Réinstaller doit repartir de zéro : ni bascule apprise, ni date d'autonomie."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from custom_components.bwt_aqa_perla_ble import async_remove_entry
    from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator

    coordinator._calculer_autonomie(
        _decode_broadcast(make_broadcast()), jours_fictifs(clock, 30)
    )
    assert store._backing, "l'état aurait dû être enregistré"

    entree = SimpleNamespace(data={"address": "03:12:00:34:00:5E"})
    await async_remove_entry(MagicMock(), entree)
    assert not store._backing

    neuf = BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")
    await neuf.async_load_stored_data()
    assert neuf._autonomie_date is None and neuf._heure_bascule_texte() is None
