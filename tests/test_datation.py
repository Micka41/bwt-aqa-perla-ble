"""Tests de la datation : apprentissage de la bascule, cases journalières, fusion."""
from datetime import date, datetime, timedelta, timezone

from custom_components.bwt_aqa_perla_ble.datation import (
    ApprentissageBascule,
    dater_cases,
    historique_par_jour,
    mediane_circulaire,
    total_sur_jours,
)

TZ = timezone(timedelta(hours=2))
TAILLE = 1825


def dt(jour: int, h: int, m: int = 0, mois: int = 9) -> datetime:
    return datetime(2026, mois, jour, h, m, tzinfo=TZ)


def hm(h: int, m: int = 0) -> int:
    return h * 60 + m


# ── Médiane circulaire ───────────────────────────────────────────────────────

class TestMediane:
    def test_vide(self):
        assert mediane_circulaire([]) is None

    def test_simple(self):
        assert mediane_circulaire([hm(9, 30), hm(9, 20), hm(9, 40)]) == hm(9, 30)

    def test_autour_de_minuit(self):
        """23 h 50, 0 h 00 et 0 h 10 ont pour médiane minuit, pas midi."""
        assert mediane_circulaire([hm(23, 50), hm(0, 0), hm(0, 10)]) == 0

    def test_valeur_aberrante_sans_effet(self):
        valeurs = [hm(9, 30)] * 5 + [hm(21, 0)]
        assert mediane_circulaire(valeurs) == hm(9, 30)


# ── Apprentissage ────────────────────────────────────────────────────────────

class TestApprentissage:
    def test_premiere_lecture_ne_date_rien(self):
        a = ApprentissageBascule()
        assert a.observer(100, dt(22, 9), TAILLE) is False
        assert a.heure is None

    def test_index_inchange(self):
        a = ApprentissageBascule()
        a.observer(100, dt(22, 9), TAILLE)
        assert a.observer(100, dt(22, 9, 15), TAILLE) is False

    def test_avance_datee_au_milieu_de_la_fenetre(self):
        a = ApprentissageBascule()
        a.observer(100, dt(22, 9, 20), TAILLE)
        assert a.observer(101, dt(22, 9, 35), TAILLE) is True
        assert a.heure == hm(9, 27)            # milieu de 9 h 20 – 9 h 35
        assert a.ancre[0] == 101

    def test_fenetre_trop_longue_ignoree(self):
        """Des cycles ratés élargissent la fenêtre : la bascule reste indatée."""
        a = ApprentissageBascule()
        a.observer(100, dt(22, 8), TAILLE)
        assert a.observer(101, dt(22, 10), TAILLE) is False
        assert a.heure is None

    def test_saut_de_plusieurs_cases_ignore(self):
        a = ApprentissageBascule()
        a.observer(100, dt(22, 9, 20), TAILLE)
        assert a.observer(102, dt(22, 9, 35), TAILLE) is False

    def test_passage_par_zero_du_buffer(self):
        a = ApprentissageBascule()
        a.observer(TAILLE - 1, dt(22, 9, 20), TAILLE)
        assert a.observer(0, dt(22, 9, 35), TAILLE) is True

    def test_garde_les_sept_dernieres(self):
        a = ApprentissageBascule()
        for jour in range(1, 12):
            a.observer(jour, dt(jour, 9, 20), TAILLE)
            a.observer(jour + 1, dt(jour, 9, 35), TAILLE)
        assert len(a.observations) == 7

    def test_suit_la_derive_de_l_horloge(self):
        """La bascule glisse de 9 h 30 à 10 h : la médiane suit en quelques jours."""
        a = ApprentissageBascule()
        idx = 0
        for jour, (h, m) in enumerate([(9, 30)] * 7 + [(10, 0)] * 4, start=1):
            debut = dt(jour, h, m) - timedelta(minutes=5)
            a.observer(idx, debut, TAILLE)
            idx += 1
            a.observer(idx, debut + timedelta(minutes=10), TAILLE)
        assert a.heure == hm(10, 0)

    def test_persistance(self):
        a = ApprentissageBascule()
        a.observer(100, dt(22, 9, 20), TAILLE)
        a.observer(101, dt(22, 9, 35), TAILLE)
        b = ApprentissageBascule.depuis_dict(a.vers_dict())
        assert b.observations == a.observations
        assert b.ancre == a.ancre

    def test_persistance_corrompue(self):
        b = ApprentissageBascule.depuis_dict({"observations": [570], "ancre": {"idx": "x"}})
        assert b.observations == [570] and b.ancre is None
        assert ApprentissageBascule.depuis_dict(None).heure is None


# ── Dernière bascule ─────────────────────────────────────────────────────────

def appris(heure_h: int, heure_m: int, idx: int, jour: int) -> ApprentissageBascule:
    """Apprentissage ayant observé une bascule vers idx le `jour` à heure_h:heure_m."""
    a = ApprentissageBascule()
    t = dt(jour, heure_h, heure_m)
    a.observer(idx - 1, t - timedelta(minutes=5), TAILLE)
    a.observer(idx, t + timedelta(minutes=5), TAILLE)
    return a


class TestDerniereBascule:
    def test_inconnue_sans_observation(self):
        assert ApprentissageBascule().derniere_bascule(100, dt(22, 12), TAILLE) is None

    def test_meme_case_que_l_ancre(self):
        a = appris(9, 30, 113, 22)
        assert a.derniere_bascule(113, dt(22, 21), TAILLE) == dt(22, 9, 30)

    def test_cases_suivantes_par_comptage(self):
        """Home Assistant redémarré : deux bascules ont eu lieu depuis l'ancre."""
        a = appris(9, 30, 113, 22)
        assert a.derniere_bascule(115, dt(24, 12), TAILLE) == dt(24, 9, 30)

    def test_jamais_dans_le_futur(self):
        a = appris(9, 30, 113, 22)
        assert a.derniere_bascule(113, dt(22, 9, 31), TAILLE) <= dt(22, 9, 31)


# ── Datation des cases ───────────────────────────────────────────────────────

def cases(*valeurs, fin_idx: int) -> list[dict]:
    """Cases consécutives se terminant à l'index fin_idx - 1."""
    n = len(valeurs)
    return [{"idx": fin_idx - n + i, "litres": v, "rege": 0} for i, v in enumerate(valeurs)]


class TestDaterCases:
    def test_bascule_a_minuit_reproduit_l_ancien_comportement(self):
        """Tant que rien n'est appris, la dernière case est « hier »."""
        datees = dater_cases(cases(10, 20, fin_idx=113), 113, TAILLE, dt(22, 0))
        assert [c["date"] for c in datees] == ["2026-09-20", "2026-09-21"]

    def test_bascule_matinale_date_de_debut(self):
        """Bascule à 4 h : la case 21/4 h – 22/4 h est le 21."""
        datees = dater_cases(cases(10, fin_idx=113), 113, TAILLE, dt(22, 4))
        assert datees[0]["date"] == "2026-09-21"
        assert datees[0]["debut"] == dt(21, 4) and datees[0]["fin"] == dt(22, 4)

    def test_bascule_apres_midi_date_de_fin(self):
        """Bascule à 14 h : la case 21/14 h – 22/14 h passe 14 h le 22."""
        datees = dater_cases(cases(10, fin_idx=113), 113, TAILLE, dt(22, 14))
        assert datees[0]["date"] == "2026-09-22"

    def test_issue_10_deux_lectures_memes_dates(self):
        """Le cas de jflefebvre06 : 9 h (avant la bascule) et 10 h (après).

        Entre les deux lectures, l'adoucisseur ouvre une case : la fenêtre de
        lecture glisse d'un cran. Avec l'ancienne règle, les 21 valeurs communes
        changeaient toutes de date. Datées d'après la bascule apprise, elles
        gardent chacune la leur.
        """
        a = appris(9, 30, 113, 21)                 # bascule apprise la veille
        valeurs = [80, 110, 2170, 910, 70, 180, 220, 110, 110, 110, 90,
                   110, 120, 140, 130, 90, 90, 1940, 250, 120, 200, 140, 90]

        # Test 1 à 9 h : la case 113 n'est pas encore ouverte
        l1 = a.derniere_bascule(113, dt(22, 9), TAILLE)
        t1 = dater_cases(cases(*valeurs[:22], fin_idx=113), 113, TAILLE, l1)

        # L'appareil bascule à 9 h 30 ; le coordinator l'observe
        a.observer(113, dt(22, 9, 25), TAILLE)
        a.observer(114, dt(22, 9, 40), TAILLE)

        # Test 2 à 10 h : une case de plus
        l2 = a.derniere_bascule(114, dt(22, 10), TAILLE)
        t2 = dater_cases(cases(*valeurs[1:], fin_idx=114), 114, TAILLE, l2)

        par_date_1 = {c["date"]: c["litres"] for c in t1}
        par_date_2 = {c["date"]: c["litres"] for c in t2}
        communes = set(par_date_1) & set(par_date_2)
        assert len(communes) == 21
        assert all(par_date_1[d] == par_date_2[d] for d in communes)


# ── Historique par jour ──────────────────────────────────────────────────────

def quarts_constants(debut: datetime, fin: datetime, litres: int = 1) -> list[dict]:
    q, t = [], debut
    while t < fin:
        q.append({"debut": t, "litres": litres, "rege": False})
        t += timedelta(minutes=15)
    return q


class TestHistoriqueParJour:
    AUJOURDHUI = date(2026, 9, 22)

    def test_quarts_seuls_decoupes_a_minuit(self):
        q = quarts_constants(dt(20, 0), dt(22, 10))
        h = historique_par_jour([], q, self.AUJOURDHUI, "litres")
        assert h == {date(2026, 9, 20): 96, date(2026, 9, 21): 96}

    def test_premier_jour_partiel_ecarte(self):
        """Les quarts commencent à 6 h le 19 : le 19 est laissé aux cases."""
        q = quarts_constants(dt(19, 6), dt(22, 0))
        h = historique_par_jour([], q, self.AUJOURDHUI, "litres")
        assert date(2026, 9, 19) not in h

    def test_cases_seules(self):
        c = dater_cases(cases(100, 200, fin_idx=113), 113, TAILLE, dt(22, 0))
        h = historique_par_jour(c, [], self.AUJOURDHUI, "litres")
        assert h == {date(2026, 9, 20): 100, date(2026, 9, 21): 200}

    def test_raccord_sans_double_comptage(self):
        """Bascule à 9 h 30, quarts à partir du 20 à minuit.

        La case 19/9 h 30 – 20/9 h 30 chevauche le 20 : ses heures d'après
        minuit, déjà dans les quarts, en sont retirées.
        """
        # Cases : 18/9h30–19/9h30 = 400, 19/9h30–20/9h30 = 500,
        # 20/9h30–21/9h30 = 600, 21/9h30–22/9h30 = 700 ; quarts de 1 L dès le 20 à 0 h
        c = dater_cases(cases(400, 500, 600, 700, fin_idx=114), 114, TAILLE, dt(22, 9, 30))
        q = quarts_constants(dt(20, 0), dt(22, 12))
        h = historique_par_jour(c, q, self.AUJOURDHUI, "litres")

        quarts_avant_fin_case = 38            # 20/0 h – 20/9 h 30
        assert h[date(2026, 9, 19)] == 500 - quarts_avant_fin_case
        assert h[date(2026, 9, 18)] == 400
        assert h[date(2026, 9, 20)] == 96 and h[date(2026, 9, 21)] == 96

    def test_total_conserve_au_raccord(self):
        """Le total ne dépend pas de l'endroit où l'on bascule vers les quarts."""
        c = dater_cases(cases(400, 500, 600, 700, fin_idx=114), 114, TAILLE, dt(22, 9, 30))
        q = quarts_constants(dt(20, 0), dt(22, 12))
        h = historique_par_jour(c, q, self.AUJOURDHUI, "litres")
        # Consommation réelle jusqu'au 22 à minuit : cases jusqu'au 20/0 h
        # (400 + 500 − 38) + quarts du 20 et du 21 (2 × 96)
        assert sum(h.values()) == 400 + 500 - 38 + 96 * 2

    def test_aujourd_hui_exclu(self):
        q = quarts_constants(dt(21, 0), dt(22, 12))
        h = historique_par_jour([], q, self.AUJOURDHUI, "litres")
        assert date(2026, 9, 22) not in h

    def test_regeneration_comptee_une_fois_le_jour_ou_elle_commence(self):
        q = quarts_constants(dt(20, 0), dt(22, 0))
        for quart in q:
            if dt(20, 23, 30) <= quart["debut"] < dt(21, 1):   # à cheval sur minuit
                quart["rege"] = True
        h = historique_par_jour([], q, self.AUJOURDHUI, "rege")
        assert h == {date(2026, 9, 20): 1, date(2026, 9, 21): 0}

    def test_raccord_sans_resultat_negatif(self):
        """L'arrondi à 10 L des cases peut rendre la soustraction négative."""
        c = dater_cases(cases(10, 20, fin_idx=113), 113, TAILLE, dt(22, 9, 30))
        q = quarts_constants(dt(21, 0), dt(22, 12), litres=5)
        h = historique_par_jour(c, q, self.AUJOURDHUI, "litres")
        assert min(h.values()) >= 0


def test_total_sur_jours():
    q = quarts_constants(dt(15, 0), dt(22, 0))
    assert total_sur_jours(q, date(2026, 9, 21), date(2026, 9, 21)) == 96
    assert total_sur_jours(q, date(2026, 9, 15), date(2026, 9, 21)) == 7 * 96
    assert total_sur_jours(q, date(2026, 9, 1), date(2026, 9, 2)) is None
