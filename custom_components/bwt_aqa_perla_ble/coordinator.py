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
from datetime import date, timedelta
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
    MAX_TAB_JOUR,
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
    KEY_SALT_AUTONOMY_DAYS,
    KEY_SALT_AUTONOMY_WEEKS,
    KEY_SALT_AUTONOMY_DATE,
    KEY_AVG_DAILY_30D,
    KEY_LAST_SYNC,
    KEY_FIRMWARE,
    KEY_DEBUG_BROADCAST,
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
            raise UpdateFailed(f"BROADCAST too short: {len(buf)} bytes")
    
    # Debug: log raw BROADCAST for firmware debugging
    hex_dump = " ".join(f"{b:02x}" for b in buf)
    _LOGGER.debug(
        "BROADCAST raw [%d bytes]: %s",
        len(buf), hex_dump
    )
    
    qte_sel    = _get_word_le(buf, 0) + _get_word_le(buf, 2) * 65536
    
    # Firmware V2.x reports salt quantity values 4× higher than expected
    # Dividing by 4 yields correct values (empirically verified)
    # Issue #4: https://github.com/Micka41/bwt-aqa-perla-ble/issues/4
    if buf[13] >= 2:  # V2.x and newer
        qte_sel //= 4
    
    capa_total = _get_word_le(buf, 10) * 1000
    flags      = buf[12]
    pct        = max(0, min(100, (qte_sel * 100) // capa_total)) if capa_total > 0 else 0
    
    _LOGGER.debug(
        "BROADCAST decoded: qte_sel=%d g (%.2f kg), capa_total=%d g (%.2f kg), "
        "pct=%d%%, flags=0x%02x, version=A22X V%d.%d",
        qte_sel, qte_sel / 1000, capa_total, capa_total / 1000,
        pct, flags, buf[13], buf[14]
    )
    return {
        "qte_sel_restant":  qte_sel,
        "index_tab_quart":  _get_word_le(buf, 4),
        "index_tab_jour":   _get_word_le(buf, 6),
        "vol_sel_rege":     _get_word_le(buf, 8),
        "capa_total_sel":   capa_total,
        "alarme":           bool(flags & 0x01),
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
        self._autonomie_jours:    int | None = None
        self._autonomie_semaines: int | None = None
        self._autonomie_date:     date | None = None  # figée tant qu'il n'y a pas de régénération
        self._regens_precedent:   int = 0              # pour détecter les régénérations
        
        # Debug (diagnostic entity)
        self._debug_broadcast_history: list[str] = []  # dernières 10 trames BROADCAST

    def _store_broadcast_debug(self, buf: bytes) -> None:
        """Stocker la trame BROADCAST pour l'entité diagnostic."""
        timestamp = dt_util.now().strftime("%Y-%m-%d %H:%M:%S")
        hex_dump = " ".join(f"{b:02x}" for b in buf)
        entry = f"{timestamp} [{len(buf)}B]: {hex_dump}"
        
        self._debug_broadcast_history.append(entry)
        # Garder seulement les 10 dernières
        if len(self._debug_broadcast_history) > 10:
            self._debug_broadcast_history.pop(0)

    # ── Hook principal ────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(
                f"BWT AQA Perla ({self.address}) not found — "
                "vérifiez portée BLE ou proxy ESPHome"
            )

        now            = dt_util.now()
        aujourd_hui    = now.date().isoformat()
        now_hm         = now.hour * 60 + now.minute
        changement_jour = aujourd_hui != self._date_dernier_complet

        # Reset minuit — une seule fois par jour
        if (changement_jour
                and self._date_dernier_complet != ""
                and self._date_remise_a_zero != aujourd_hui
                and self._litres_jour_total > 0):
            _LOGGER.info("Midnight — resetting daily consumption")
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
                    _LOGGER.info("New day after 04:00 — forcing full cycle")
                    self._date_dernier_complet = aujourd_hui
                result = await self._run_complet(ble_device)
            else:
                result = await self._run_rapide(ble_device)
        except BleakError as err:
            raise UpdateFailed(f"BLE error: {err}") from err

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

            buf = await client.read_gatt_char(UUID_BROADCAST)
            self._store_broadcast_debug(buf)
            bcast = _decode_broadcast(buf)
            self._dernier_index_tab_quart = bcast["index_tab_quart"]
            if bcast["version"]:
                self._firmware = bcast["version"]

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
            "Fast cycle — base=%d + delta=%d = %d L",
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

            buf = await client.read_gatt_char(UUID_BROADCAST)
            self._store_broadcast_debug(buf)
            bcast = _decode_broadcast(buf)
            self._dernier_index_tab_quart = bcast["index_tab_quart"]
            if bcast["version"]:
                self._firmware = bcast["version"]

            # Quarts — gestion du buffer circulaire (wrap tous les 30 jours)
            idx_q = bcast["index_tab_quart"]
            nb_q  = min(NB_QUARTS_COMPLET, MAX_TAB_QUART)
            quarts: list[dict] = []
            if idx_q >= nb_q:
                # Cas normal : pas de wrap dans la fenêtre
                quarts = await self._lire_blocs(
                    client, ADRESSE_TAB_QUART, idx_q - nb_q, nb_q, is_quart=True
                )
            else:
                # Wrap (inclut idx_q == 0) : lire en deux parties
                nb_partie1 = nb_q - idx_q
                debut1 = MAX_TAB_QUART - nb_partie1
                quarts = await self._lire_blocs(
                    client, ADRESSE_TAB_QUART, debut1, nb_partie1, is_quart=True
                )
                if idx_q > 0:
                    quarts += await self._lire_blocs(
                        client, ADRESSE_TAB_QUART, 0, idx_q, is_quart=True
                    )

            # Jours — gestion du buffer circulaire (wrap après 5 ans)
            idx_j = bcast["index_tab_jour"]
            nb_j  = min(NB_JOURS_COMPLET, MAX_TAB_JOUR)
            jours: list[dict] = []
            if idx_j >= nb_j:
                # Cas normal
                jours = await self._lire_blocs(
                    client, ADRESSE_TAB_JOUR, idx_j - nb_j, nb_j, is_quart=False
                )
            else:
                # Wrap (inclut idx_j == 0)
                nb_partie1 = nb_j - idx_j
                debut1 = MAX_TAB_JOUR - nb_partie1
                jours = await self._lire_blocs(
                    client, ADRESSE_TAB_JOUR, debut1, nb_partie1, is_quart=False
                )
                if idx_j > 0:
                    jours += await self._lire_blocs(
                        client, ADRESSE_TAB_JOUR, 0, idx_j, is_quart=False
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
            {**q, "date": (ancre_q - timedelta(minutes=15 * (len(quarts) - 1 - i))).strftime("%Y-%m-%d")}
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
        _LOGGER.debug("30-day average: %s L/d (%d days)", self._avg_daily_30d, len(jours_30))

        # Autonomie sel : sel_restant / (regens_moy_jour × sel_par_regen)
        # Moyenne sur les jours disponibles avec au moins 1 régénération
        self._calculer_autonomie(bcast, jours_dates)

        _LOGGER.info(
            "Full cycle — base=%d L  index=%d  regens=%d  yesterday=%d L  week=%d L",
            self._litres_jour_base, self._index_base,
            self._regens_jour_stable, self._conso_hier_stable, self._conso_semaine_stable,
        )
        return self._build_result(bcast)

    # ── Calcul de l'autonomie sel ─────────────────────────────────────

    def _calculer_autonomie(self, bcast: dict, jours_dates: list[dict]) -> None:
        """
        Calcul de l'autonomie sel basé sur la consommation moyenne de sel par jour.

        Formule :
          sel_consomme_par_jour = (nb_regens_sur_periode × vol_sel_rege) / nb_jours_periode
          autonomie_jours       = qte_sel_restant / sel_consomme_par_jour

        Utilise uniquement les jours avec au moins une régénération pour la moyenne.
        """
        vol_rege = bcast.get("vol_sel_rege", 0)
        qte_sel  = bcast.get("qte_sel_restant", 0)

        if vol_rege <= 0 or qte_sel <= 0:
            _LOGGER.debug("Salt autonomy not calculable (vol_rege=%d qte_sel=%d)", vol_rege, qte_sel)
            self._autonomie_jours    = None
            self._autonomie_semaines = None
            return

        jours_tries = sorted(jours_dates, key=lambda e: e["date"])
        if len(jours_tries) < 2:
            self._autonomie_jours    = None
            self._autonomie_semaines = None
            return

        # Total des régénérations sur toute la période disponible
        total_regens = sum(j["rege"] for j in jours_tries)
        if total_regens == 0:
            self._autonomie_jours    = None
            self._autonomie_semaines = None
            return

        # Sel consommé par jour en moyenne
        nb_jours = len(jours_tries)
        sel_par_jour = (total_regens * vol_rege) / nb_jours

        jours = round(qte_sel / sel_par_jour)
        
        # Recalculer la date uniquement lors d'une régénération
        # Détection : _regens_jour_stable augmente (0→1 signale une nouvelle régénération)
        if self._autonomie_date is None or self._regens_jour_stable > self._regens_precedent:
            # Première initialisation ou régénération détectée
            self._autonomie_date = dt_util.now().date() + timedelta(days=jours)
        
        # Toujours mettre à jour après le calcul (pour détecter le prochain changement)
        self._regens_precedent = self._regens_jour_stable
        
        # Toujours mettre à jour les valeurs en jours/semaines
        self._autonomie_jours    = jours
        self._autonomie_semaines = jours // 7
        _LOGGER.info(
            "Salt autonomy: %d days (%d weeks) "
            "[sel=%dg  regens=%d/%dj  sel/j=%.1fg]",
            self._autonomie_jours, self._autonomie_semaines,
            qte_sel, total_regens, nb_jours, sel_par_jour,
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
            _LOGGER.info("Yesterday consumption consolidated: %d L", self._conso_hier_stable)
        elif self._date_hier_stable != hier_iso and self._conso_hier_stable == 0:
            # Pas encore consolidé → chercher dernière valeur non-nulle
            for i in range(1, 8):
                d = (dt_util.now().date() - timedelta(days=i)).isoformat()
                e = jours_dict.get(d)
                if e and e["litres"] > 0:
                    self._conso_hier_stable = e["litres"]
                    _LOGGER.info(
                        "Yesterday provisional consumption from %s: %d L", d, self._conso_hier_stable
                    )
                    break

        # Semaine : 7 jours J-1..J-7 (mis à jour uniquement si J-1 consolidé)
        if entree_hier is not None:
            self._conso_semaine_stable = sum(
                jours_dict[d]["litres"]
                for i in range(1, 8)
                if (d := (dt_util.now().date() - timedelta(days=i)).isoformat()) in jours_dict
            )
            _LOGGER.info("Weekly consumption: %d L", self._conso_semaine_stable)
        else:
            _LOGGER.info(
                "Weekly consumption: yesterday not yet consolidated — keeping stable value (%d L)",
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
                _LOGGER.warning("No notification received @ %#x (%d entries)", adresse, bloc)
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

    # ── Services HA ───────────────────────────────────────────────────

    async def _read_full_history(self) -> list[dict]:
        """Lit tout l'historique journalier disponible (jusqu'à 1825 jours)."""
        from .const import MAX_TAB_JOUR as _MAX_TAB_JOUR

        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise ValueError(f"BWT AQA Perla ({self.address}) not found")

        from bleak_retry_connector import establish_connection as _establish
        client = await _establish(
            BleakClient, ble_device, self.address,
            max_attempts=3, ctor_kwargs={"timeout": BLE_CONNECT_TIMEOUT},
        )
        try:
            await self._start_notify(client)
            await client.read_gatt_char(UUID_OTHER)
            buf = await client.read_gatt_char(UUID_BROADCAST)
            self._store_broadcast_debug(buf)
            bcast = _decode_broadcast(buf)

            idx_j     = bcast["index_tab_jour"]
            loop_jour = bcast["loop_jour"]

            if loop_jour:
                # Buffer plein (>5 ans) : lire les 1825 jours en 2 parties
                # idx_j pointe sur le plus ancien → partie 1 : idx_j..fin, partie 2 : 0..idx_j-1
                nb_j = MAX_TAB_JOUR
                jours = await self._lire_blocs(
                    client, ADRESSE_TAB_JOUR, idx_j, MAX_TAB_JOUR - idx_j, is_quart=False
                )
                if idx_j > 0:
                    jours += await self._lire_blocs(
                        client, ADRESSE_TAB_JOUR, 0, idx_j, is_quart=False
                    )
            elif idx_j > 0:
                # Buffer non plein : lire idx_j entrées depuis le début
                jours = await self._lire_blocs(
                    client, ADRESSE_TAB_JOUR, 0, idx_j, is_quart=False
                )
            else:
                jours = []

            await client.write_gatt_char(UUID_WRITE, _build_break_cmd())
            await client.stop_notify(UUID_READ1)
        finally:
            await client.disconnect()

        # Assigner les dates (ancre = hier)
        from homeassistant.util import dt as _dt
        hier_d = _dt.now().date() - timedelta(days=1)
        return [
            {**j, "date": (hier_d - timedelta(days=(len(jours) - 1 - i))).isoformat()}
            for i, j in enumerate(jours)
        ]

    async def service_total_consumption(self) -> dict:
        """Service get_total_consumption — total en litres depuis l'historique complet."""
        jours = await self._read_full_history()
        total = sum(j["litres"] for j in jours)
        _LOGGER.info("Total history: %d L over %d days", total, len(jours))
        return {
            "total_liters":  total,
            "days_count":    len(jours),
            "from_date":     jours[0]["date"] if jours else None,
            "to_date":       jours[-1]["date"] if jours else None,
        }

    async def service_history_consumption(self) -> dict:
        """Service get_history_consumption — consommation par année/mois/jour."""
        jours = await self._read_full_history()
        result: dict = {}
        for j in jours:
            annee, mois, jour = j["date"].split("-")
            result.setdefault(annee, {}).setdefault(mois, {})[jour] = j["litres"]
        return result

    async def service_history_regenerations(self) -> dict:
        """Service get_history_regenerations — régénérations par année/mois/jour."""
        jours = await self._read_full_history()
        result: dict = {}
        for j in jours:
            annee, mois, jour = j["date"].split("-")
            result.setdefault(annee, {}).setdefault(mois, {})[jour] = j["rege"]
        return result

    # ── Construction du résultat HA ───────────────────────────────────

    def _build_result(self, bcast: dict) -> dict[str, Any]:
        return {
            KEY_SALT_PCT:              bcast["pourcentage_sel"],
            KEY_SALT_KG:               round(bcast["qte_sel_restant"] / 1000, 2),
            KEY_SALT_TOTAL_KG:         round(bcast["capa_total_sel"]  / 1000, 2),
            KEY_SALT_ALARM:            bcast["alarme"],
            KEY_CONSUMPTION_TODAY:     self._litres_jour_total,
            KEY_CONSUMPTION_YESTERDAY: self._conso_hier_stable if self._date_hier_stable != "" else None,
            KEY_CONSUMPTION_WEEK:      self._conso_semaine_stable if self._date_hier_stable != "" else None,
            KEY_REGEN_TODAY:           self._regens_jour_stable,
            KEY_SALT_AUTONOMY_DAYS:    self._autonomie_jours,
            KEY_SALT_AUTONOMY_WEEKS:   self._autonomie_semaines,
            KEY_SALT_AUTONOMY_DATE:    self._autonomie_date,
            KEY_AVG_DAILY_30D:         self._avg_daily_30d,
            KEY_LAST_SYNC:             dt_util.now(),
            KEY_FIRMWARE:              self._firmware,
            KEY_DEBUG_BROADCAST:       "\n".join(self._debug_broadcast_history) if self._debug_broadcast_history else "No data",
        }