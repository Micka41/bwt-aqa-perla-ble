"""Datation des données de l'adoucisseur — fonctions pures, sans Home Assistant.

L'adoucisseur tient deux historiques :

- un buffer de quarts d'heure (2880 cases, 30 jours), précis au litre ;
- un buffer journalier (1825 cases, 5 ans), en dizaines de litres.

Le buffer journalier ne découpe pas les journées à minuit : chaque appareil
ouvre sa case suivante à une heure qui lui est propre (vers 4 h pour l'un,
vers 9 h 30 pour un autre), non documentée et sans rapport avec l'heure de
régénération. Ce module apprend cette heure en observant l'appareil, date les
cases journalières en conséquence, et reconstitue des journées calendaires
à partir des quarts d'heure partout où ils existent.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

MINUTES_PAR_JOUR = 1440

# Une avance d'index n'est retenue comme observation que si l'intervalle qui
# l'encadre est court : au-delà, l'heure de bascule serait trop imprécise.
FENETRE_OBSERVATION_MAX = timedelta(minutes=60)

# Médiane glissante : suit la dérive de l'horloge de l'appareil, et se recale
# en quelques jours si l'heure de bascule change (après une coupure de courant).
NB_OBSERVATIONS = 7

# Au-delà, une ancre de passage est jugée trop ancienne pour en déduire les
# bascules suivantes par simple comptage de jours.
ANCIENNETE_ANCRE_MAX = 30


def minutes_du_jour(instant: datetime) -> int:
    return instant.hour * 60 + instant.minute


def mediane_circulaire(minutes: list[int]) -> int | None:
    """Médiane d'heures de la journée, en tenant compte du passage par minuit.

    23 h 55 et 0 h 05 sont voisines : on exprime chaque valeur comme un écart
    signé à la première, dans l'intervalle [-12 h, +12 h[, avant d'en prendre
    la médiane.
    """
    if not minutes:
        return None
    ref = minutes[0]
    ecarts = sorted(
        (m - ref + MINUTES_PAR_JOUR // 2) % MINUTES_PAR_JOUR - MINUTES_PAR_JOUR // 2
        for m in minutes
    )
    n = len(ecarts)
    milieu = ecarts[n // 2] if n % 2 else (ecarts[n // 2 - 1] + ecarts[n // 2]) / 2
    return round(ref + milieu) % MINUTES_PAR_JOUR


@dataclass
class ApprentissageBascule:
    """Apprend l'heure à laquelle l'adoucisseur ouvre une nouvelle case journalière.

    L'index journalier est lu à chaque session BLE. Quand il avance d'une case
    entre deux lectures proches, la bascule a eu lieu dans cet intervalle : son
    milieu est retenu comme observation, et comme ancre de datation.
    """

    observations: list[int] = field(default_factory=list)
    # Dernière bascule observée : (index ouvert, instant)
    ancre: tuple[int, datetime] | None = None
    # Dernière lecture, pour détecter l'avance suivante (non persistée)
    _precedente: tuple[int, datetime] | None = None

    @property
    def heure(self) -> int | None:
        """Heure de bascule apprise, en minutes après minuit."""
        return mediane_circulaire(self.observations)

    def observer(self, idx: int, instant: datetime, taille: int) -> bool:
        """Enregistre une lecture de l'index ; True si une bascule a été datée."""
        precedente, self._precedente = self._precedente, (idx, instant)
        if precedente is None:
            return False

        idx_prec, t_prec = precedente
        saut = (idx - idx_prec) % taille
        if saut == 0:
            return False

        # Un saut de plusieurs cases (Home Assistant arrêté plusieurs jours) ou
        # un intervalle trop long ne permettent pas de dater la bascule.
        if saut != 1 or not timedelta(0) < instant - t_prec <= FENETRE_OBSERVATION_MAX:
            return False

        milieu = t_prec + (instant - t_prec) / 2
        self.observations = (self.observations + [minutes_du_jour(milieu)])[-NB_OBSERVATIONS:]
        self.ancre = (idx, milieu)
        return True

    def derniere_bascule(
        self, idx: int, maintenant: datetime, taille: int
    ) -> datetime | None:
        """Instant de la bascule qui a ouvert la case `idx`, ou None si inconnu."""
        heure = self.heure
        if heure is None:
            return None

        estimation = None
        if self.ancre is not None:
            idx_ancre, t_ancre = self.ancre
            ecart = (idx - idx_ancre) % taille
            if ecart <= ANCIENNETE_ANCRE_MAX:
                estimation = t_ancre + timedelta(days=ecart)

        if estimation is None:
            estimation = maintenant

        # Recaler sur l'heure apprise la plus proche de l'estimation : l'ancre
        # est une observation isolée, la médiane est plus fiable.
        candidat = estimation.replace(
            hour=heure // 60, minute=heure % 60, second=0, microsecond=0
        )
        candidats = (candidat - timedelta(days=1), candidat, candidat + timedelta(days=1))
        bascule = min(candidats, key=lambda c: abs(c - estimation))
        while bascule > maintenant:
            bascule -= timedelta(days=1)
        return bascule

    # ── Persistance ──

    def vers_dict(self) -> dict:
        return {
            "observations": list(self.observations),
            "ancre": (
                {"idx": self.ancre[0], "instant": self.ancre[1].isoformat()}
                if self.ancre else None
            ),
        }

    @classmethod
    def depuis_dict(cls, donnees: dict | None) -> ApprentissageBascule:
        appr = cls()
        if not donnees:
            return appr
        appr.observations = [
            int(m) % MINUTES_PAR_JOUR for m in donnees.get("observations", [])
        ][-NB_OBSERVATIONS:]
        ancre = donnees.get("ancre")
        if ancre:
            try:
                appr.ancre = (int(ancre["idx"]), datetime.fromisoformat(ancre["instant"]))
            except (KeyError, TypeError, ValueError):
                appr.ancre = None
        return appr


def dater_cases(
    cases: list[dict], idx_courant: int, taille: int, derniere_bascule: datetime
) -> list[dict]:
    """Attribue à chaque case journalière sa période et sa date.

    La case idx_courant - 1 est la dernière close : elle couvre les 24 heures
    qui précèdent `derniere_bascule`. Les autres s'en déduisent en remontant
    le buffer par leur index absolu.

    Une case couvre deux journées calendaires ; elle prend la date de celle où
    elle passe le plus d'heures — la date de début si elle commence avant midi.
    """
    datees = []
    for case in cases:
        rang = (idx_courant - 1 - case["idx"]) % taille
        fin = derniere_bascule - timedelta(days=rang)
        debut = fin - timedelta(days=1)
        etiquette = debut.date() if minutes_du_jour(debut) <= 720 else fin.date()
        datees.append({**case, "debut": debut, "fin": fin, "date": etiquette.isoformat()})
    return datees


def _valeurs_par_quart(quarts: list[dict], champ: str) -> list[tuple[datetime, int]]:
    """(début, valeur) de chaque quart, triés chronologiquement.

    Pour les litres, la valeur est le volume du quart. Pour les régénérations,
    c'est 1 au premier quart d'une régénération : une régénération s'étale sur
    plusieurs quarts mais ne doit compter qu'une fois.
    """
    tries = sorted(quarts, key=lambda q: q["debut"])
    if champ == "litres":
        return [(q["debut"], q["litres"]) for q in tries]
    valeurs, precedent = [], False
    for q in tries:
        valeurs.append((q["debut"], 1 if q["rege"] and not precedent else 0))
        precedent = bool(q["rege"])
    return valeurs


def historique_par_jour(
    cases: list[dict], quarts: list[dict], aujourd_hui: date, champ: str
) -> dict[date, int]:
    """Valeur par journée calendaire, des cases journalières et des quarts d'heure.

    Partout où les quarts d'heure couvrent une journée entière, ils font foi :
    découpage à minuit et précision au litre, comme l'application BWT. Les
    cases journalières complètent l'historique au-delà.

    Au raccord, la dernière case utilisée chevauche le premier jour couvert par
    les quarts : on en retire ce que les quarts ont déjà compté, pour que le
    total reste exact. La journée en cours est exclue, n'étant pas close.

    `champ` vaut "litres" ou "rege".
    """
    resultat: dict[date, int] = defaultdict(int)
    valeurs = _valeurs_par_quart(quarts, champ)

    raccord: datetime | None = None
    if valeurs:
        premier = valeurs[0][0]
        premier_jour = premier.date() if minutes_du_jour(premier) == 0 else premier.date() + timedelta(days=1)
        raccord = datetime.combine(premier_jour, time(0), tzinfo=premier.tzinfo)
        for instant, valeur in valeurs:
            if raccord <= instant and instant.date() < aujourd_hui:
                resultat[instant.date()] += valeur

    for case in cases:
        valeur = case["litres"] if champ == "litres" else case["rege"]
        if raccord is None or case["fin"] <= raccord:
            resultat[date.fromisoformat(case["date"])] += valeur
        elif case["debut"] < raccord:
            deja_compte = sum(v for t, v in valeurs if raccord <= t < case["fin"])
            veille = (raccord - timedelta(days=1)).date()
            resultat[veille] += max(0, valeur - deja_compte)
        # Case entièrement couverte par les quarts : déjà comptée

    return dict(sorted(resultat.items()))


def total_sur_jours(quarts: list[dict], premier: date, dernier: date) -> int | None:
    """Litres consommés du `premier` au `dernier` jour inclus, d'après les quarts."""
    retenus = [q["litres"] for q in quarts if premier <= q["debut"].date() <= dernier]
    return sum(retenus) if retenus else None
