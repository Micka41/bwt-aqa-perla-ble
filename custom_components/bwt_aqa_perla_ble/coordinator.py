"""DataUpdateCoordinator for BWT AQA Perla.

Stratégie duale portée de bwt_service.py :

  Cycle RAPIDE (toutes les 15 min) :
    BROADCAST + quarts depuis _index_base → ~5s BLE
    litres_jour = _litres_jour_base + delta

  Cycle COMPLET (toutes les 1h, forcé à 04h00) :
    BROADCAST + derniers 120 quarts + 8 derniers jours → ~20s BLE
    recalcule _litres_jour_base et _index_base
    met à jour conso_hier et conso_semaine (stables, protégées)

  Reset minuit :
    _litres_jour_base = 0, _index_base = _dernier_index_tab_quart

  conso_hier / conso_semaine : mémorisées, ne mises à jour que si valeur > 0
  (le BWT consolide J-1 vers 04h00, pas à minuit).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SCAN_INTERVAL,
    INTERVALLE_COMPLET_H,
    INTERVALLE_RAPIDE_S,
    NB_QUARTS_COMPLET,
    NB_JOURS_COMPLET,
    UUID_READ1,
    UUID_WRITE,
    UUID_BROADCAST,
    UUID_OTHER,
    ADRESSE_TAB_QUART,
    ADRESSE_TAB_JOUR,
    MAX_TAB_QUART,
    BLE_CONNECT_TIMEOUT,
    BLE_NOTIFY_SILENCE,
    BLE_NOTIFY_TIMEOUT,
    KEY_SALT_PCT,
    KEY_SALT_KG,
    KEY_SALT_TOTAL_KG,
    KEY_SALT_ALARM,
    KEY_CONSUMPTION_TODAY,
    KEY_CONSUMPTION_YESTERDAY,
    KEY_CONSUMPTION_WEEK,
    KEY_REGEN_TODAY,
    KEY_REGEN_YESTERDAY,
    KEY_SALT_AUTONOMY_DAYS,
    KEY_SALT_AUTONOMY_WEEKS,
    KEY_AVG_DAILY_30D,
    KEY_LAST_SYNC,
    KEY_FIRMWARE,
)

_LOGGER = logging.getLogger(__name__)

_CYCLES_PAR_COMPLET = (INTERVALLE_COMPLET_H * 3600) // INTERVALLE_RAPIDE_S


# ── Helpers protocole ────────────────────────────────────────────────────────

def _get_word_le(buf: bytes, offset: int) -> int:
    return buf[offset] | (buf[offset + 1] << 8)


def _get_word_from(buf: bytes, index: int, first_min: bool) -> int:
    a = buf[index + 1] & 0xFF
    b = buf[index]     & 0xFF
    return (a * 256 + b) if first_min else (b * 256 + a)


def _decode_broadcast(buf: bytes) -> dict[str, Any]:
    if len(buf) < 15:
        raise UpdateFailed(f"BROADCAST trop court : {len(buf)} octets")
    qte_sel    = _get_word_le(buf, 0) + _get_word_le(buf, 2) * 65536
    capa_total = _get_word_le(buf, 10) * 1000
    flags      = buf[12]
    pct        = max(0, min(100, (qte_sel * 100) // capa_total)) if capa_total > 0 else 0
    return {
        "qte_sel_restant":  qte_sel,
        "index_tab_quart":  _get_word_le(buf, 4),
        "index_tab_jour":   _get_word_le(buf, 6),
        "vol_sel_rege":     _get_word_le(buf, 8),
        "capa_total_sel":   capa_total,
        "alarme":           bool(flags & 0x01),
        "loop_quart":       bool(flags & 0x02),
        "loop_jour":        bool(flags & 0x04),
        "pourcentage_sel":  pct,
        "version":          f"A22X V{buf[13]}.{buf[14]}",
    }


def _build_read_cmd(adresse: int, longueur: int, inter_ms: int = 20) -> bytes:
    return bytes([
        0x02,
        adresse & 0xFF, (adresse >> 8) & 0xFF,
        longueur & 0xFF, (longueur >> 8) & 0xFF,
        inter_ms & 0xFF, (inter_ms >> 8) & 0xFF,
    ])


def _build_break_cmd() -> bytes:
    return bytes([0x03, 0x00, 0x00])


def _decode_notification(buf: bytes, is_quart: bool) -> tuple[int, list[dict]]:
    if len(buf) < 20:
        return -1, []
    index   = _get_word_from(buf, 0, True)
    entries = []
    for i in range(9):
        word = _get_word_from(buf, 2 + i * 2, False)
        if word > 32767:
            break
        if is_quart:
            entries.append({"litres": word & 0x03FF, "rege": bool(word & 0x0800)})
        else:
            entries.append({"litres": (word & 0x07FF) * 10, "rege": (word >> 12) & 0x03})
    return index, entries


# ── Coordinator ──────────────────────────────────────────────────────────────

class BwtCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator BWT AQA Perla — dual cycle rapide/complet."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )
        self.address = address

        # Notifications BLE
        self._notifications: list[bytes] = []
        self._notify_event = asyncio.Event()

        # État persistant entre cycles (porté de BwtService)
        self._cycles_rapides: int       = 0
        self._date_dernier_complet: str = ""
        self._date_remise_a_zero: str   = ""

        # Accumulateur conso jour
        self._litres_jour_base:  int = 0
        self._index_base:        int = 0
        self._litres_jour_total: int = 0
        self._dernier_index_tab_quart: int = 0

        # Valeurs stables (mémorisées, protégées contre non-consolidation)
        self._conso_hier_stable:    int = 0
        self._conso_semaine_stable: int = 0
        self._regens_jour_stable:   int = 0
        self._regens_hier_stable:   int = 0
        self._date_hier_stable:     str = ""
        self._firmware:             str = ""

        # Moyenne 30 jours glissants
        self._avg_daily_30d: float | None = None

        # Autonomie sel
        self._vol_sel_rege:         int = 0   # grammes de sel par régénération
        self._autonomie_jours:      int | None = None
        self._autonomie_semaines:   float | None = None

    # ── Hook principal ────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(
                f"BWT AQA Perla ({self.address}) introuvable — "
                "vérifiez portée BLE ou proxy ESPHome"
            )

        aujourd_hui    = dt_util.now().date().isoformat()
        now_hm         = dt_util.now().hour * 60 + dt_util.now().minute
        changement_jour = aujourd_hui != self._date_dernier_complet

        # Reset minuit — une seule fois par jour
        if (changement_jour
                and self._date_dernier_complet != ""
                and self._date_remise_a_zero != aujourd_hui
                and self._litres_jour_total > 0):
            _LOGGER.info("Minuit — remise à zéro conso jour")
            self._regens_hier_stable  = self._regens_jour_stable   # ← sauvegarder avant reset
            self._litres_jour_base  = 0
            self._litres_jour_total = 0
            self._index_base        = self._dernier_index_tab_quart
            self._date_remise_a_zero = aujourd_hui

        # Sélection du type de cycle
        nouveau_jour_apres_04h = changement_jour and now_hm >= 240
        faire_complet = (
            self._cycles_rapides % _CYCLES_PAR_COMPLET == 0
            or nouveau_jour_apres_04h
        )

        try:
            if faire_complet:
                if nouveau_jour_apres_04h and self._cycles_rapides > 0:
                    _LOGGER.info("Nouveau jour après 04h — cycle complet forcé")
                    self._date_dernier_complet = aujourd_hui
                result = await self._run_complet(ble_device)
            else:
                result = await self._run_rapide(ble_device)
        except BleakError as err:
            raise UpdateFailed(f"Erreur BLE : {err}") from err

        self._cycles_rapides += 1
        return result

    # ── Cycle rapide ──────────────────────────────────────────────────

    async def _run_rapide(self, ble_device) -> dict[str, Any]:
        """BROADCAST + quarts depuis _index_base → delta conso jour."""
        client = await establish_connection(
            BleakClient,
            ble_device,
            self.address,
            max_attempts=3,
            ctor_kwargs={"timeout": BLE_CONNECT_TIMEOUT},
        )
        try:
            await self._start_notify(client)
            await client.read_gatt_char(UUID_OTHER)  # auth

            bcast = _decode_broadcast(await client.read_gatt_char(UUID_BROADCAST))
            self._dernier_index_tab_quart = bcast["index_tab_quart"]
            if bcast["version"]:
                self._firmware = bcast["version"]
            if bcast["vol_sel_rege"] > 0:
                self._vol_sel_rege = bcast["vol_sel_rege"]

            # Quarts nouveaux depuis _index_base
            idx = bcast["index_tab_quart"]
            nb  = (idx - self._index_base) % MAX_TAB_QUART
            quarts: list[dict] = []
            if nb > 0:
                quarts = await self._lire_blocs(
                    client, ADRESSE_TAB_QUART, self._index_base, nb, is_quart=True
                )

            await client.write_gatt_char(UUID_WRITE, _build_break_cmd())
            await client.stop_notify(UUID_READ1)
        finally:
            await client.disconnect()

        delta = sum(q["litres"] for q in quarts)
        self._litres_jour_total = self._litres_jour_base + delta
        _LOGGER.debug(
            "Rapide — base=%d + delta=%d = %d L",
            self._litres_jour_base, delta, self._litres_jour_total,
        )
        return self._build_result(bcast)

    # ── Cycle complet ─────────────────────────────────────────────────

    async def _run_complet(self, ble_device) -> dict[str, Any]:
        """BROADCAST + 120 quarts + 8 jours → recalibrage complet."""
        client = await establish_connection(
            BleakClient,
            ble_device,
            self.address,
            max_attempts=3,
            ctor_kwargs={"timeout": BLE_CONNECT_TIMEOUT},
        )
        try:
            await self._start_notify(client)
            await client.read_gatt_char(UUID_OTHER)

            bcast = _decode_broadcast(await client.read_gatt_char(UUID_BROADCAST))
            self._dernier_index_tab_quart = bcast["index_tab_quart"]
            if bcast["version"]:
                self._firmware = bcast["version"]

            # Quarts
            idx_q = bcast["index_tab_quart"]
            nb_q  = min(idx_q, NB_QUARTS_COMPLET)
            quarts: list[dict] = []
            if nb_q > 0:
                quarts = await self._lire_blocs(
                    client, ADRESSE_TAB_QUART,
                    idx_q - nb_q, nb_q, is_quart=True
                )

            # Jours
            idx_j = bcast["index_tab_jour"]
            nb_j  = min(idx_j, NB_JOURS_COMPLET)
            jours: list[dict] = []
            if nb_j > 0:
                jours = await self._lire_blocs(
                    client, ADRESSE_TAB_JOUR,
                    idx_j - nb_j, nb_j, is_quart=False
                )

            await client.write_gatt_char(UUID_WRITE, _build_break_cmd())
            await client.stop_notify(UUID_READ1)
        finally:
            await client.disconnect()

        # Assigner les dates ET heures aux quarts (ancre = dernier quart terminé)
        _now     = dt_util.now()
        _min_arr = (_now.minute // 15) * 15
        ancre_q  = _now.replace(minute=_min_arr, second=0, microsecond=0) - timedelta(minutes=15)
        quarts_dates = [
            {
                **q,
                "date":  (ancre_q - timedelta(minutes=15 * (len(quarts) - 1 - i))).strftime("%Y-%m-%d"),
                "heure": (ancre_q - timedelta(minutes=15 * (len(quarts) - 1 - i))).hour,
            }
            for i, q in enumerate(quarts)
        ]

        # Assigner les dates aux jours (ancre = hier)
        hier_d = dt_util.now().date() - timedelta(days=1)
        jours_dates = [
            {**j, "date": (hier_d - timedelta(days=(len(jours) - 1 - i))).isoformat()}
            for i, j in enumerate(jours)
        ]

        # Recalibrer conso jour depuis les quarts d'aujourd'hui
        aujourd_hui_str = dt_util.now().date().isoformat()
        quarts_auj = [q for q in quarts_dates if q["date"] == aujourd_hui_str]
        self._litres_jour_base  = sum(q["litres"] for q in quarts_auj)
        self._index_base        = bcast["index_tab_quart"]
        self._litres_jour_total = self._litres_jour_base
        self._date_dernier_complet = aujourd_hui_str

        # Régénérations du jour : transitions False → True dans les quarts d'aujourd'hui
        regens, prev = 0, False
        for q in quarts_auj:
            if q["rege"] and not prev:
                regens += 1
            prev = q["rege"]
        self._regens_jour_stable = regens

        # Hier / semaine
        self._mettre_a_jour_hier_semaine({j["date"]: j for j in jours_dates})

        # Moyenne 30 jours glissants (J-1 à J-30, jours consolidés uniquement)
        hier_d_iso = (dt_util.now().date() - timedelta(days=1)).isoformat()
        jours_30 = [
            j["litres"] for j in jours_dates
            if j["date"] <= hier_d_iso   # exclure aujourd'hui non consolidé
        ][-30:]   # 30 derniers jours disponibles
        self._avg_daily_30d = round(sum(jours_30) / len(jours_30), 1) if jours_30 else None
        _LOGGER.debug("Moyenne 30j : %s L/j (%d jours)", self._avg_daily_30d, len(jours_30))

        # Autonomie sel : sel_restant / (regens_moy_jour × sel_par_regen)
        # Moyenne sur les jours disponibles avec au moins 1 régénération
        self._calculer_autonomie(bcast, jours_dates)

        _LOGGER.info(
            "Complet — base=%d L  index=%d  regens=%d  hier=%d L  semaine=%d L",
            self._litres_jour_base, self._index_base,
            self._regens_jour_stable, self._conso_hier_stable, self._conso_semaine_stable,
        )
        return self._build_result(bcast)

    # ── Calcul de l'autonomie sel ─────────────────────────────────────

    def _calculer_autonomie(self, bcast: dict, jours_dates: list[dict]) -> None:
        """
        Réplique exacte de CalcAutonomie() du firmware Java BWT.

        Principe : on compte les jours en partant du plus récent et en
        soustrayant les régénérations réelles jusqu'à épuisement du sel estimé.

          nb_rege_restant = qte_sel_restant // vol_sel_rege
          Pour chaque jour (du plus récent au plus ancien) :
            nb_rege_restant -= rege_du_jour
            si nb_rege_restant <= 0 → stop
            sinon nb_jours += 1

        Avantage vs moyenne : fonctionne correctement pour les adoucisseurs
        à faible fréquence de régénération (ex: 1 regen/semaine → la moyenne
        sur 7 jours bloquerait l'estimation à 7j max).

        Nécessite NB_JOURS_COMPLET >= 30 pour être fiable.
        """
        vol_rege = bcast.get("vol_sel_rege", 0) or self._vol_sel_rege
        qte_sel  = bcast.get("qte_sel_restant", 0)

        if vol_rege <= 0 or qte_sel <= 0:
            _LOGGER.debug("Autonomie non calculable (vol_rege=%d qte_sel=%d)", vol_rege, qte_sel)
            self._autonomie_jours    = None
            self._autonomie_semaines = None
            return

        # Trier du plus ancien au plus récent (même ordre que le Java)
        jours_tries = sorted(jours_dates, key=lambda e: e["date"])
        taille_tab  = len(jours_tries)

        if taille_tab == 0:
            self._autonomie_jours    = None
            self._autonomie_semaines = None
            return

        nb_rege_restant = qte_sel // vol_rege
        nb_jours = 0

        # Parcours du plus récent au plus ancien
        for i in range(1, taille_tab + 1):
            nb_rege_restant -= jours_tries[taille_tab - i]["rege"]
            if nb_rege_restant <= 0:
                break
            nb_jours += 1

        self._autonomie_jours    = nb_jours
        self._autonomie_semaines = nb_jours // 7   # division entière, cohérent avec CalcAutonomie() Java
        _LOGGER.info(
            "Autonomie sel : %d jours (%.1f semaines) "
            "[sel=%dg  nb_rege_possible=%d  historique=%d jours]",
            self._autonomie_jours, self._autonomie_semaines,
            qte_sel, qte_sel // vol_rege, taille_tab,
        )

    # ── Stabilisation hier / semaine ─────────────────────────────────

    def _mettre_a_jour_hier_semaine(self, jours_dict: dict[str, dict]) -> None:
        """Protège contre la non-consolidation du BWT (J-1 consolidé vers 04h00).
        Note : _regens_hier_stable est géré au reset minuit, pas ici."""
        hier_iso    = (dt_util.now().date() - timedelta(days=1)).isoformat()
        entree_hier = jours_dict.get(hier_iso)
        val_hier    = entree_hier["litres"] if entree_hier else 0

        if val_hier > 0:
            self._conso_hier_stable = val_hier
            self._date_hier_stable  = hier_iso
            _LOGGER.info("Conso hier consolidée : %d L", self._conso_hier_stable)
        elif self._date_hier_stable != hier_iso and self._conso_hier_stable == 0:
            # Pas encore consolidé → chercher dernière valeur non-nulle
            for i in range(1, 8):
                d = (dt_util.now().date() - timedelta(days=i)).isoformat()
                e = jours_dict.get(d)
                if e and e["litres"] > 0:
                    self._conso_hier_stable = e["litres"]
                    _LOGGER.info(
                        "Conso hier provisoire depuis %s : %d L", d, self._conso_hier_stable
                    )
                    break

        # Semaine : 7 jours J-1..J-7 (mis à jour uniquement si J-1 consolidé)
        if entree_hier is not None:
            self._conso_semaine_stable = sum(
                jours_dict[d]["litres"]
                for i in range(1, 8)
                if (d := (dt_util.now().date() - timedelta(days=i)).isoformat()) in jours_dict
            )
            _LOGGER.info("Conso semaine : %d L", self._conso_semaine_stable)
        else:
            _LOGGER.info(
                "Conso semaine : J-1 non consolidé — stable conservée (%d L)",
                self._conso_semaine_stable,
            )

    # ── Lecture des blocs mémoire flash ──────────────────────────────

    async def _lire_blocs(
        self,
        client: BleakClient,
        adresse_base: int,
        index_os: int,
        nb: int,
        is_quart: bool,
    ) -> list[dict]:
        """Lit nb entrées en envoyant des commandes READ_BUFFER par blocs de 90."""
        BLOCK_SIZE = 90
        resultats: list[dict] = []
        restant = nb

        while restant > 0:
            bloc    = min(restant, BLOCK_SIZE)
            nb_oct  = bloc * 2
            adresse = adresse_base + 2 * index_os
            nb_tr   = (nb_oct + 17) // 18

            self._notifications.clear()
            await client.write_gatt_char(UUID_WRITE, _build_read_cmd(adresse, nb_oct))
            await self._attendre_notifications(nb_tr)

            if not self._notifications:
                _LOGGER.warning("Aucune notification reçue @ %#x (%d entrées)", adresse, bloc)
                break

            for notif in self._notifications:
                _, entries = _decode_notification(notif, is_quart)
                resultats.extend(entries)

            index_os += bloc
            restant  -= bloc

        return resultats

    # ── Gestion des notifications BLE ────────────────────────────────

    async def _start_notify(self, client: BleakClient) -> None:
        self._notifications.clear()
        self._notify_event.clear()
        await client.start_notify(UUID_READ1, self._on_notification)

    def _on_notification(self, sender, payload: bytearray) -> None:
        self._notifications.append(bytes(payload))
        self._notify_event.set()

    async def _attendre_notifications(
        self, nb_attendues: int, timeout: float = BLE_NOTIFY_TIMEOUT
    ) -> None:
        """Attend les trames avec détection de silence = fin de bloc."""
        loop     = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while True:
            restant  = deadline - loop.time()
            if restant <= 0:
                break
            nb_avant = len(self._notifications)
            self._notify_event.clear()
            try:
                await asyncio.wait_for(
                    self._notify_event.wait(),
                    timeout=min(BLE_NOTIFY_SILENCE, restant),
                )
                if len(self._notifications) >= nb_attendues:
                    break
            except asyncio.TimeoutError:
                if len(self._notifications) > nb_avant:
                    continue   # encore actif
                break          # silence prolongé = bloc terminé

    # ── Construction du résultat HA ───────────────────────────────────

    def _build_result(self, bcast: dict) -> dict[str, Any]:
        return {
            KEY_SALT_PCT:              bcast["pourcentage_sel"],
            KEY_SALT_KG:               round(bcast["qte_sel_restant"] / 1000, 2),
            KEY_SALT_TOTAL_KG:         round(bcast["capa_total_sel"]  / 1000, 2),
            KEY_SALT_ALARM:            bcast["alarme"],
            KEY_CONSUMPTION_TODAY:     self._litres_jour_total,
            KEY_CONSUMPTION_YESTERDAY: self._conso_hier_stable or None,
            KEY_CONSUMPTION_WEEK:      self._conso_semaine_stable or None,
            KEY_REGEN_TODAY:           self._regens_jour_stable,
            KEY_REGEN_YESTERDAY:       self._regens_hier_stable or None,
            KEY_SALT_AUTONOMY_DAYS:    self._autonomie_jours,
            KEY_SALT_AUTONOMY_WEEKS:   self._autonomie_semaines,
            KEY_AVG_DAILY_30D:         self._avg_daily_30d,
            KEY_LAST_SYNC:             dt_util.now(),
            KEY_FIRMWARE:              self._firmware,
        }