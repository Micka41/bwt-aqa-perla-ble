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
        dev = fake_device(broadcast=make_broadcast(idx_quart=20, idx_jour=100))
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
    async def test_wrap_a_index_zero(self, coordinator, fake_device, patched_ble):
        """Cas limite idx == 0 : tout doit être lu en fin de buffer."""
        dev = fake_device(broadcast=make_broadcast(idx_quart=0, idx_jour=0))
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
    @pytest.mark.asyncio
    async def test_total_consumption(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_jour=100),
                          jours=[jour_word(150) for _ in range(1825)])
        with patched_ble(dev):
            result = await coordinator.service_total_consumption()
        assert result["days_count"] == 100
        assert result["total_liters"] == 100 * 150
        assert result["from_date"] < result["to_date"]

    @pytest.mark.asyncio
    async def test_historique_vide(self, coordinator, fake_device, patched_ble):
        dev = fake_device(broadcast=make_broadcast(idx_jour=0))
        with patched_ble(dev):
            result = await coordinator.service_total_consumption()
        assert result["days_count"] == 0
        assert result["from_date"] is None

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
    async def test_historique_regenerations(self, coordinator, fake_device, patched_ble):
        jours = [jour_word(100, regens=1 if i % 5 == 0 else 0) for i in range(1825)]
        dev = fake_device(broadcast=make_broadcast(idx_jour=40), jours=jours)
        with patched_ble(dev):
            result = await coordinator.service_history_regenerations()
        total = sum(v for a in result.values() for m in a.values() for v in m.values())
        assert total > 0

    @pytest.mark.asyncio
    async def test_buffer_plein_lu_en_deux_parties(self, coordinator, fake_device, patched_ble):
        """loop_jour actif : les 1825 jours doivent être lus, wrap inclus."""
        from custom_components.bwt_aqa_perla_ble.const import MAX_TAB_JOUR
        dev = fake_device(
            broadcast=make_broadcast(idx_jour=300, loop_jour=True),
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

    @pytest.mark.asyncio
    async def test_numero_de_sequence_inattendu_tolere(
        self, coordinator, fake_device, patched_ble
    ):
        """Un numéro de séquence inattendu ne doit pas faire échouer la lecture.

        Le firmware A22X V1.18 annonce des numéros qui ne repartent pas de zéro
        à chaque bloc (issue #10). La datation s'appuyant sur l'ordre d'arrivée
        des trames, ces numéros sont seulement journalisés.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from conftest import make_notification

        dev = fake_device()
        original = dev.write_gatt_char

        async def write_sequence_decalee(uuid, data):
            if data[0] == 0x03:
                return await original(uuid, data)
            dev._callback(None, bytearray(
                make_notification([quart_word(7)] * 9, 6)   # annonce 6, pas 0
            ))
        dev.write_gatt_char = write_sequence_decalee

        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 200, 9, is_quart=True
            )
        # Les index restent déduits de la position, donc corrects
        assert [e["idx"] for e in entries] == list(range(200, 209))

    @pytest.mark.asyncio
    async def test_trame_perdue_ne_decale_pas_les_index(
        self, coordinator, fake_device, patched_ble
    ):
        """Une trame perdue laisse un trou, elle ne décale pas ce qui suit.

        Sans le numéro de séquence, la deuxième trame reçue serait prise pour
        la deuxième émise et ses neuf entrées seraient datées neuf crans trop
        tôt — le décalage de l'issue #9 sous une autre forme.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from conftest import make_notification

        dev = fake_device()
        original = dev.write_gatt_char

        async def write_avec_trame_perdue(uuid, data):
            if data[0] == 0x03:
                return await original(uuid, data)
            # Séquences 0 et 2 : la trame 1 ne nous est jamais parvenue
            dev._callback(None, bytearray(make_notification([quart_word(1)] * 9, 0)))
            dev._callback(None, bytearray(make_notification([quart_word(3)] * 9, 2)))
        dev.write_gatt_char = write_avec_trame_perdue

        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 100, 27, is_quart=True
            )

        idx = [e["idx"] for e in entries]
        assert idx[:9] == list(range(100, 109))
        assert idx[9:] == list(range(118, 127)), (
            "la trame 2 doit être placée à son rang réel, pas à la suite"
        )

    @pytest.mark.asyncio
    async def test_premieres_trames_perdues(
        self, coordinator, fake_device, patched_ble
    ):
        """Issue #10 : quand les premières trames manquent, le rang les situe.

        La première trame reçue annonce 6 parce que les six précédentes se sont
        perdues. Ses entrées appartiennent au rang 6, pas au rang 0 : les
        compter dans l'ordre d'arrivée les daterait six trames trop tôt.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from conftest import make_notification

        dev = fake_device()
        original = dev.write_gatt_char

        async def write_debut_perdu(uuid, data):
            if data[0] == 0x03:
                return await original(uuid, data)
            for rang in (6, 7):          # les rangs 0 à 5 ne sont jamais arrivés
                dev._callback(None, bytearray(
                    make_notification([quart_word(5)] * 9, rang)
                ))
        dev.write_gatt_char = write_debut_perdu

        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 300, 90, is_quart=True
            )

        # Rang 6 → index_os + 54, et non index_os
        assert [e["idx"] for e in entries] == list(range(354, 372))

    @pytest.mark.asyncio
    async def test_trame_perdue_ne_decale_pas_les_index(
        self, coordinator, fake_device, patched_ble
    ):
        """Une trame perdue laisse un trou, elle ne décale pas ce qui suit.

        Sans le numéro de séquence, la deuxième trame reçue serait prise pour
        la deuxième émise et ses neuf entrées seraient datées neuf crans trop
        tôt — le décalage de l'issue #9 sous une autre forme.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from conftest import make_notification

        dev = fake_device()
        original = dev.write_gatt_char

        async def write_avec_trame_perdue(uuid, data):
            if data[0] == 0x03:
                return await original(uuid, data)
            # Séquences 0 et 2 : la trame 1 ne nous est jamais parvenue
            dev._callback(None, bytearray(make_notification([quart_word(1)] * 9, 0)))
            dev._callback(None, bytearray(make_notification([quart_word(3)] * 9, 2)))
        dev.write_gatt_char = write_avec_trame_perdue

        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 100, 27, is_quart=True
            )

        idx = [e["idx"] for e in entries]
        assert idx[:9] == list(range(100, 109))
        assert idx[9:] == list(range(118, 127)), (
            "la trame 2 doit être placée à son rang réel, pas à la suite"
        )

    @pytest.mark.asyncio
    async def test_sequence_ne_repartant_pas_de_zero(
        self, coordinator, fake_device, patched_ble
    ):
        """Firmware V1.18 : la séquence se poursuit d'un bloc à l'autre (issue #10)."""
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from conftest import make_notification

        dev = fake_device()
        original = dev.write_gatt_char

        async def write_depuis_six(uuid, data):
            if data[0] == 0x03:
                return await original(uuid, data)
            for offset in (6, 7):
                dev._callback(None, bytearray(
                    make_notification([quart_word(5)] * 9, offset)
                ))
        dev.write_gatt_char = write_depuis_six

        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 300, 18, is_quart=True
            )
        assert [e["idx"] for e in entries] == list(range(300, 318))

    @pytest.mark.asyncio
    async def test_repli_si_le_champ_nest_pas_un_compteur(
        self, coordinator, fake_device, patched_ble
    ):
        """Un champ au comportement imprévisible ne doit pas corrompre la datation.

        Si les numéros reculent ou dépassent le bloc demandé, ce n'est pas un
        compteur de séquence : l'ordre d'arrivée reprend la main.
        """
        from custom_components.bwt_aqa_perla_ble.const import ADRESSE_TAB_QUART
        from conftest import make_notification

        dev = fake_device()
        original = dev.write_gatt_char

        async def write_incoherent(uuid, data):
            if data[0] == 0x03:
                return await original(uuid, data)
            for valeur in (500, 12, 999):      # ni monotone, ni borné
                dev._callback(None, bytearray(
                    make_notification([quart_word(2)] * 9, valeur)
                ))
        dev.write_gatt_char = write_incoherent

        with patched_ble(dev):
            from custom_components.bwt_aqa_perla_ble.coordinator import establish_connection
            client = await establish_connection(None, None, None)
            await coordinator._start_notify(client)
            entries = await coordinator._lire_blocs(
                client, ADRESSE_TAB_QUART, 400, 27, is_quart=True
            )
        assert [e["idx"] for e in entries] == list(range(400, 427))
