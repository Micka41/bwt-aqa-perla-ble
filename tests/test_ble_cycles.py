"""Tests des cycles BLE avec un appareil simulé.

Couvre : cycle rapide, cycle complet, wrap des buffers circulaires,
services d'historique, et les échecs de lecture.
"""
from datetime import timedelta
import asyncio

from bleak.exc import BleakError
from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_broadcast, make_notification, jour_word, quart_word


class FakeBwtDevice:
    """Simule l'adoucisseur : répond aux commandes READ par des notifications."""

    def __init__(self, broadcast: bytes, quarts: list[int], jours: list[int],
                 fail_after_blocks: int | None = None):
        self.broadcast = broadcast
        self.quarts = quarts          # mots bruts du buffer quart
        self.jours = jours            # mots bruts du buffer journalier
        self.fail_after_blocks = fail_after_blocks
        self.blocks_served = 0
        self.reads: list[tuple[int, int]] = []   # (adresse, nb_octets)
        self._callback = None
        self.disconnected = False
        self.break_sent = False

    # -- API BleakClient utilisée par le coordinator --

    async def start_notify(self, uuid, callback):
        self._callback = callback

    async def stop_notify(self, uuid):
        self._callback = None

    async def read_gatt_char(self, uuid):
        from custom_components.bwt_aqa_perla_ble.const import UUID_BROADCAST
        return self.broadcast if uuid == UUID_BROADCAST else b"\x00"

    async def write_gatt_char(self, uuid, data):
        if data[0] == 0x03:                       # BREAK
            self.break_sent = True
            return
        adresse = data[1] | (data[2] << 8)
        nb_oct = data[3] | (data[4] << 8)
        self.reads.append((adresse, nb_oct))

        self.blocks_served += 1
        if self.fail_after_blocks is not None and self.blocks_served > self.fail_after_blocks:
            return                                # silence : aucune notification

        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_JOUR, ADRESSE_TAB_QUART
        if adresse >= ADRESSE_TAB_JOUR:
            source, base = self.jours, ADRESSE_TAB_JOUR
        else:
            source, base = self.quarts, ADRESSE_TAB_QUART

        index = (adresse - base) // 2
        mots = source[index:index + nb_oct // 2]
        # Le numéro de séquence repart de zéro à chaque bloc
        for n, i in enumerate(range(0, len(mots), 9)):
            self._callback(None, bytearray(make_notification(mots[i:i + 9], n)))

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake_device():
    def _make(**kwargs):
        params = {
            "broadcast": make_broadcast(idx_quart=100, idx_jour=50),
            "quarts": [quart_word(10) for _ in range(2880)],
            "jours": [jour_word(200) for _ in range(1825)],
        }
        params.update(kwargs)
        return FakeBwtDevice(**params)
    return _make


@pytest.fixture
def patched_ble(fake_device):
    """Branche le faux appareil sur establish_connection.

    Patche les deux chemins : l'import de tête de coordinator.py ET
    l'import local fait dans _read_full_history (from bleak_retry_connector
    import establish_connection as _establish).
    """
    from contextlib import ExitStack

    def _run(device):
        stack = ExitStack()
        mock = AsyncMock(return_value=device)
        stack.enter_context(patch("custom_components.bwt_aqa_perla_ble.coordinator.establish_connection", mock))
        stack.enter_context(patch("bleak_retry_connector.establish_connection", mock))
        # Timeouts raccourcis : les tests ne doivent pas attendre le silence BLE réel
        stack.enter_context(patch("custom_components.bwt_aqa_perla_ble.coordinator.BLE_NOTIFY_SILENCE", 0.01))
        stack.enter_context(patch("custom_components.bwt_aqa_perla_ble.coordinator.BLE_NOTIFY_TIMEOUT", 0.2))
        stack.enter_context(patch("custom_components.bwt_aqa_perla_ble.coordinator.BLE_DISCONNECT_TIMEOUT", 0.1))
        return stack
    return _run



def coordinator_aujourd_hui() -> str:
    from homeassistant.util import dt as dt_util
    return dt_util.now().date().isoformat()


# ── Cycle rapide ─────────────────────────────────────────────────────────────

class TestCycleRapide:
    @pytest.mark.asyncio
    async def test_accumule_le_delta(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_quart=105),
                          quarts=[quart_word(25) for _ in range(2880)])
        coordinator._index_base = 100
        coordinator._litres_jour_base = 1000
        with patched_ble(dev):
            result = await coordinator._run_rapide(object())
        from custom_components.bwt_aqa_perla_ble.const import KEY_CONSUMPTION_TODAY
        assert result[KEY_CONSUMPTION_TODAY] == 1000 + 5 * 25
        assert dev.disconnected and dev.break_sent

    @pytest.mark.asyncio
    async def test_sans_nouveau_quart(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_quart=100))
        coordinator._index_base = 100
        coordinator._litres_jour_base = 500
        with patched_ble(dev):
            result = await coordinator._run_rapide(object())
        from custom_components.bwt_aqa_perla_ble.const import KEY_CONSUMPTION_TODAY
        assert result[KEY_CONSUMPTION_TODAY] == 500

    @pytest.mark.asyncio
    async def test_deconnexion_meme_en_cas_derreur(self, coordinator, fake_device, patched_ble):
        dev = fake_device()
        dev.read_gatt_char = AsyncMock(side_effect=RuntimeError("boom"))
        with patched_ble(dev), pytest.raises(RuntimeError):
            await coordinator._run_rapide(object())
        assert dev.disconnected, "client non déconnecté après erreur"


# ── Cycle complet et buffers circulaires ─────────────────────────────────────

class TestCycleComplet:
    @pytest.mark.asyncio
    async def test_lecture_nominale(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_quart=500, idx_jour=100))
        with patched_ble(dev):
            await coordinator._run_complet(object())
        assert coordinator._index_base == 500
        assert coordinator._avg_daily_30d is not None

    @pytest.mark.asyncio
    async def test_wrap_buffer_quart(self, coordinator, fake_device, patched_ble):
        """idx < nb : la lecture doit se faire en deux parties, sans doublon."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART, MAX_TAB_QUART, NB_QUARTS_COMPLET
        dev = fake_device(broadcast=make_broadcast(idx_quart=20, idx_jour=100, loop_quart=True))
        # Hier et 7 jours déjà calculés aujourd'hui : lecture courte de 120 quarts
        coordinator._jour_hier_semaine = coordinator_aujourd_hui()
        with patched_ble(dev):
            await coordinator._run_complet(object())

        lectures_quart = [
            ((a - ADRESSE_TAB_QUART) // 2, n // 2)
            for a, n in dev.reads if a < ADRESSE_TAB_QUART + MAX_TAB_QUART * 2
        ]
        total = sum(n for _, n in lectures_quart)
        assert total == min(NB_QUARTS_COMPLET, MAX_TAB_QUART)
        assert any(i >= MAX_TAB_QUART - 100 for i, _ in lectures_quart), "fin de buffer non lue"

    @pytest.mark.asyncio
    async def test_lecture_etendue_une_fois_par_jour(
        self, coordinator, fake_device, patched_ble, clock
    ):
        """Le premier cycle du jour remonte à J-7 ; les suivants lisent 120 quarts."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART, MAX_TAB_QUART
        clock.set(hour=12, minute=0)

        def quarts_lus(dev):
            return sum(n // 2 for a, n in dev.reads if a < ADRESSE_TAB_QUART + MAX_TAB_QUART * 2)

        dev = fake_device(broadcast=make_broadcast(idx_quart=2000, idx_jour=100))
        with patched_ble(dev):
            await coordinator._run_complet(object())
        assert quarts_lus(dev) == 7 * 96 + 48 + 1       # de J-7 à midi

        dev = fake_device(broadcast=make_broadcast(idx_quart=2004, idx_jour=100))
        with patched_ble(dev):
            await coordinator._run_complet(object())
        assert quarts_lus(dev) == 120

    @pytest.mark.asyncio
    async def test_wrap_a_index_zero(self, coordinator, fake_device, patched_ble):
        """Cas limite idx == 0 : tout doit être lu en fin de buffer."""
        dev = fake_device(broadcast=make_broadcast(idx_quart=0, idx_jour=0, loop_quart=True))
        with patched_ble(dev):
            await coordinator._run_complet(object())
        assert dev.reads, "aucune lecture effectuée"

    @pytest.mark.asyncio
    async def test_regenerations_du_jour(self, coordinator, fake_device, patched_ble, clock):
        """Compte les transitions False→True, pas les quarts en régénération."""
        clock.set(hour=12, minute=0)
        quarts = [quart_word(10) for _ in range(2880)]
        # 3 quarts consécutifs en régénération = 1 seule régénération
        for i in range(2870, 2873):
            quarts[i] = quart_word(0, rege=True)
        dev = fake_device(broadcast=make_broadcast(idx_quart=2880, idx_jour=100), quarts=quarts)
        with patched_ble(dev):
            await coordinator._run_complet(object())
        assert coordinator._regens_jour_stable <= 1


# ── Échecs de lecture (BUG 1) ────────────────────────────────────────────────

class TestEchecsLecture:
    @pytest.mark.asyncio
    async def test_lecture_complete_renvoie_tout(self, coordinator, fake_device, patched_ble):
        """Sans panne, toutes les entrées demandées sont lues."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        dev = fake_device()
        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 0, 180, is_quart=True
            )
        assert len(entries) == 180

    @pytest.mark.asyncio
    async def test_lecture_partielle_devrait_lever(self, coordinator, fake_device, patched_ble):
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from homeassistant.helpers.update_coordinator import UpdateFailed
        dev = fake_device(fail_after_blocks=1)
        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            with pytest.raises(UpdateFailed):
                await coordinator._lire_blocs(
                    client, ADRESSE_TAB_QUART, 0, 180, is_quart=True
                )

    @pytest.mark.asyncio
    async def test_index_absolu_present_sur_chaque_entree(
        self, coordinator, fake_device, patched_ble
    ):
        """Chaque entrée lue porte son index dans le buffer circulaire."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        dev = fake_device()
        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 40, 20, is_quart=True
            )
        assert [e["idx"] for e in entries] == list(range(40, 60))


# ── Services d'historique ────────────────────────────────────────────────────

class TestServices:
    """Services d'historique : quarts d'heure récents, cases journalières au-delà.

    L'horloge des tests est fixée au 28/04 à midi. Avec 240 quarts écrits, le
    buffer des quarts couvre exactement le 26 et le 27 en entier, puis le 28
    jusqu'à 11 h 45 — la journée en cours, exclue des services.
    """

    QUARTS_DEUX_JOURS = 240

    @staticmethod
    def _deux_jours_de_quarts(litres: int) -> list[int]:
        return [quart_word(litres) for _ in range(2880)]

    @pytest.mark.asyncio
    async def test_cases_seules(self, coordinator, fake_device, patched_ble):
        """Sans quarts, l'historique vient entièrement des cases journalières."""
        dev = fake_device(broadcast=make_broadcast(idx_jour=100, idx_quart=0),
                          jours=[jour_word(150) for _ in range(1825)])
        with patched_ble(dev):
            result = await coordinator.service_total_consumption()
        assert result["days_count"] == 100
        assert result["total_liters"] == 100 * 150
        assert result["to_date"] == "2026-04-27"          # hier

    @pytest.mark.asyncio
    async def test_historique_vide(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_jour=0, idx_quart=0))
        with patched_ble(dev):
            result = await coordinator.service_total_consumption()
        assert result["days_count"] == 0
        assert result["from_date"] is None

    @pytest.mark.asyncio
    async def test_jours_recents_depuis_les_quarts(self, coordinator, fake_device, patched_ble):
        """Les jours couverts par les quarts remplacent les cases, au litre près."""
        dev = fake_device(
            broadcast=make_broadcast(idx_jour=100, idx_quart=self.QUARTS_DEUX_JOURS),
            jours=[jour_word(150) for _ in range(1825)],
            quarts=self._deux_jours_de_quarts(3),
        )
        with patched_ble(dev):
            result = await coordinator.service_history_consumption()
        avril = result["2026"]["04"]
        assert avril["26"] == avril["27"] == 96 * 3        # 288 L : pas un multiple de 10
        assert avril["25"] == 150                          # au-delà : case journalière
        assert "28" not in avril                           # journée en cours exclue

    @pytest.mark.asyncio
    async def test_total_sans_double_comptage(self, coordinator, fake_device, patched_ble):
        """Bascule à minuit : les cases du 26 et du 27 cèdent la place aux quarts."""
        dev = fake_device(
            broadcast=make_broadcast(idx_jour=100, idx_quart=self.QUARTS_DEUX_JOURS),
            jours=[jour_word(150) for _ in range(1825)],
            quarts=self._deux_jours_de_quarts(2),
        )
        with patched_ble(dev):
            result = await coordinator.service_total_consumption()
        assert result["days_count"] == 100
        assert result["total_liters"] == 98 * 150 + 2 * 96 * 2

    @pytest.mark.asyncio
    async def test_raccord_avec_bascule_apprise(
        self, coordinator, fake_device, patched_ble, clock
    ):
        """Bascule apprise à 9 h 30 : la case à cheval sur le 26 est tronquée.

        La case 25/9 h 30 – 26/9 h 30 couvre les 9 h 30 du 26 que les quarts
        comptent déjà : 38 quarts de 2 L en sont retirés, sous la date du 25.
        """
        from custom_components.bwt_aqa_perla_ble.const import MAX_TAB_JOUR

        bascule = clock.now().replace(hour=9, minute=30)
        coordinator._bascule.observer(99, bascule - timedelta(minutes=5), MAX_TAB_JOUR)
        coordinator._bascule.observer(100, bascule + timedelta(minutes=5), MAX_TAB_JOUR)

        dev = fake_device(
            broadcast=make_broadcast(idx_jour=100, idx_quart=self.QUARTS_DEUX_JOURS),
            jours=[jour_word(150) for _ in range(1825)],
            quarts=self._deux_jours_de_quarts(2),
        )
        with patched_ble(dev):
            historique = await coordinator.service_history_consumption()
            total = await coordinator.service_total_consumption()

        avril = historique["2026"]["04"]
        assert avril["25"] == 150 - 38 * 2
        assert avril["26"] == avril["27"] == 96 * 2
        assert total["total_liters"] == 97 * 150 + (150 - 38 * 2) + 2 * 96 * 2

    @pytest.mark.asyncio
    async def test_historique_conso_structure(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_jour=40),
                          jours=[jour_word(100) for _ in range(1825)])
        with patched_ble(dev):
            result = await coordinator.service_history_consumption()
        annee = next(iter(result))
        mois = next(iter(result[annee]))
        jour = next(iter(result[annee][mois]))
        assert len(annee) == 4 and len(mois) == 2 and len(jour) == 2
        assert isinstance(result[annee][mois][jour], int)

    @pytest.mark.asyncio
    async def test_regenerations_recentes_depuis_les_quarts(
        self, coordinator, fake_device, patched_ble
    ):
        """Une régénération sur quatre quarts du 27 compte pour une, le 27."""
        quarts = self._deux_jours_de_quarts(1)
        fin = self.QUARTS_DEUX_JOURS               # index du quart de 11 h 45 le 28 + 1
        debut_27 = fin - 1 - 47 - 96               # 27/04 à 0 h
        for i in range(debut_27 + 12, debut_27 + 16):   # 3 h – 4 h
            quarts[i] = quart_word(0, rege=True)
        jours = [jour_word(100, regens=1 if i % 5 == 0 else 0) for i in range(1825)]
        dev = fake_device(
            broadcast=make_broadcast(idx_jour=100, idx_quart=self.QUARTS_DEUX_JOURS),
            jours=jours, quarts=quarts,
        )
        with patched_ble(dev):
            result = await coordinator.service_history_regenerations()
        avril = result["2026"]["04"]
        assert avril["27"] == 1 and avril["26"] == 0

    @pytest.mark.asyncio
    async def test_buffer_plein_lu_en_deux_parties(self, coordinator, fake_device, patched_ble):
        """loop_jour actif : les 1825 jours doivent être lus, wrap inclus."""
        from custom_components.bwt_aqa_perla_ble.const import MAX_TAB_JOUR
        dev = fake_device(
            broadcast=make_broadcast(idx_jour=300, idx_quart=0, loop_jour=True),
            jours=[jour_word(100) for _ in range(MAX_TAB_JOUR)],
        )
        with patched_ble(dev):
            result = await coordinator.service_total_consumption()
        assert result["days_count"] == MAX_TAB_JOUR


# ── Session BLE (context manager) ────────────────────────────────────────────

class TestSessionBLE:
    """Le context manager doit garantir connexion propre et fermeture."""

    @pytest.mark.asyncio
    async def test_cede_client_et_broadcast(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_quart=777))
        with patched_ble(dev):
            async with coordinator._ble_session(object()) as (client, bcast):
                assert client is dev
                assert bcast["index_tab_quart"] == 777
        assert dev.disconnected and dev.break_sent

    @pytest.mark.asyncio
    async def test_deconnecte_si_le_bloc_leve(self, coordinator, fake_device, patched_ble):
        dev = fake_device()
        with patched_ble(dev):
            with pytest.raises(RuntimeError):
                async with coordinator._ble_session(object()):
                    raise RuntimeError("échec pendant la lecture")
        assert dev.disconnected, "client non déconnecté après exception"
        assert not dev.break_sent, "BREAK envoyé alors que le bloc a échoué"

    @pytest.mark.asyncio
    async def test_met_a_jour_firmware_et_index(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_quart=1234, version=(2, 21), length=20))
        with patched_ble(dev):
            async with coordinator._ble_session(object()):
                pass
        assert coordinator._firmware == "A22X V2.21"
        assert coordinator._dernier_index_tab_quart == 1234

    @pytest.mark.asyncio
    async def test_trame_enregistree_pour_le_diagnostic(self, coordinator, fake_device, patched_ble):
        dev = fake_device()
        with patched_ble(dev):
            async with coordinator._ble_session(object()):
                pass
        assert len(coordinator._debug_broadcast_history) == 1

    @pytest.mark.asyncio
    async def test_recherche_lappareil_si_absent(self, coordinator, fake_device, patched_ble):
        """Sans ble_device fourni (appels de service), la session le résout."""
        dev = fake_device()
        with patched_ble(dev), patch.object(
            coordinator, "_resolve_ble_device", return_value=object()
        ) as resolve:
            async with coordinator._ble_session():
                pass
        resolve.assert_called_once()

    @pytest.mark.asyncio
    async def test_echoue_si_appareil_hors_de_portee(self, coordinator):
        from homeassistant.helpers.update_coordinator import UpdateFailed
        with patch("custom_components.bwt_aqa_perla_ble.coordinator"
                   ".async_ble_device_from_address", return_value=None):
            with pytest.raises(UpdateFailed, match="not found"):
                async with coordinator._ble_session():
                    pass

    @pytest.mark.asyncio
    async def test_deconnexion_bloquee_nempeche_pas_lerreur_de_remonter(
        self, coordinator, fake_device, patched_ble
    ):
        """Issue #8 : un disconnect() qui ne rend jamais la main bloquait tout.

        Sur une pile BlueZ dégradée, `disconnect()` peut attendre indéfiniment.
        Comme cela se produit pendant la propagation d'une exception, l'erreur
        d'origine n'atteignait jamais son gestionnaire : le démarrage de Home
        Assistant restait suspendu jusqu'au délai global, et l'entrée finissait
        en `setup_error`, que HA ne réessaie pas.
        """
        dev = fake_device()
        dev.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

        async def disconnect_qui_bloque():
            await asyncio.sleep(3600)
        dev.disconnect = disconnect_qui_bloque

        with patched_ble(dev):
            # L'erreur d'origine doit remonter, sans attendre la déconnexion
            with pytest.raises(BleakError, match="Not connected"):
                async with asyncio.timeout(2):
                    async with coordinator._ble_session(object()):
                        pass

    @pytest.mark.asyncio
    async def test_echec_de_deconnexion_ignore(self, coordinator, fake_device, patched_ble):
        """Une déconnexion qui échoue ne doit pas masquer un cycle réussi."""
        dev = fake_device()
        dev.disconnect = AsyncMock(side_effect=BleakError("already gone"))
        with patched_ble(dev):
            async with coordinator._ble_session(object()) as (client, bcast):
                assert bcast is not None

    @pytest.mark.asyncio
    async def test_index_issu_de_la_trame(self, coordinator, fake_device, patched_ble):
        """L'index vient de la trame elle-même quand il est cohérent."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        dev = fake_device()
        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 100, 18, is_quart=True
            )
        assert [e["idx"] for e in entries] == list(range(100, 118))


# ── Sessions concurrentes (issues #9 et #10) ─────────────────────────────────

class ConnexionPartagee(FakeBwtDevice):
    """Deux clients sur une même connexion GATT, comme via un proxy ESPHome.

    Chaque abonnement actif reçoit toutes les notifications ; `stop_notify` et
    `disconnect` retirent l'abonnement, comme le fait Bleak.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.abonnes: list = []

    async def start_notify(self, uuid, callback):
        self.abonnes.append(callback)
        self._callback = lambda s, d: [cb(s, d) for cb in list(self.abonnes)]

    async def stop_notify(self, uuid):
        self.abonnes.clear()

    async def disconnect(self):
        self.abonnes.clear()
        self.disconnected = True

    async def write_gatt_char(self, uuid, data):
        await asyncio.sleep(0.005)      # laisse l'autre session s'intercaler
        return await super().write_gatt_char(uuid, data)


class TestSessionsConcurrentes:
    """Une automatisation qui appelle un service pendant un cycle de rafraîchissement.

    Sans verrou, les deux sessions s'abonnaient aux notifications de la même
    connexion et empilaient leurs trames dans la même liste : chaque trame
    arrivait deux fois, les résultats étaient faux, souvent sans erreur.
    """

    @pytest.fixture
    def appareil(self):
        return ConnexionPartagee(
            make_broadcast(idx_jour=200, idx_quart=0), [0] * 2880,
            [jour_word(150) for _ in range(1825)],
        )

    @pytest.mark.asyncio
    async def test_deux_services_simultanes_donnent_le_bon_resultat(
        self, coordinator, appareil, patched_ble
    ):
        with patched_ble(appareil), patch(
            "custom_components.bwt_aqa_perla_ble.coordinator"
            ".async_ble_device_from_address", return_value=object(),
        ):
            resultats = await asyncio.gather(
                coordinator.service_total_consumption(),
                coordinator.service_total_consumption(),
            )
        for r in resultats:
            assert r["days_count"] == 200
            assert r["total_liters"] == 200 * 150

    @pytest.mark.asyncio
    async def test_service_pendant_un_cycle_complet(
        self, coordinator, appareil, patched_ble
    ):
        """Le cas réel : l'automatisation tombe pendant un cycle horaire."""
        with patched_ble(appareil), patch(
            "custom_components.bwt_aqa_perla_ble.coordinator"
            ".async_ble_device_from_address", return_value=object(),
        ):
            _, total = await asyncio.gather(
                coordinator._run_complet(object()),
                coordinator.service_total_consumption(),
            )
        assert total["days_count"] == 200

    @pytest.mark.asyncio
    async def test_les_sessions_sont_serialisees(self, coordinator, fake_device, patched_ble):
        """Aucune session ne démarre tant que la précédente n'est pas terminée."""
        dev = fake_device()
        actives, max_actives = 0, 0

        with patched_ble(dev):
            async def session():
                nonlocal actives, max_actives
                async with coordinator._ble_session(object()):
                    actives += 1
                    max_actives = max(max_actives, actives)
                    await asyncio.sleep(0.01)
                    actives -= 1

            await asyncio.gather(session(), session(), session())
        assert max_actives == 1

    @pytest.mark.asyncio
    async def test_historique_trie_par_date(self, coordinator, fake_device, patched_ble):
        """Le résultat sort dans l'ordre chronologique, buffer bouclé compris.

        L'ordre de lecture l'est déjà, mais il dépend du découpage en blocs et
        du point de wrap : le tri rend la garantie explicite.
        """
        from custom_components.bwt_aqa_perla_ble.const import MAX_TAB_JOUR

        dev = fake_device(
            broadcast=make_broadcast(idx_jour=300, loop_jour=True),
            jours=[jour_word(100 + (k % 50) * 10) for k in range(MAX_TAB_JOUR)],
        )
        with patched_ble(dev):
            result = await coordinator.service_history_consumption()

        dates = [
            f"{annee}-{mois}-{jour}"
            for annee, mois_dict in result.items()
            for mois, jours_dict in mois_dict.items()
            for jour in jours_dict
        ]
        assert dates == sorted(dates), "les dates ne sortent pas dans l'ordre"



# ── Trames tardives et cascade (issues #9 et #10) ────────────────────────────

class ProxyAvecLatence(FakeBwtDevice):
    """Relaie les trames en arrière-plan, comme un proxy ESPHome.

    Chaque commande READ met `latence` secondes à atteindre l'appareil ; la
    dernière trame du premier bloc peut subir une `pause` supplémentaire.
    Avec pause > silence toléré mais < silence + latence, cette trame arrive
    après l'envoi de la commande suivante et avant ses propres trames : c'est
    la configuration observée dans les journaux de l'issue #10.
    """

    def __init__(self, *args, latence=0.02, pause=0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.latence, self.pause, self.num_bloc = latence, pause, 0

    async def write_gatt_char(self, uuid, data):
        if data[0] == 0x03:
            self.break_sent = True
            return
        from custom_components.bwt_aqa_perla_ble.const import (
            ADRESSE_TAB_JOUR, ADRESSE_TAB_QUART,
        )
        adresse = data[1] | (data[2] << 8)
        nb_oct = data[3] | (data[4] << 8)
        self.reads.append((adresse, nb_oct))
        if adresse >= ADRESSE_TAB_JOUR:
            source, base = self.jours, ADRESSE_TAB_JOUR
        else:
            source, base = self.quarts, ADRESSE_TAB_QUART
        index = (adresse - base) // 2
        mots = source[index:index + nb_oct // 2]
        trames = [make_notification(mots[i:i + 9], n)
                  for n, i in enumerate(range(0, len(mots), 9))]
        premier = self.num_bloc == 0
        self.num_bloc += 1

        async def envoyer():
            await asyncio.sleep(self.latence)
            for n, t in enumerate(trames):
                if premier and n == len(trames) - 1 and self.pause:
                    await asyncio.sleep(self.pause)
                await asyncio.sleep(0.001)
                if self._callback:
                    self._callback(None, bytearray(t))
        asyncio.get_event_loop().create_task(envoyer())


def _valeurs_indexees(taille):
    """Chaque case vaut son propre index : un mauvais placement saute aux yeux."""
    return [quart_word(k % 1000) for k in range(taille)]


class TestTramesTardives:

    @pytest.mark.asyncio
    async def test_trame_tardive_en_tete_de_bloc_ecartee(
        self, coordinator, fake_device, patched_ble
    ):
        """Le motif exact des journaux : « 0 after 9 » en tête de bloc.

        La dernière trame du bloc précédent (rang 9) arrive avant la première
        du bloc courant. Elle ne prolonge pas la séquence 0, 1, 2… et doit être
        écartée ; les entrées du bloc restent à leur place.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        dev = fake_device(quarts=_valeurs_indexees(2880))
        original = dev.write_gatt_char

        async def avec_tardive(uuid, data):
            if data[0] != 0x03:
                dev._callback(None, bytearray(
                    make_notification([quart_word(999)] * 9, 9)
                ))
            return await original(uuid, data)
        dev.write_gatt_char = avec_tardive

        with patched_ble(dev):
            client = dev
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 100, 90, is_quart=True
            )
        assert [e["idx"] for e in entries] == list(range(100, 190))
        assert all(e["litres"] == e["idx"] for e in entries), (
            "une trame tardive a été prise pour une trame du bloc"
        )

    @pytest.mark.asyncio
    async def test_doublon_ecarte(self, coordinator, fake_device, patched_ble):
        """Une trame reçue deux fois ne décale pas les suivantes."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        dev = fake_device(quarts=_valeurs_indexees(2880))

        async def en_double(uuid, data):
            if data[0] == 0x03:
                return
            mots = dev.quarts[100:118]
            t0 = make_notification(mots[0:9], 0)
            t1 = make_notification(mots[9:18], 1)
            for t in (t0, t0, t1):
                dev._callback(None, bytearray(t))
        dev.write_gatt_char = en_double

        with patched_ble(dev):
            await coordinator._start_notify(dev)
            entries = await coordinator._lire_blocs(
                dev, ADRESSE_TAB_QUART, 100, 18, is_quart=True
            )
        assert all(e["litres"] == e["idx"] for e in entries)
        assert len(entries) == 18

    @pytest.mark.asyncio
    async def test_bloc_incomplet_echoue(self, coordinator, fake_device, patched_ble):
        """Un bloc auquel il manque une trame ne produit pas de données.

        Ses entrées seraient mal placées : mieux vaut échouer et réessayer au
        cycle suivant que remonter des valeurs fausses sans erreur.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from homeassistant.helpers.update_coordinator import UpdateFailed
        dev = fake_device()

        async def sans_la_derniere(uuid, data):
            if data[0] == 0x03:
                return
            for n in range(9):          # 9 trames sur les 10 attendues
                dev._callback(None, bytearray(
                    make_notification([quart_word(1)] * 9, n)
                ))
        dev.write_gatt_char = sans_la_derniere

        with patched_ble(dev):
            await coordinator._start_notify(dev)
            with pytest.raises(UpdateFailed, match="Incomplete block"):
                await coordinator._lire_blocs(
                    dev, ADRESSE_TAB_QUART, 0, 90, is_quart=True
                )

    @pytest.mark.asyncio
    async def test_pause_du_proxy_toleree_sans_cascade(self, coordinator, patched_ble):
        """Une pause du proxy, plus courte que le silence toléré, ne corrompt rien.

        Avant le correctif, cette seule pause déclenchait une cascade : chaque
        bloc héritait de la dernière trame du précédent, et 216 entrées sur 300
        finissaient mal placées, sans aucune erreur.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        dev = ProxyAvecLatence(make_broadcast(), _valeurs_indexees(2880), [0] * 1825,
                               latence=0.005, pause=0.03)
        with patched_ble(dev), patch(
            "custom_components.bwt_aqa_perla_ble.coordinator.BLE_NOTIFY_SILENCE", 0.1
        ):
            await coordinator._start_notify(dev)
            entries = await coordinator._lire_blocs(
                dev, ADRESSE_TAB_QUART, 100, 300, is_quart=True
            )
        assert len(entries) == 300
        mal_placees = [e for e in entries if e["litres"] != e["idx"] % 1000]
        assert not mal_placees, f"{len(mal_placees)} entrées mal placées"

    @pytest.mark.asyncio
    async def test_pause_excessive_echoue_sans_corrompre(
        self, coordinator, patched_ble
    ):
        """Au-delà du silence toléré, la lecture échoue au lieu de se décaler."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from homeassistant.helpers.update_coordinator import UpdateFailed
        dev = ProxyAvecLatence(make_broadcast(), _valeurs_indexees(2880), [0] * 1825,
                               latence=0.005, pause=0.06)
        with patched_ble(dev), patch(
            "custom_components.bwt_aqa_perla_ble.coordinator.BLE_NOTIFY_SILENCE", 0.03
        ):
            await coordinator._start_notify(dev)
            with pytest.raises(UpdateFailed, match="Incomplete block"):
                await coordinator._lire_blocs(
                    dev, ADRESSE_TAB_QUART, 100, 300, is_quart=True
                )


# ── Relance de la lecture d'historique ───────────────────────────────────────

class ProxyInstable(FakeBwtDevice):
    """Perd la dernière trame du premier bloc lors des `echecs` premières sessions."""

    def __init__(self, *args, echecs=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.echecs, self.sessions = echecs, 0

    async def start_notify(self, uuid, callback):
        self.sessions += 1
        self._bloc_de_session = 0
        await super().start_notify(uuid, callback)

    async def write_gatt_char(self, uuid, data):
        if data[0] == 0x03:
            self.break_sent = True
            return
        if self.sessions <= self.echecs and self._bloc_de_session == 0:
            self._bloc_de_session += 1
            from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_JOUR
            adresse = data[1] | (data[2] << 8)
            nb_oct = data[3] | (data[4] << 8)
            index = (adresse - ADRESSE_TAB_JOUR) // 2
            mots = self.jours[index:index + nb_oct // 2]
            trames = [make_notification(mots[i:i + 9], n)
                      for n, i in enumerate(range(0, len(mots), 9))]
            for t in trames[:-1]:           # la dernière trame se perd
                self._callback(None, bytearray(t))
            return
        self._bloc_de_session += 1
        return await super().write_gatt_char(uuid, data)


@pytest.fixture
def sans_delai_de_relance():
    with patch(
        "custom_components.bwt_aqa_perla_ble.coordinator._DELAI_ENTRE_TENTATIVES", 0
    ):
        yield


class TestRelanceHistorique:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("service", [
        "service_total_consumption",
        "service_history_consumption",
        "service_history_regenerations",
    ])
    async def test_lecture_ratee_puis_reussie(
        self, coordinator, patched_ble, sans_delai_de_relance, service
    ):
        """Un bloc incomplet à la première session ne fait échouer aucun service.

        Les trois services lisent l'historique par le même chemin : la relance
        doit les couvrir tous, et continuer à le faire si l'un d'eux évolue.
        """
        dev = ProxyInstable(make_broadcast(idx_jour=200, idx_quart=0), [0] * 2880,
                            [jour_word(150) for _ in range(1825)], echecs=1)
        with patched_ble(dev), patch(
            "custom_components.bwt_aqa_perla_ble.coordinator"
            ".async_ble_device_from_address", return_value=object(),
        ):
            result = await getattr(coordinator, service)()
        assert result, f"{service} n'a rien renvoyé"
        assert dev.sessions == 2, "une seconde session aurait dû être ouverte"
        if service == "service_total_consumption":
            assert result["days_count"] == 200
            assert result["total_liters"] == 200 * 150

    @pytest.mark.asyncio
    async def test_echec_persistant_apres_toutes_les_tentatives(
        self, coordinator, patched_ble, sans_delai_de_relance
    ):
        """Si chaque session échoue, le service remonte l'erreur."""
        from custom_components.bwt_aqa_perla_ble.coordinator import (
            _TENTATIVES_HISTORIQUE,
        )
        from homeassistant.helpers.update_coordinator import UpdateFailed

        dev = ProxyInstable(make_broadcast(idx_jour=200, idx_quart=0), [0] * 2880,
                            [jour_word(150) for _ in range(1825)], echecs=99)
        with patched_ble(dev), patch(
            "custom_components.bwt_aqa_perla_ble.coordinator"
            ".async_ble_device_from_address", return_value=object(),
        ):
            with pytest.raises(UpdateFailed, match="Incomplete block"):
                await coordinator.service_total_consumption()
        assert dev.sessions == _TENTATIVES_HISTORIQUE

    @pytest.mark.asyncio
    async def test_chaque_tentative_se_deconnecte(
        self, coordinator, patched_ble, sans_delai_de_relance
    ):
        """Une tentative ratée ferme sa session avant que la suivante n'ouvre."""
        dev = ProxyInstable(make_broadcast(idx_jour=200, idx_quart=0), [0] * 2880,
                            [jour_word(150) for _ in range(1825)], echecs=1)
        deconnexions = 0
        original = dev.disconnect

        async def compter():
            nonlocal deconnexions
            deconnexions += 1
            await original()
        dev.disconnect = compter

        with patched_ble(dev), patch(
            "custom_components.bwt_aqa_perla_ble.coordinator"
            ".async_ble_device_from_address", return_value=object(),
        ):
            await coordinator.service_total_consumption()
        assert deconnexions == dev.sessions == 2


# ── Apprentissage de la bascule, de bout en bout ─────────────────────────────

class TestApprentissageBascule:

    @pytest.mark.asyncio
    async def test_appris_en_observant_l_index(self, coordinator, fake_device, patched_ble, clock):
        """Deux cycles encadrant l'avance de l'index suffisent à dater la bascule."""
        clock.set(hour=9, minute=20)
        with patched_ble(fake_device(broadcast=make_broadcast(idx_jour=112))):
            await coordinator._run_rapide(object())
        assert coordinator._heure_bascule_texte() is None

        clock.advance(minutes=15)
        with patched_ble(fake_device(broadcast=make_broadcast(idx_jour=113))):
            result = await coordinator._run_rapide(object())
        assert coordinator._heure_bascule_texte() == "09:27"
        assert result["day_rollover"] == "09:27"
        assert result["day_rollover_observations"] == 1

    @pytest.mark.asyncio
    async def test_persiste_et_restaure(self, coordinator, fake_device, patched_ble, clock, store):
        from unittest.mock import MagicMock
        from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
        clock.set(hour=9, minute=20)
        with patched_ble(fake_device(broadcast=make_broadcast(idx_jour=112))):
            await coordinator._run_rapide(object())
        clock.advance(minutes=15)
        with patched_ble(fake_device(broadcast=make_broadcast(idx_jour=113))):
            await coordinator._run_rapide(object())

        neuf = BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")
        await neuf.async_load_stored_data()
        assert neuf._heure_bascule_texte() == "09:27"
        assert neuf._bascule.ancre[0] == 113

    @pytest.mark.asyncio
    async def test_nouvelle_installation_comportement_inchange(
        self, coordinator, fake_device, patched_ble
    ):
        """Avant toute observation, la dernière case est « hier », comme avant."""
        dev = fake_device(broadcast=make_broadcast(idx_jour=100, idx_quart=0),
                          jours=[jour_word(150) for _ in range(1825)])
        with patched_ble(dev):
            result = await coordinator.service_total_consumption()
        assert result["to_date"] == "2026-04-27"


# ── Ordonnancement autour de minuit ──────────────────────────────────────────

class TestOrdonnancementMinuit:
    """Hier et 7 jours sont recalculés au premier cycle après 00 h 20."""

    @staticmethod
    async def _cycle(coordinator):
        with patch.object(coordinator, "_resolve_ble_device", return_value=object()), \
             patch.object(coordinator, "_run_complet", AsyncMock(return_value={})) as complet, \
             patch.object(coordinator, "_run_rapide", AsyncMock(return_value={})):
            await coordinator._async_update_data()
        return "complet" if complet.called else "rapide"

    @pytest.mark.asyncio
    async def test_pas_avant_00h20(self, coordinator, clock):
        clock.set(hour=0, minute=10)
        coordinator._cycles_rapides = 1
        coordinator._jour_hier_semaine = "2026-04-27"
        assert await self._cycle(coordinator) == "rapide"

    @pytest.mark.asyncio
    async def test_force_apres_00h20(self, coordinator, clock):
        clock.set(hour=0, minute=25)
        coordinator._cycles_rapides = 1
        coordinator._jour_hier_semaine = "2026-04-27"
        assert await self._cycle(coordinator) == "complet"

    @pytest.mark.asyncio
    async def test_une_seule_fois_par_jour(self, coordinator, clock):
        clock.set(hour=10)
        coordinator._cycles_rapides = 1
        coordinator._jour_hier_semaine = "2026-04-28"
        assert await self._cycle(coordinator) == "rapide"


@pytest.mark.asyncio
async def test_issue_10_rejouee_de_bout_en_bout(coordinator, fake_device, patched_ble, clock):
    """Deux appels à 9 h et à 10 h, bascule de l'adoucisseur à 9 h 30 entre les deux.

    Avant ce correctif, les valeurs communes aux deux lectures changeaient
    toutes de date (19 sur 22 dans le rapport de jflefebvre06). Ici tout passe
    par les vraies sessions BLE : la veille, deux cycles encadrent la bascule
    et l'apprennent ; le jour même, la seconde lecture voit la case suivante
    ouverte. Chaque valeur doit garder sa date.
    """
    valeurs = [jour_word(10 * (k % 50) + 20) for k in range(1825)]

    async def lire(idx_jour):
        dev = fake_device(broadcast=make_broadcast(idx_jour=idx_jour, idx_quart=0),
                          jours=valeurs)
        with patched_ble(dev):
            h = await coordinator.service_history_consumption()
        return {f"{a}-{m}-{j}": v for a, ms in h.items() for m, js in ms.items()
                for j, v in js.items()}

    async def cycle(idx_jour):
        with patched_ble(fake_device(broadcast=make_broadcast(idx_jour=idx_jour))):
            await coordinator._run_rapide(object())

    # La veille : cycles de 9 h 25 et 9 h 40, la case 113 s'ouvre entre les deux
    clock.set(day=27, hour=9, minute=25)
    await cycle(112)
    clock.set(day=27, hour=9, minute=40)
    await cycle(113)
    assert coordinator._heure_bascule_texte() == "09:32"

    clock.set(day=28, hour=9, minute=0)
    avant = await lire(113)             # la case 114 n'existe pas encore
    clock.set(day=28, hour=10, minute=0)
    apres = await lire(114)             # ouverte à 9 h 30

    communes = set(avant) & set(apres)
    assert len(communes) >= 112
    changees = [d for d in communes if avant[d] != apres[d]]
    assert not changees, f"{len(changees)} dates ont changé de valeur"


@pytest.mark.asyncio
async def test_total_signale_si_la_bascule_est_apprise(coordinator, fake_device, patched_ble, clock):
    """`day_rollover_learned` passe à True dès la première bascule observée.

    Avant, le raccord entre cases et quarts est placé à minuit par défaut et le
    total peut être faux de quelques heures de consommation : une automatisation
    qui cumule ce total doit pouvoir attendre.
    """
    dev = lambda idx: fake_device(broadcast=make_broadcast(idx_jour=idx, idx_quart=0),
                                  jours=[jour_word(150) for _ in range(1825)])
    clock.set(hour=9, minute=20)
    with patched_ble(dev(112)):
        avant = await coordinator.service_total_consumption()
    assert avant["day_rollover_learned"] is False

    clock.set(hour=9, minute=35)
    with patched_ble(dev(113)):            # la lecture elle-même observe la bascule
        apres = await coordinator.service_total_consumption()
    assert apres["day_rollover_learned"] is True


class TestPassageDeMinuit:
    """La consommation du jour ne doit jamais baisser entre minuit et 00 h 20.

    Le quart 23 h 45 – 00 h 00 est écrit à minuit pile. Additionné sans être
    daté, il passait de la veille à la journée en cours ; le cycle complet de
    00 h 20 le retirait ensuite, et cette baisse faisait croire à Home
    Assistant que le compteur total_increasing avait été remis à zéro.
    """

    N = 1000     # index qui suit le quart 23 h 30 – 23 h 45

    def _quarts(self):
        q = [quart_word(10) for _ in range(2880)]
        q[self.N] = quart_word(50)            # 23 h 45 – 00 h 00 : la veille
        return q

    def _etat_a_23h50(self, coordinator, total=500):
        coordinator._date_dernier_complet = "2026-04-27"
        coordinator._jour_hier_semaine = "2026-04-27"
        coordinator._litres_jour_base = coordinator._litres_jour_total = total
        coordinator._index_base = coordinator._dernier_index_tab_quart = self.N
        coordinator._cycles_rapides = 1

    async def _cycle(self, coordinator, fake_device, patched_ble, idx_quart):
        from custom_components.bwt_aqa_perla_ble.const import KEY_CONSUMPTION_TODAY
        dev = fake_device(broadcast=make_broadcast(idx_quart=idx_quart), quarts=self._quarts())
        with patched_ble(dev), patch.object(coordinator, "_resolve_ble_device",
                                            return_value=object()):
            return (await coordinator._async_update_data())[KEY_CONSUMPTION_TODAY]

    @pytest.mark.asyncio
    async def test_dernier_quart_de_la_veille_exclu(
        self, coordinator, fake_device, patched_ble, clock
    ):
        self._etat_a_23h50(coordinator)
        clock.set(hour=0, minute=5)
        assert await self._cycle(coordinator, fake_device, patched_ble, self.N + 1) == 0

    @pytest.mark.asyncio
    async def test_aucune_baisse_jusqu_au_cycle_de_00h20(
        self, coordinator, fake_device, patched_ble, clock
    ):
        self._etat_a_23h50(coordinator)
        valeurs = []
        clock.set(hour=0, minute=5)
        valeurs.append(await self._cycle(coordinator, fake_device, patched_ble, self.N + 1))
        clock.set(hour=0, minute=20)                     # cycle complet forcé
        valeurs.append(await self._cycle(coordinator, fake_device, patched_ble, self.N + 2))
        assert valeurs == sorted(valeurs), f"la consommation du jour a baissé : {valeurs}"
        assert valeurs == [0, 10]

    @pytest.mark.asyncio
    async def test_veille_sans_consommation(
        self, coordinator, fake_device, patched_ble, clock
    ):
        """Veille à 0 L : pas de reset, et l'index de base reste celui de 23 h 30.

        Les quarts de la veille lus depuis cet index ne doivent pas être
        comptés dans la nouvelle journée.
        """
        self._etat_a_23h50(coordinator, total=0)
        coordinator._index_base = self.N - 2             # dernier cycle complet
        clock.set(hour=0, minute=5)
        assert await self._cycle(coordinator, fake_device, patched_ble, self.N + 1) == 0


# ── Compteur d'eau cumulé ────────────────────────────────────────────────────

def _quarts_distincts():
    """Le quart d'index i vaut i % 50 + 1 litres : chaque erreur se voit."""
    return [quart_word(i % 50 + 1) for i in range(2880)]


def _litres(debut, fin):
    return sum(i % 50 + 1 for i in range(debut, fin))


class TestCompteurEau:

    async def _rapide(self, coordinator, fake_device, patched_ble, idx):
        from custom_components.bwt_aqa_perla_ble.const import KEY_WATER_METER
        dev = fake_device(broadcast=make_broadcast(idx_quart=idx), quarts=_quarts_distincts())
        with patched_ble(dev):
            return (await coordinator._run_rapide(object()))[KEY_WATER_METER]

    async def _complet(self, coordinator, fake_device, patched_ble, idx, **kw):
        from custom_components.bwt_aqa_perla_ble.const import KEY_WATER_METER
        dev = fake_device(broadcast=make_broadcast(idx_quart=idx, **kw),
                          quarts=_quarts_distincts())
        with patched_ble(dev):
            return (await coordinator._run_complet(object()))[KEY_WATER_METER]

    @pytest.mark.asyncio
    async def test_demarre_a_zero(self, coordinator, fake_device, patched_ble):
        coordinator._index_base = 100
        assert await self._rapide(coordinator, fake_device, patched_ble, 100) == 0

    @pytest.mark.asyncio
    async def test_chaque_quart_compte_une_seule_fois(
        self, coordinator, fake_device, patched_ble, clock
    ):
        """Cycles rapides et complet alternés : ni oubli, ni double comptage."""
        coordinator._index_base = 100
        await self._rapide(coordinator, fake_device, patched_ble, 100)
        clock.advance(minutes=15)
        await self._rapide(coordinator, fake_device, patched_ble, 101)
        clock.advance(minutes=15)
        await self._complet(coordinator, fake_device, patched_ble, 102)
        clock.advance(minutes=15)
        valeur = await self._rapide(coordinator, fake_device, patched_ble, 103)
        assert valeur == _litres(100, 103)

    @pytest.mark.asyncio
    async def test_releve_repete_sans_nouveau_quart(
        self, coordinator, fake_device, patched_ble, clock
    ):
        coordinator._index_base = 100
        await self._rapide(coordinator, fake_device, patched_ble, 100)
        clock.advance(minutes=15)
        premier = await self._rapide(coordinator, fake_device, patched_ble, 101)
        clock.advance(minutes=5)
        assert await self._rapide(coordinator, fake_device, patched_ble, 101) == premier

    @pytest.mark.asyncio
    async def test_quart_de_23h45_compte(self, coordinator, fake_device, patched_ble, clock):
        """Le quart écrit à minuit entre dans le compteur, pas dans la journée."""
        from custom_components.bwt_aqa_perla_ble.const import (
            KEY_CONSUMPTION_TODAY, KEY_WATER_METER,
        )
        n = 1000
        clock.set(day=27, hour=23, minute=50)
        coordinator._index_base = n
        await self._rapide(coordinator, fake_device, patched_ble, n)
        coordinator._litres_jour_base = coordinator._litres_jour_total = 0

        clock.set(day=28, hour=0, minute=5)
        dev = fake_device(broadcast=make_broadcast(idx_quart=n + 1), quarts=_quarts_distincts())
        with patched_ble(dev):
            r = await coordinator._run_rapide(object())
        assert r[KEY_CONSUMPTION_TODAY] == 0
        assert r[KEY_WATER_METER] == n % 50 + 1

    @pytest.mark.asyncio
    async def test_rattrapage_apres_un_arret_de_dix_jours(
        self, coordinator, fake_device, patched_ble, clock, store
    ):
        """Au redémarrage, les quarts écoulés pendant l'arrêt sont comptés.

        Dix jours, c'est plus que les 7 jours lus par le premier cycle : le
        compteur doit lire lui-même les quarts manquants.
        """
        from unittest.mock import MagicMock
        from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
        coordinator._index_base = 500
        await self._rapide(coordinator, fake_device, patched_ble, 500)

        clock.advance(days=10)
        neuf = BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")
        await neuf.async_load_stored_data()
        valeur = await self._complet(neuf, fake_device, patched_ble, 500 + 960)
        assert valeur == _litres(500, 1460)

    @pytest.mark.asyncio
    async def test_persiste(self, coordinator, fake_device, patched_ble, clock, store):
        from unittest.mock import MagicMock
        from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
        coordinator._index_base = 100
        await self._rapide(coordinator, fake_device, patched_ble, 100)
        clock.advance(minutes=15)
        valeur = await self._rapide(coordinator, fake_device, patched_ble, 101)

        neuf = BwtCoordinator(MagicMock(), "03:12:00:34:00:5E")
        await neuf.async_load_stored_data()
        assert neuf._compteur_litres == valeur and neuf._compteur_idx == 101

    @pytest.mark.asyncio
    async def test_index_reinitialise_sans_recompter(
        self, coordinator, fake_device, patched_ble, clock
    ):
        """Un index qui recule ne doit pas faire recompter un mois de données."""
        coordinator._index_base = 2000
        await self._rapide(coordinator, fake_device, patched_ble, 2000)
        clock.advance(minutes=15)
        coordinator._index_base = 100
        assert await self._rapide(coordinator, fake_device, patched_ble, 100) == 0
        assert coordinator._compteur_idx == 100

    @pytest.mark.asyncio
    async def test_cycle_rapide_au_passage_par_la_fin_du_buffer(
        self, coordinator, fake_device, patched_ble, clock
    ):
        """Les quarts de fin et de début de buffer sont lus à leur vraie adresse."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART, MAX_TAB_QUART
        coordinator._index_base = MAX_TAB_QUART - 2
        await self._rapide(coordinator, fake_device, patched_ble, MAX_TAB_QUART - 2)
        clock.advance(minutes=45)
        dev = fake_device(broadcast=make_broadcast(idx_quart=1, loop_quart=True),
                          quarts=_quarts_distincts())
        with patched_ble(dev):
            await coordinator._run_rapide(object())
        for adresse, nb_oct in dev.reads:
            assert (adresse - ADRESSE_TAB_QUART) // 2 + nb_oct // 2 <= MAX_TAB_QUART
        assert coordinator._compteur_litres == _litres(MAX_TAB_QUART - 2, MAX_TAB_QUART) + _litres(0, 1)
