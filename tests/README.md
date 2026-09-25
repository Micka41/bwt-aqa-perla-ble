# Plan de test — bwt_aqa_perla_ble

Suite de tests exécutable **sans installer Home Assistant** : `conftest.py` injecte
des stubs légers de `homeassistant` et `bleak` dans `sys.modules` avant l'import du
composant, qui est donc testé tel quel, sans modification.

## Exécution

Depuis la racine du dépôt :

```bash
pip install -r requirements-test.txt
python3 -m pytest -q                              # tout
python3 -m pytest -v                              # détaillé
python3 -m pytest tests/test_logic.py::TestAutonomie   # une classe
```

Un test qui dépasse 30 secondes est interrompu par `pytest-timeout` : le
dépassement signale une attente réelle laissée dans le code, les délais BLE
étant raccourcis par les fixtures.

Les tests tournent aussi en CI à chaque push et pull request
(`.github/workflows/tests.yml`, Python 3.12 et 3.13).

Durée : moins d'une seconde. Les timeouts BLE sont raccourcis par la fixture
`patched_ble`, sinon la suite prendrait plus d'une minute.

## Organisation

| Fichier | Portée |
|---|---|
| `conftest.py` | Stubs HA/bleak, horloge contrôlable, constructeurs de trames |
| `test_protocol.py` | Décodage BROADCAST et notifications, commandes, issue #4 |
| `test_logic.py` | Autonomie, consolidation hier/semaine, historique debug, résultat |
| `test_ble_cycles.py` | Cycles rapide/complet, buffers circulaires, services, échecs |
| `test_datation.py` | Apprentissage de l'heure de bascule, datation des cases journalières, fusion avec les quarts |
| `test_package.py` | Manifest, traductions, cohérence des clés et des services |

## Outillage

**Horloge contrôlable** — `dt_util.now()` est branché sur `FakeClock`, ce qui rend
testables la consolidation à 04h00, le reset minuit et le figement de la date
d'autonomie :

```python
def test_exemple(coordinator, clock):
    clock.set(hour=6)          # se placer après la consolidation BWT
    clock.advance(days=1)      # avancer d'un jour
```

**Constructeurs de trames** — `make_broadcast()`, `make_notification()`,
`quart_word()`, `jour_word()` produisent des trames binaires valides. Pour un
firmware V2.x, `make_broadcast(qte_sel_g=36400, version=(2, 21))` encode
automatiquement la valeur multipliée par 4 attendue par le décodeur.

**Appareil simulé** — `FakeBwtDevice` répond aux commandes READ par des
notifications, permet d'inspecter les adresses lues (`dev.reads`) et de simuler
une panne de lecture (`fail_after_blocks=1`).

## Correctifs verrouillés

Chaque correctif a son test d'acceptation ; une régression les fait échouer.

| Test | Ce qu'il garantit |
|---|---|
| `test_lecture_partielle_devrait_lever` | Une lecture BLE incomplète lève `UpdateFailed` au lieu de renvoyer des données tronquées |
| `test_regeneration_manquee_apres_minuit` | Une régénération est détectée même après la remise à zéro du compteur |
| `test_date_reinitialisee_si_calcul_impossible` | La date de fin disparaît avec les autres capteurs d'autonomie |
| `test_conso_faible_apres_4h_devient_disponible` | Une consommation sous 10 L/jour ne bloque pas les capteurs |
| `test_etat_sous_255_caracteres` | L'entité de diagnostic respecte la limite de longueur d'un état HA |
| `TestPersistance` (6 tests) | La date de fin d'autonomie survit à un redémarrage sans glisser |
| `test_pas_dimport_local_redondant` | Pas d'import local masquant le module importé en tête de fichier |
| `TestSessionBLE` (6 tests) | Le context manager déconnecte toujours, même si le bloc lève |
| `test_issue_10_rejouee_de_bout_en_bout` | Deux lectures encadrant la bascule de l'adoucisseur gardent chaque valeur à sa date |
| `TestServices` | Les 29 derniers jours viennent des quarts, au litre près, sans double comptage au raccord |
| `TestHierSemaine` | Hier et 7 jours découpés à minuit, indépendants de l'heure de bascule |
| `test_suppression_de_l_integration_efface_l_etat` | Réinstaller repart de zéro |
| `test_toutes_les_cles_const_sont_utilisees` | Aucune constante orpheline |

## Régressions couvertes

Ces tests verrouillent des bugs déjà corrigés, pour qu'ils ne reviennent pas :

- `test_toutes_les_cles_utilisees_sont_importees` — le `NameError` sur
  `KEY_DEBUG_BROADCAST` qui empêchait le chargement de l'intégration
- `test_date_autonomie_est_un_objet_date` — `device_class: DATE` exige un objet
  `date`, une chaîne provoque une `ValueError`
- `test_etat_sous_255_caracteres` — limite de longueur d'un état HA
- `test_conso_faible_apres_4h_devient_disponible` — consommation < 10 L/jour
  bloquant les capteurs sur « indisponible »
- `test_firmware_v2_divides_by_four` et la série de calibration — issue #4


## Pourquoi pas pytest-homeassistant-custom-component

C'est le plugin de référence pour tester une intégration Home Assistant : il
fournit une instance réelle de HA et ses fixtures officielles (`hass`,
`MockConfigEntry`), ce qui permet de tester le flux de configuration, le
registre d'entités et l'enregistrement des services pour de vrai.

Il n'est pas utilisé ici, pour deux raisons.

**Chaque version du plugin est épinglée à une version de Home Assistant.** Il
faudrait la relever à chaque publication mensuelle, faute de quoi la CI
validerait le composant contre une version périmée.

**Il est incompatible avec l'approche par doublures.** `conftest.py` injecte de
faux modules `homeassistant` dans `sys.modules` ; avec le vrai Home Assistant
installé, ces doublures le masqueraient. Adopter le plugin demanderait de
réécrire l'ensemble de la suite.

Le compromis retenu privilégie ce que ce composant a de spécifique : le
décodage du protocole BLE, la datation des entrées, les buffers circulaires et
les règles de consolidation — du code qui n'a besoin d'aucun Home Assistant
pour être vérifié, et qui se teste en moins d'une seconde.

Ce que la suite ne couvre donc pas, et qu'un passage au plugin apporterait :
le flux de configuration, la découverte Bluetooth, le cycle de vie de l'entrée
de configuration et le rendu réel des entités. Ces parties restent validées par
l'usage.
