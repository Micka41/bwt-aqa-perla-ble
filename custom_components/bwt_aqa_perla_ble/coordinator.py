"""DataUpdateCoordinator for BWT AQA Perla.

L'adoucisseur tient deux historiques : des quarts d'heure (30 jours, au litre
près) et des cases journalières (5 ans, en dizaines de litres). Les cases
journalières ne sont pas découpées à minuit : chaque appareil change de case à
une heure qui lui est propre, apprise en l'observant (voir datation.py).

  Cycle RAPIDE (toutes les 15 min) :
    BROADCAST + quarts depuis _index_base → ~5 s BLE
    litres_jour = _litres_jour_base + delta

  Cycle COMPLET (toutes les heures) :
    BROADCAST + 120 derniers quarts + 365 cases journalières → ~20 s BLE
    recalibre la consommation du jour, la moyenne 30 jours et l'autonomie.
    Le premier de chaque journée, dès 00 h 20, remonte les quarts jusqu'à
    J-7 pour calculer hier et 7 jours, découpés à minuit.

  Reset minuit :
    _litres_jour_base = 0, _index_base = _dernier_index_tab_quart

  À chaque lecture du BROADCAST, l'index journalier est comparé au précédent
  pour apprendre l'heure de bascule de l'appareil.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .datation import (
    ApprentissageBascule,
    dater_cases,
    historique_par_jour,
    total_sur_jours,
)
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
    KEY_WATER_METER,
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
    KEY_DAY_ROLLOVER,
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

def cle_stockage(address: str) -> str:
    """Clé du fichier .storage propre à un adoucisseur."""
    return f"{STORAGE_KEY}.{address.replace(':', '').lower()}"


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

        # Valeurs mémorisées entre deux cycles complets
        self._conso_hier_stable:    int | None = None
        self._conso_semaine_stable: int | None = None
        self._regens_jour_stable:   int = 0
        self._coupures_jour_stable: int = 0
        # Hier et 7 jours : calculés depuis les quarts, une fois par jour
        self._jour_hier_semaine:    str = ""   # date du dernier calcul

        # Heure à laquelle l'adoucisseur ouvre une nouvelle case journalière
        self._bascule = ApprentissageBascule()

        # Compteur d'eau cumulé : ne revient jamais à zéro. Il additionne chaque
        # quart d'heure exactement une fois — y compris celui de 23 h 45, écrit
        # à minuit, que la consommation du jour ne voit jamais.
        self._compteur_litres: int = 0
        self._compteur_idx: int | None = None       # prochain quart à compter
        self._compteur_instant: datetime | None = None
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
        self._store: Store = Store(hass, STORAGE_VERSION, cle_stockage(address))

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
        self._bascule = ApprentissageBascule.depuis_dict(stored.get("day_rollover"))

        compteur = stored.get("water_meter") or {}
        try:
            self._compteur_litres = int(compteur["liters"])
            self._compteur_idx = int(compteur["next_index"])
            self._compteur_instant = datetime.fromisoformat(compteur["at"])
        except (KeyError, TypeError, ValueError):
            self._compteur_litres, self._compteur_idx, self._compteur_instant = 0, None, None

        _LOGGER.debug(
            "Restored state: autonomy_date=%s days=%s day_rollover=%s",
            self._autonomie_date, self._autonomie_jours, self._heure_bascule_texte(),
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
                "day_rollover":    self._bascule.vers_dict(),
                "water_meter": (
                    {
                        "liters":     self._compteur_litres,
                        "next_index": self._compteur_idx,
                        "at":         self._compteur_instant.isoformat(),
                    }
                    if self._compteur_idx is not None else None
                ),
            },
            delay=10,
        )

    # ── Heure de bascule journalière ──────────────────────────────────

    def _observer_bascule(self, idx_jour: int) -> None:
        """Note l'index journalier lu ; date la bascule s'il vient d'avancer.

        Chaque adoucisseur ouvre sa case journalière suivante à une heure qui
        lui est propre (vers 4 h chez l'un, 9 h 30 chez l'autre, issue #10).
        Elle n'est documentée nulle part : on l'apprend en observant l'index.
        """
        if self._bascule.observer(idx_jour, dt_util.now(), MAX_TAB_JOUR):
            _LOGGER.info(
                "Day rollover observed (index %d) — learned rollover time %s "
                "from %d observation(s)",
                idx_jour, self._heure_bascule_texte(), len(self._bascule.observations),
            )
            self._persist_state()

    def _heure_bascule_texte(self) -> str | None:
        heure = self._bascule.heure
        return None if heure is None else f"{heure // 60:02d}:{heure % 60:02d}"

    def _dater_jours(self, jours: list[dict], idx_jour: int) -> list[dict]:
        """Date les cases journalières d'après la bascule apprise.

        Tant qu'aucune bascule n'a été observée, on suppose qu'elle a lieu à
        minuit : la dernière case close est la veille, comme dans les versions
        précédentes.
        """
        maintenant = dt_util.now()
        bascule = self._bascule.derniere_bascule(idx_jour, maintenant, MAX_TAB_JOUR)
        if bascule is None:
            bascule = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
        return dater_cases(jours, idx_jour, MAX_TAB_JOUR, bascule)

    # ── Compteur d'eau cumulé ─────────────────────────────────────────

    async def _mettre_a_jour_compteur(
        self, client, bcast: dict, quarts_lus: list[dict]
    ) -> None:
        """Ajoute au compteur les quarts écrits depuis le dernier comptage.

        Appelée dans la session BLE de chaque cycle : les quarts non encore
        comptés qui ne figurent pas dans la lecture du cycle — après un
        redémarrage de Home Assistant, par exemple — sont lus en complément.
        """
        idx_q = bcast["index_tab_quart"]
        maintenant = dt_util.now()

        if self._compteur_idx is None:
            # Première mise en service : le compteur part de zéro, maintenant
            self._compteur_idx, self._compteur_instant = idx_q, maintenant
            self._persist_state()
            return

        a_compter = (idx_q - self._compteur_idx) % MAX_TAB_QUART
        if a_compter == 0:
            return

        # L'index ne peut pas avoir avancé de plus d'un quart par quart d'heure
        # écoulé. Au-delà, il a été réinitialisé côté appareil : on se recale
        # sans rien compter, plutôt que de recompter un mois de données.
        attendus = int((maintenant - self._compteur_instant) / timedelta(minutes=15)) + 2
        if a_compter > attendus:
            _LOGGER.warning(
                "Water meter: quarter-hour index jumped by %d (at most %d expected) "
                "— resynchronising without counting",
                a_compter, attendus,
            )
            self._compteur_idx, self._compteur_instant = idx_q, maintenant
            self._persist_state()
            return

        disponibles = MAX_TAB_QUART if bcast["loop_quart"] else idx_q
        if a_compter > disponibles:
            _LOGGER.warning(
                "Water meter: %d quarter-hours were overwritten before they could be "
                "counted (Home Assistant stopped for more than 30 days?)",
                a_compter - disponibles,
            )
            self._compteur_idx = (idx_q - disponibles) % MAX_TAB_QUART
            a_compter = disponibles

        litres = {q["idx"]: q["litres"] for q in quarts_lus}
        absents = 0
        while absents < a_compter and (self._compteur_idx + absents) % MAX_TAB_QUART not in litres:
            absents += 1
        if absents:
            for q in await self._lire_plage(
                client, ADRESSE_TAB_QUART, self._compteur_idx, absents,
                MAX_TAB_QUART, is_quart=True,
            ):
                litres[q["idx"]] = q["litres"]

        ajout = sum(
            litres.get((self._compteur_idx + k) % MAX_TAB_QUART, 0)
            for k in range(a_compter)
        )
        self._compteur_litres += ajout
        self._compteur_idx, self._compteur_instant = idx_q, maintenant
        self._persist_state()
        _LOGGER.debug(
            "Water meter: +%d L over %d quarter-hour(s) → %d L",
            ajout, a_compter, self._compteur_litres,
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
            self._observer_bascule(bcast["index_tab_jour"])
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

        # Sélection du type de cycle. Hier et 7 jours se calculent sur les
        # quarts d'heure : dès 00 h 20, la veille est entièrement écrite.
        hier_a_calculer = self._jour_hier_semaine != aujourd_hui and now_hm >= 20
        faire_complet = (
            self._cycles_rapides % _CYCLES_PAR_COMPLET == 0
            or hier_a_calculer
        )

        try:
            if faire_complet:
                if hier_a_calculer and self._cycles_rapides > 0:
                    _LOGGER.info("New day — forcing full cycle for yesterday and last 7 days")
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
                quarts = await self._lire_plage(
                    client, ADRESSE_TAB_QUART, self._index_base, nb,
                    MAX_TAB_QUART, is_quart=True,
                )
            await self._mettre_a_jour_compteur(client, bcast, quarts)

        # Ne compter que les quarts d'aujourd'hui. Juste après minuit, les
        # nouveaux quarts incluent celui de 23 h 45 – 00 h 00, écrit à minuit
        # pile : l'additionner sans le dater le ferait passer de la veille à la
        # journée en cours, puis le cycle complet suivant le retirerait — une
        # baisse qu'un capteur total_increasing prend pour une remise à zéro.
        aujourd_hui = dt_util.now().date().isoformat()
        delta = sum(
            q["litres"] for q in self._dater_quarts(quarts, idx)
            if q["date"] == aujourd_hui
        )
        self._litres_jour_total = self._litres_jour_base + delta
        _LOGGER.debug(
            "Fast cycle — base=%d + delta=%d = %d L",
            self._litres_jour_base, delta, self._litres_jour_total,
        )
        return self._build_result(bcast)

    # ── Cycle complet ─────────────────────────────────────────────────

    async def _run_complet(self, ble_device) -> dict[str, Any]:
        """BROADCAST + quarts + 365 jours → recalibrage complet.

        Une fois par jour, la lecture des quarts remonte à J-7 pour recalculer
        la consommation d'hier et des 7 derniers jours ; le reste du temps,
        les 120 derniers quarts suffisent à la journée en cours.
        """
        maintenant  = dt_util.now()
        aujourd_hui = maintenant.date()
        recalcul_hier = self._jour_hier_semaine != aujourd_hui.isoformat()

        if recalcul_hier:
            debut_semaine = maintenant.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=7)
            nb_q = int((maintenant - debut_semaine) / timedelta(minutes=15)) + 1
        else:
            nb_q = NB_QUARTS_COMPLET

        async with self._ble_session(ble_device) as (client, bcast):
            idx_q = bcast["index_tab_quart"]
            idx_j = bcast["index_tab_jour"]
            quarts = await self._lire_derniers(
                client, ADRESSE_TAB_QUART, idx_q, nb_q,
                MAX_TAB_QUART, bcast["loop_quart"], is_quart=True,
            )
            jours = await self._lire_derniers(
                client, ADRESSE_TAB_JOUR, idx_j, NB_JOURS_COMPLET,
                MAX_TAB_JOUR, bcast["loop_jour"], is_quart=False,
            )
            await self._mettre_a_jour_compteur(client, bcast, quarts)

        quarts_dates = self._dater_quarts(quarts, idx_q)
        jours_dates  = self._dater_jours(jours, idx_j)

        # Recalibrer conso jour depuis les quarts d'aujourd'hui
        aujourd_hui_str = aujourd_hui.isoformat()
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

        if recalcul_hier:
            self._calculer_hier_semaine(quarts_dates, maintenant)

        # Moyenne sur les 30 dernières cases journalières. Chaque case couvre
        # une journée de l'adoucisseur plutôt qu'une journée calendaire, mais
        # sur 30 jours ce décalage ne déplace de l'eau qu'aux deux extrémités.
        jours_30 = [
            j["litres"] for j in sorted(jours_dates, key=lambda e: e["fin"])
        ][-30:]
        self._avg_daily_30d = round(sum(jours_30) / len(jours_30), 1) if jours_30 else None
        _LOGGER.debug("30-day average: %s L/d (%d days)", self._avg_daily_30d, len(jours_30))

        # Autonomie sel : sel_restant / (regens_moy_jour × sel_par_regen)
        self._calculer_autonomie(bcast, jours_dates)

        _LOGGER.info(
            "Full cycle — base=%d L  index=%d  regens=%d  yesterday=%s L  week=%s L  "
            "day rollover=%s",
            self._litres_jour_base, self._index_base, self._regens_jour_stable,
            self._conso_hier_stable, self._conso_semaine_stable,
            self._heure_bascule_texte() or "not learned yet",
        )
        return self._build_result(bcast)

    def _calculer_hier_semaine(self, quarts_dates: list[dict], maintenant) -> None:
        """Consommation d'hier et des 7 derniers jours, depuis les quarts d'heure.

        Découpage à minuit et précision au litre, comme l'application BWT, et
        indépendant de l'heure à laquelle l'adoucisseur change de case
        journalière. Le calcul n'est validé qu'à partir de 00 h 20 : avant, le
        dernier quart de la veille peut ne pas être encore écrit.
        """
        aujourd_hui = maintenant.date()
        hier = aujourd_hui - timedelta(days=1)
        self._conso_hier_stable    = total_sur_jours(quarts_dates, hier, hier)
        self._conso_semaine_stable = total_sur_jours(
            quarts_dates, aujourd_hui - timedelta(days=7), hier
        )
        if maintenant.hour * 60 + maintenant.minute >= 20:
            self._jour_hier_semaine = aujourd_hui.isoformat()
        _LOGGER.info(
            "Yesterday: %s L — last 7 days: %s L (from quarter-hour data)",
            self._conso_hier_stable, self._conso_semaine_stable,
        )

    def _dater_quarts(self, quarts: list[dict], idx_q: int) -> list[dict]:
        """Date les quarts d'après leur index absolu.

        L'entrée idx_q - 1 est la plus récente : elle correspond au dernier
        quart d'heure terminé. Chaque quart reçoit son instant de début
        (`debut`) et sa date calendaire (`date`).
        """
        maintenant = dt_util.now()
        ancre = maintenant.replace(
            minute=(maintenant.minute // 15) * 15, second=0, microsecond=0
        ) - timedelta(minutes=15)
        return [
            {**q, "debut": q["date"], "date": q["date"].date().isoformat()}
            for q in _dater_entrees(
                quarts, idx_q, MAX_TAB_QUART, ancre, timedelta(minutes=15)
            )
        ]

    async def _lire_plage(
        self, client, adresse: int, debut: int, nb: int, taille: int, is_quart: bool,
    ) -> list[dict]:
        """Lit `nb` entrées à partir de l'index `debut`, en repassant par zéro.

        Une plage qui franchit la fin du buffer circulaire est lue en deux
        fois : d'un seul tenant, la lecture déborderait du tableau.
        """
        if nb <= 0:
            return []
        debut %= taille
        if debut + nb <= taille:
            return await self._lire_blocs(client, adresse, debut, nb, is_quart)
        premiere = taille - debut
        return (
            await self._lire_blocs(client, adresse, debut, premiere, is_quart)
            + await self._lire_blocs(client, adresse, 0, nb - premiere, is_quart)
        )

    async def _lire_derniers(
        self, client, adresse: int, idx: int, nb: int,
        taille: int, boucle: bool, is_quart: bool,
    ) -> list[dict]:
        """Lit les `nb` dernières entrées d'un buffer circulaire, jusqu'à idx - 1.

        Tant que le buffer n'a pas fait un tour complet, seules ses `idx`
        premières cases sont écrites.
        """
        nb = min(nb, taille if boucle else idx)
        return await self._lire_plage(client, adresse, idx - nb, nb, taille, is_quart)

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

    async def _read_full_history(self) -> dict:
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

    async def _read_full_history_once(self) -> dict:
        """Une tentative : lit les deux buffers en entier et les date.

        Le buffer journalier remonte jusqu'à 5 ans ; le buffer des quarts
        d'heure couvre les 30 derniers jours, au litre près.
        """
        async with self._ble_session() as (client, bcast):
            idx_j = bcast["index_tab_jour"]
            idx_q = bcast["index_tab_quart"]
            jours = await self._lire_derniers(
                client, ADRESSE_TAB_JOUR, idx_j, MAX_TAB_JOUR,
                MAX_TAB_JOUR, bcast["loop_jour"], is_quart=False,
            )
            quarts = await self._lire_derniers(
                client, ADRESSE_TAB_QUART, idx_q, MAX_TAB_QUART,
                MAX_TAB_QUART, bcast["loop_quart"], is_quart=True,
            )

        return {
            "jours":       self._dater_jours(jours, idx_j),
            "quarts":      self._dater_quarts(quarts, idx_q),
            "aujourd_hui": dt_util.now().date(),
        }

    async def _historique(self, champ: str) -> dict:
        """Valeur par journée calendaire close, sur tout l'historique disponible.

        Les 29 derniers jours viennent des quarts d'heure : découpage à minuit
        et précision au litre, comme l'application BWT. Au-delà, des cases
        journalières, datées d'après l'heure de bascule apprise.
        """
        lecture = await self._read_full_history()
        return historique_par_jour(
            lecture["jours"], lecture["quarts"], lecture["aujourd_hui"], champ
        )

    @staticmethod
    def _par_annee_mois_jour(historique: dict) -> dict:
        result: dict = {}
        for jour, valeur in historique.items():
            annee, mois, j = jour.isoformat().split("-")
            result.setdefault(annee, {}).setdefault(mois, {})[j] = valeur
        return result

    async def service_total_consumption(self) -> dict:
        """Service get_total_consumption — total en litres jusqu'à hier inclus.

        La journée en cours n'est pas comptée : elle est déjà exposée par le
        capteur de consommation du jour.
        """
        historique = await self._historique("litres")
        total = sum(historique.values())
        jours = list(historique)

        # Tant que l'heure de bascule n'est pas apprise, le raccord entre cases
        # journalières et quarts d'heure est placé à minuit par défaut : le
        # total peut alors compter deux fois, ou pas du tout, les quelques
        # heures entre la bascule réelle et minuit. On le signale pour que les
        # automatisations qui cumulent ce total puissent attendre.
        appris = self._bascule.heure is not None
        if appris:
            _LOGGER.info("Total history: %d L over %d days", total, len(jours))
        else:
            _LOGGER.info(
                "Total history: %d L over %d days — day rollover not learned yet, "
                "the total may be off by a few hours of consumption",
                total, len(jours),
            )
        return {
            "total_liters":         total,
            "days_count":           len(jours),
            "from_date":            jours[0].isoformat() if jours else None,
            "to_date":              jours[-1].isoformat() if jours else None,
            "day_rollover_learned": appris,
        }

    async def service_history_consumption(self) -> dict:
        """Service get_history_consumption — litres par année/mois/jour."""
        return self._par_annee_mois_jour(await self._historique("litres"))

    async def service_history_regenerations(self) -> dict:
        """Service get_history_regenerations — régénérations par année/mois/jour."""
        return self._par_annee_mois_jour(await self._historique("rege"))

    # ── Construction du résultat HA ───────────────────────────────────

    def _build_result(self, bcast: dict) -> dict[str, Any]:
        return {
            KEY_SALT_PCT:              bcast["pourcentage_sel"],
            KEY_SALT_KG:               round(bcast["qte_sel_restant"] / 1000, 2),
            KEY_SALT_TOTAL_KG:         round(bcast["capa_total_sel"]  / 1000, 2),
            KEY_SALT_ALARM:            bcast["alarme"],
            KEY_WATER_METER:           (
                self._compteur_litres if self._compteur_idx is not None else None
            ),
            KEY_CONSUMPTION_TODAY:     self._litres_jour_total,
            KEY_CONSUMPTION_YESTERDAY: self._conso_hier_stable,
            KEY_CONSUMPTION_WEEK:      self._conso_semaine_stable,
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
            # Heure à laquelle cet adoucisseur ouvre une nouvelle case
            # journalière, apprise en l'observant (issue #10)
            KEY_DAY_ROLLOVER:          self._heure_bascule_texte(),
            "day_rollover_observations": len(self._bascule.observations),
        }