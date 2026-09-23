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
            make_broadcast(idx_jour=200), [0] * 2880,
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
        dev = ProxyInstable(make_broadcast(idx_jour=200), [0] * 2880,
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

        dev = ProxyInstable(make_broadcast(idx_jour=200), [0] * 2880,
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
        dev = ProxyInstable(make_broadcast(idx_jour=200), [0] * 2880,
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
