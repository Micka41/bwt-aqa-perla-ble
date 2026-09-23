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
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
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
    BLE_DISCONNECT_TIMEOUT,
    KEY_SALT_PCT,
    KEY_SALT_KG,
    KEY_SALT_TOTAL_KG,
    KEY_SALT_ALARM,
    KEY_CONSUMPTION_TODAY,
    KEY_CONSUMPTION_YESTERDAY,
    KEY_CONSUMPTION_WEEK,
    KEY_REGEN_TODAY,
    KEY_CUTOFF_TODAY,
    KEY_SALT_AUTONOMY_DAYS,
    KEY_SALT_AUTONOMY_WEEKS,
    KEY_SALT_AUTONOMY_DATE,
    KEY_AVG_DAILY_30D,
    KEY_LAST_SYNC,
    KEY_FIRMWARE,
    KEY_DEBUG_BROADCAST,
)

_LOGGER = logging.getLogger(__name__)

# Une notification transporte au plus 9 mots de 16 bits
_ENTREES_PAR_NOTIF = 9

# Lecture de l'historique complet par les services : environ 21 blocs, donc
# autant d'occasions de tomber sur une pause du proxy. Une lecture ratée est
# relancée dans une nouvelle session plutôt que remontée à l'utilisateur.
_TENTATIVES_HISTORIQUE = 3
_DELAI_ENTRE_TENTATIVES = 2.0   # secondes

_CYCLES_PAR_COMPLET = (INTERVALLE_COMPLET_H * 3600) // INTERVALLE_RAPIDE_S


# ── Helpers protocole ────────────────────────────────────────────────────────

def _get_word_le(buf: bytes, offset: int) -> int:
    return buf[offset] | (buf[offset + 1] << 8)


def _get_word_from(buf: bytes, index: int, first_min: bool) -> int:
    a = buf[index + 1] & 0xFF
    b = buf[index]     & 0xFF
    return (a * 256 + b) if first_min else (b * 256 + a)


def _dater_entrees(
    entrees: list[dict],
    idx_courant: int,
    taille_buffer: int,
    ancre,
    pas: timedelta,
) -> list[dict]:
    """Date des entrées d'après leur index absolu dans le buffer circulaire.

    L'entrée d'index `idx_courant - 1` est la plus récente et reçoit `ancre` ;
    les autres reculent d'un `pas` par cran d'écart. Passer par l'index plutôt
    que par la position dans la liste rend la datation insensible aux entrées
    manquantes — une seule absence décalait auparavant tout le calendrier.
    """
    return [
        {
            **e,
            "date": (
                ancre - pas * ((idx_courant - 1 - e["idx"]) % taille_buffer)
            ),
        }
        for e in entrees
    ]


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
    # Le pourcentage suit la logique de l'application d'origine : une valeur
    # délirante (> 50000 %) signale une trame incohérente et vaut 0, pas 100 —
    # afficher « plein » sur une donnée aberrante serait trompeur.
    if capa_total > 0:
        pct = (qte_sel * 100) // capa_total
        pct = 0 if pct > 50000 else max(0, min(100, pct))
    else:
        pct = 0
    
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
            entries.append({
                "litres":  word & 0x03FF,
                "rege":    bool(word & 0x0800),
                "coupure": bool(word & 0x0400),
            })
        else:
            entries.append({
                "litres":  (word & 0x07FF) * 10,
                "rege":    (word >> 12) & 0x03,
                "coupure": bool(word & 0x0800),
            })
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
        self._coupures_jour_stable: int = 0
        self._date_hier_stable:     str = ""
        self._firmware:             str = ""

        # Moyenne 30 jours glissants
        self._avg_daily_30d: float | None = None

        # Autonomie sel
        self._autonomie_jours:    int | None = None
        self._autonomie_semaines: int | None = None
        self._autonomie_date:     date | None = None  # figée tant qu'il n'y a pas de régénération
        self._regens_precedent:   int = 0              # pour détecter les régénérations
        
        # Une seule session BLE à la fois. Les cycles de rafraîchissement et
        # les appels de service partagent le même état de réception : deux
        # sessions concurrentes mélangeraient leurs notifications (issues #9
        # et #10) et produiraient des données fausses sans aucune erreur.
        self._ble_lock = asyncio.Lock()

        # Debug (diagnostic entity)
        self._debug_broadcast_history: list[str] = []  # dernières 10 trames BROADCAST

        # Stockage persistant : survit aux redémarrages de Home Assistant
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{address.replace(':', '').lower()}"
        )

    def _store_broadcast_debug(self, buf: bytes) -> None:
        """Stocker la trame BROADCAST pour l'entité diagnostic."""
        timestamp = dt_util.now().strftime("%Y-%m-%d %H:%M:%S")
        hex_dump = " ".join(f"{b:02x}" for b in buf)
        entry = f"{timestamp} [{len(buf)}B]: {hex_dump}"
        
        self._debug_broadcast_history.append(entry)
        # Garder seulement les 10 dernières
        if len(self._debug_broadcast_history) > 10:
            self._debug_broadcast_history.pop(0)

    # ── Persistance ───────────────────────────────────────────────────

    async def async_load_stored_data(self) -> None:
        """Restaure l'état persistant au démarrage de Home Assistant.

        La date de fin d'autonomie est figée entre deux régénérations : sans
        cette restauration, un redémarrage la recalculerait à partir de la date
        du jour et la ferait glisser.
        """
        stored = await self._store.async_load()
        if not stored:
            return

        if (iso := stored.get("autonomy_date")) is not None:
            try:
                self._autonomie_date = date.fromisoformat(iso)
            except ValueError:
                _LOGGER.warning("Stored autonomy date is invalid: %s", iso)

        self._autonomie_jours    = stored.get("autonomy_days")
        self._autonomie_semaines = stored.get("autonomy_weeks")
        self._regens_precedent   = stored.get("previous_regens", 0)

        _LOGGER.debug(
            "Restored state: autonomy_date=%s days=%s",
            self._autonomie_date, self._autonomie_jours,
        )

    def _persist_state(self) -> None:
        """Planifie l'écriture de l'état (différée et regroupée par HA)."""
        self._store.async_delay_save(
            lambda: {
                "autonomy_date": (
                    self._autonomie_date.isoformat() if self._autonomie_date else None
                ),
                "autonomy_days":   self._autonomie_jours,
                "autonomy_weeks":  self._autonomie_semaines,
                "previous_regens": self._regens_precedent,
            },
            delay=10,
        )

    # ── Session BLE ───────────────────────────────────────────────────

    def _resolve_ble_device(self):
        """Retrouve l'appareil dans la pile Bluetooth, ou échoue clairement."""
        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(
                f"BWT AQA Perla ({self.address}) not found — "
                "vérifiez portée BLE ou proxy ESPHome"
            )
        return ble_device

    @asynccontextmanager
    async def _ble_session(self, ble_device=None):
        """Ouvre une session avec l'adoucisseur et garantit sa fermeture.

        Prend en charge la séquence commune aux trois usages : connexion,
        abonnement aux notifications, authentification, lecture du BROADCAST.
        Cède `(client, bcast)` au bloc appelant, puis envoie la commande BREAK
        et se déconnecte — y compris si le bloc lève.

        `ble_device` peut être fourni lorsqu'il a déjà été résolu (cycles de
        rafraîchissement) ; sinon il est recherché ici (appels de service).
        """
        if self._ble_lock.locked():
            _LOGGER.debug("BLE session busy — waiting for the current one to finish")

        async with self._ble_lock:
            async with self._ble_session_unlocked(ble_device) as session:
                yield session

    @asynccontextmanager
    async def _ble_session_unlocked(self, ble_device=None):
        """Corps de la session BLE — n'appeler que sous `_ble_lock`."""
        if ble_device is None:
            ble_device = self._resolve_ble_device()

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

            yield client, bcast

            await client.write_gatt_char(UUID_WRITE, _build_break_cmd())
            await client.stop_notify(UUID_READ1)
        finally:
            # Une pile BlueZ dégradée peut faire attendre disconnect()
            # indéfiniment. Comme ce bloc s'exécute pendant la propagation
            # d'une exception, ce blocage empêcherait l'erreur d'origine
            # d'atteindre son gestionnaire : Home Assistant suspendrait son
            # démarrage puis classerait l'entrée en setup_error, qu'il ne
            # réessaie jamais. Voir issue #8.
            #
            # suppress(Exception) laisse passer CancelledError, qui dérive de
            # BaseException : une annulation véritable reste propagée.
            with suppress(Exception):
                async with asyncio.timeout(BLE_DISCONNECT_TIMEOUT):
                    await client.disconnect()

    # ── Hook principal ────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        # Échouer avant le reset minuit si l'appareil est hors de portée
        ble_device = self._resolve_ble_device()

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
        async with self._ble_session(ble_device) as (client, bcast):
            # Quarts nouveaux depuis _index_base
            idx = bcast["index_tab_quart"]
            nb  = (idx - self._index_base) % MAX_TAB_QUART
            quarts: list[dict] = []
            if nb > 0:
                quarts = await self._lire_blocs(
                    client, ADRESSE_TAB_QUART, self._index_base, nb, is_quart=True
                )

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
        async with self._ble_session(ble_device) as (client, bcast):
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

        # Dater les quarts d'après leur index absolu dans le buffer.
        # L'entrée idx_q - 1 est la plus récente : elle correspond au dernier
        # quart d'heure terminé. Les autres s'en déduisent en remontant le
        # buffer, ce qui reste juste même si des entrées manquent à la lecture.
        _now     = dt_util.now()
        _min_arr = (_now.minute // 15) * 15
        ancre_q  = _now.replace(minute=_min_arr, second=0, microsecond=0) - timedelta(minutes=15)
        quarts_dates = [
            {**q, "date": q["date"].strftime("%Y-%m-%d")}
            for q in _dater_entrees(
                quarts, idx_q, MAX_TAB_QUART, ancre_q, timedelta(minutes=15)
            )
        ]

        # Même principe pour les jours : idx_j - 1 correspond à hier.
        hier_d = dt_util.now().date() - timedelta(days=1)
        jours_dates = [
            {**j, "date": j["date"].isoformat()}
            for j in _dater_entrees(
                jours, idx_j, MAX_TAB_JOUR, hier_d, timedelta(days=1)
            )
        ]

        # Recalibrer conso jour depuis les quarts d'aujourd'hui
        aujourd_hui_str = dt_util.now().date().isoformat()
        quarts_auj = [q for q in quarts_dates if q["date"] == aujourd_hui_str]
        self._litres_jour_base  = sum(q["litres"] for q in quarts_auj)
        self._index_base        = bcast["index_tab_quart"]
        self._litres_jour_total = self._litres_jour_base
        self._date_dernier_complet = aujourd_hui_str

        # Régénérations et coupures du jour : on compte les transitions
        # False → True, car un même événement s'étale sur plusieurs quarts.
        regens, prev_rege = 0, False
        coupures, prev_coupure = 0, False
        for q in quarts_auj:
            if q["rege"] and not prev_rege:
                regens += 1
            prev_rege = q["rege"]
            if q.get("coupure") and not prev_coupure:
                coupures += 1
            prev_coupure = q.get("coupure", False)
        self._regens_jour_stable   = regens
        self._coupures_jour_stable = coupures

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

    def _reset_autonomie(self) -> None:
        """Rend l'autonomie indisponible : jours, semaines ET date de fin.

        Laisser la date en place afficherait une échéance obsolète alors que
        les autres capteurs d'autonomie sont indisponibles.
        """
        if self._autonomie_jours is None and self._autonomie_date is None:
            return
        self._autonomie_jours    = None
        self._autonomie_semaines = None
        self._autonomie_date     = None
        self._persist_state()

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
            self._reset_autonomie()
            return

        jours_tries = sorted(jours_dates, key=lambda e: e["date"])
        if len(jours_tries) < 2:
            self._reset_autonomie()
            return

        # Total des régénérations sur toute la période disponible
        total_regens = sum(j["rege"] for j in jours_tries)
        if total_regens == 0:
            self._reset_autonomie()
            return

        # Sel consommé par jour en moyenne
        nb_jours = len(jours_tries)
        sel_par_jour = (total_regens * vol_rege) / nb_jours

        jours = round(qte_sel / sel_par_jour)

        # La date de fin est figée entre deux régénérations : la recalculer à
        # chaque cycle la ferait glisser d'un jour par jour.
        #
        # Le compteur journalier repart de zéro à minuit : une simple
        # comparaison « compteur > précédent » manquerait une régénération
        # survenue juste après le reset. On considère donc qu'il y a
        # régénération dès que le compteur change ET qu'il est non nul.
        regens = self._regens_jour_stable
        nouvelle_regen = regens > 0 and regens != self._regens_precedent

        if self._autonomie_date is None or nouvelle_regen:
            self._autonomie_date = dt_util.now().date() + timedelta(days=jours)

        self._regens_precedent   = regens
        self._autonomie_jours    = jours
        self._autonomie_semaines = jours // 7
        self._persist_state()
        _LOGGER.info(
            "Salt autonomy: %d days (%d weeks) "
            "[sel=%dg  regens=%d/%dj  sel/j=%.1fg]",
            self._autonomie_jours, self._autonomie_semaines,
            qte_sel, total_regens, nb_jours, sel_par_jour,
        )

    # ── Stabilisation hier / semaine ─────────────────────────────────

    def _mettre_a_jour_hier_semaine(self, jours_dict: dict[str, dict]) -> None:
        """Protège contre la non-consolidation du BWT (J-1 consolidé vers 04h00)."""
        hier_iso    = (dt_util.now().date() - timedelta(days=1)).isoformat()
        entree_hier = jours_dict.get(hier_iso)
        val_hier    = entree_hier["litres"] if entree_hier else 0

        # Le BWT consolide J-1 vers 04h00. Après cette heure, une valeur nulle
        # est légitime — le buffer journalier stocke par tranches de 10 L, donc
        # une consommation inférieure y est enregistrée comme 0 — et ne doit pas
        # être confondue avec « pas encore consolidé ».
        consolide = dt_util.now().hour >= 4

        if val_hier > 0 or (consolide and entree_hier is not None):
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
        """Lit nb entrées en envoyant des commandes READ_BUFFER par blocs de 90.

        Chaque entrée retournée porte une clé `idx` : son index absolu dans le
        buffer circulaire, sur lequel repose la datation.
        """
        taille_buffer = MAX_TAB_QUART if is_quart else MAX_TAB_JOUR
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

            trames = self._trames_du_bloc(nb_tr)
            ecartees = len(self._notifications) - len(trames)

            if len(trames) < nb_tr:
                # Un bloc incomplet ne peut pas être daté : ses entrées seraient
                # décalées. Mieux vaut échouer et réessayer au cycle suivant
                # que produire des valeurs fausses.
                raise UpdateFailed(
                    f"Incomplete block @ {adresse:#x}: {len(trames)}/{nb_tr} frames "
                    f"({ecartees} out-of-sequence discarded, "
                    f"{len(resultats)} entries read so far)"
                )
            if ecartees:
                _LOGGER.debug(
                    "Block @ %#x: %d out-of-sequence frame(s) discarded "
                    "(late frame from the previous block, or duplicate)",
                    adresse, ecartees,
                )

            # Chaque entrée porte son index absolu dans le buffer circulaire :
            # la n-ième trame du bloc contient les entrées index_os + n*9 et
            # suivantes. La datation s'appuie sur cet index, et non sur la
            # position dans la liste finale.
            attendues = 0
            for n, notif in enumerate(trames):
                _, entries = _decode_notification(notif, is_quart)
                base = index_os + n * _ENTREES_PAR_NOTIF
                resultats.extend(
                    {**e, "idx": (base + k) % taille_buffer}
                    for k, e in enumerate(entries)
                )
                attendues += len(entries)

            if attendues < bloc:
                # Le bloc est complet : des entrées manquantes ne peuvent venir
                # que de cases non encore écrites (sentinelle 0xFFFF).
                _LOGGER.debug(
                    "Block @ %#x: %d/%d entries decoded (unwritten buffer slots)",
                    adresse, attendues, bloc,
                )

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

    def _trames_du_bloc(self, nb_trames: int) -> list[bytearray]:
        """Trames du bloc courant, dans l'ordre : compteurs 0, 1, 2…

        Les deux premiers octets de chaque trame portent son rang dans le bloc,
        remis à zéro à chaque commande READ — c'est la règle qu'applique
        l'application BWT (`Index == LastGoodIndex + 1`).

        Une trame qui ne prolonge pas cette séquence est écartée. C'est le cas
        d'une trame du bloc précédent arrivée en retard : via un proxy ESPHome,
        elle peut atteindre Home Assistant après l'envoi de la commande
        suivante (issues #9 et #10).
        """
        valides: list[bytearray] = []
        for notif in self._notifications:
            if len(notif) >= 2 and _get_word_from(notif, 0, True) == len(valides):
                valides.append(notif)
                if len(valides) == nb_trames:
                    break
        return valides

    async def _attendre_notifications(
        self, nb_attendues: int, timeout: float = BLE_NOTIFY_TIMEOUT
    ) -> None:
        """Attend les nb_attendues trames du bloc courant.

        On ne passe pas au bloc suivant tant que celui-ci est incomplet : sa
        dernière trame arriverait sinon pendant la lecture suivante, en
        déclenchant une cascade qui décale chaque bloc d'une trame.

        L'attente s'interrompt si aucune trame utile n'arrive pendant
        BLE_NOTIFY_SILENCE secondes, ou au bout du délai total.
        """
        loop     = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while True:
            self._notify_event.clear()
            recues = len(self._trames_du_bloc(nb_attendues))
            if recues >= nb_attendues:
                return

            restant = deadline - loop.time()
            if restant <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._notify_event.wait(),
                    timeout=min(BLE_NOTIFY_SILENCE, restant),
                )
            except asyncio.TimeoutError:
                # Plus rien depuis BLE_NOTIFY_SILENCE : trame perdue
                if len(self._trames_du_bloc(nb_attendues)) == recues:
                    return

    # ── Services HA ───────────────────────────────────────────────────

    async def _read_full_history(self) -> list[dict]:
        """Lit tout l'historique journalier, en relançant une lecture ratée.

        Chaque tentative ouvre sa propre session BLE : l'appareil repart d'un
        état propre, sans hypothèse sur la façon dont il réagirait à une
        commande relancée en cours d'émission. Le code BWT d'origine, lui,
        ferme la connexion en cas d'erreur de trame.
        """
        for tentative in range(1, _TENTATIVES_HISTORIQUE + 1):
            try:
                return await self._read_full_history_once()
            except (UpdateFailed, BleakError) as err:
                if tentative == _TENTATIVES_HISTORIQUE:
                    raise
                _LOGGER.warning(
                    "History read failed (attempt %d/%d): %s — retrying",
                    tentative, _TENTATIVES_HISTORIQUE, err,
                )
                await asyncio.sleep(_DELAI_ENTRE_TENTATIVES)
        raise AssertionError("unreachable")

    async def _read_full_history_once(self) -> list[dict]:
        """Une tentative de lecture de l'historique journalier (jusqu'à 1825 jours)."""
        async with self._ble_session() as (client, bcast):
            idx_j     = bcast["index_tab_jour"]
            loop_jour = bcast["loop_jour"]

            if loop_jour:
                # Buffer plein (>5 ans) : lire les 1825 jours en 2 parties
                # idx_j pointe sur le plus ancien → partie 1 : idx_j..fin, partie 2 : 0..idx_j-1
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

        # Dater d'après l'index absolu : idx_j - 1 correspond à hier.
        hier_d = dt_util.now().date() - timedelta(days=1)
        return [
            {**j, "date": j["date"].isoformat()}
            for j in _dater_entrees(
                jours, idx_j, MAX_TAB_JOUR, hier_d, timedelta(days=1)
            )
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
        # L'ordre de lecture est déjà chronologique, wrap du buffer compris.
        # Le tri le garantit explicitement plutôt que de le faire dépendre de
        # l'ordre des blocs demandés.
        for j in sorted(jours, key=lambda e: e["date"]):
            annee, mois, jour = j["date"].split("-")
            result.setdefault(annee, {}).setdefault(mois, {})[jour] = j["litres"]
        return result

    async def service_history_regenerations(self) -> dict:
        """Service get_history_regenerations — régénérations par année/mois/jour."""
        jours = await self._read_full_history()
        result: dict = {}
        # L'ordre de lecture est déjà chronologique, wrap du buffer compris.
        # Le tri le garantit explicitement plutôt que de le faire dépendre de
        # l'ordre des blocs demandés.
        for j in sorted(jours, key=lambda e: e["date"]):
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
            KEY_CUTOFF_TODAY:          self._coupures_jour_stable,
            KEY_SALT_AUTONOMY_DAYS:    self._autonomie_jours,
            KEY_SALT_AUTONOMY_WEEKS:   self._autonomie_semaines,
            KEY_SALT_AUTONOMY_DATE:    self._autonomie_date,
            KEY_AVG_DAILY_30D:         self._avg_daily_30d,
            KEY_LAST_SYNC:             dt_util.now(),
            KEY_FIRMWARE:              self._firmware,
            # L'état d'une entité HA est limité à 255 caractères : il ne porte
            # que la dernière trame, l'historique passe par les attributs.
            KEY_DEBUG_BROADCAST:       self._debug_broadcast_history[-1] if self._debug_broadcast_history else "No data",
            "debug_broadcast_frames":  list(self._debug_broadcast_history),
        }