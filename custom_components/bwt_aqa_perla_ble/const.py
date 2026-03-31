"""Constants for BWT AQA Perla integration."""

DOMAIN = "bwt_aqa_perla_ble"

# ── Intervalles des deux cycles ─────────────────────────────────────────────
INTERVALLE_RAPIDE_S  = 900    # 15 min — cycle rapide (BROADCAST + delta quarts)
INTERVALLE_COMPLET_H = 1      # 1h     — cycle complet (historique jour + quarts)

# Alias utilisé par DataUpdateCoordinator (= cycle rapide)
SCAN_INTERVAL = INTERVALLE_RAPIDE_S

# Nombre de quarts lus en cycle complet (~30h pour couvrir la journée)
NB_QUARTS_COMPLET = 120
# Nombre de jours lus en cycle complet
# 30 jours minimum pour CalcAutonomie() — on prend 60 pour les adoucisseurs
# à faible fréquence de régénération (1 tous les 7 jours → 30j insuffisant)
NB_JOURS_COMPLET  = 60

# ── UUIDs BLE réels du protocole BWT AQA Perla ──────────────────────────────
UUID_SERVICE   = "D973F2E0-B19E-11E2-9E96-0800200C9A66"
UUID_READ1     = "D973F2E1-B19E-11E2-9E96-0800200C9A66"  # Notifications historique
UUID_WRITE     = "D973F2E2-B19E-11E2-9E96-0800200C9A66"  # Commandes READ_BUFFER / BREAK
UUID_BROADCAST = "D973F2E3-B19E-11E2-9E96-0800200C9A66"  # Sel + index (lecture directe)
UUID_OTHER     = "D973F2E4-B19E-11E2-9E96-0800200C9A66"  # Authentification (lecture)

# iBeacon manufacturer data BWT ("BestWaterTechno\0")
RECO_BWT = bytes([
    0x42, 0x65, 0x73, 0x74, 0x57, 0x61, 0x74, 0x65,
    0x72, 0x54, 0x65, 0x63, 0x68, 0x6E, 0x6F, 0x00,
])
MAX_TAB_QUART      = 2880   # 30 jours × 96 quarts d'heure
MAX_TAB_JOUR       = 1825   # 5 ans × 365 jours
ADRESSE_TAB_QUART  = 0
ADRESSE_TAB_JOUR   = 6400

# Nombre de quarts d'heure lus en cycle rapide (= 1 heure glissante + marge)
NB_QUARTS_RAPIDE   = 100    # ~25 heures, couvre largement la journée en cours

# Timeouts BLE
BLE_CONNECT_TIMEOUT  = 30.0   # secondes
BLE_NOTIFY_SILENCE   = 1.5    # secondes de silence = fin de bloc
BLE_NOTIFY_TIMEOUT   = 15.0   # timeout total par bloc

# ── Clés des capteurs HA ─────────────────────────────────────────────────────
KEY_SALT_PCT           = "salt_percent"
KEY_SALT_KG            = "salt_kg"
KEY_SALT_TOTAL_KG      = "salt_total_kg"
KEY_SALT_ALARM         = "salt_alarm"
KEY_CONSUMPTION_TODAY  = "consumption_today"
KEY_CONSUMPTION_YESTERDAY = "consumption_yesterday"
KEY_CONSUMPTION_WEEK   = "consumption_week"
KEY_REGEN_TODAY        = "regen_today"
KEY_REGEN_YESTERDAY    = "regen_yesterday"
KEY_SALT_AUTONOMY_DAYS  = "salt_autonomy_days"
KEY_SALT_AUTONOMY_WEEKS = "salt_autonomy_weeks"
KEY_AVG_DAILY_30D      = "avg_daily_consumption_30d"
KEY_LAST_SYNC          = "last_sync"
KEY_FIRMWARE           = "firmware"
