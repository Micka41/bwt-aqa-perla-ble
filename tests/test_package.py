"""Validation statique du paquet : manifest, traductions, cohérence des clés.

Ces tests attrapent les erreurs qui cassent l'intégration au chargement
(clé non importée, traduction manquante, service non déclaré).
"""
import ast
import json
from pathlib import Path

import pytest

COMPONENT = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "bwt_aqa_perla_ble"
)
TRANSLATIONS = COMPONENT / "translations"
LANGUES = ["fr", "en", "de", "it"]


def charger(nom: str) -> dict:
    return json.loads((TRANSLATIONS / f"{nom}.json").read_text(encoding="utf-8"))


# ── Syntaxe et imports ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "fichier",
    ["__init__.py", "const.py", "coordinator.py", "sensor.py",
     "binary_sensor.py", "config_flow.py"],
)
def test_syntaxe_python(fichier):
    ast.parse((COMPONENT / fichier).read_text(encoding="utf-8"))


@pytest.mark.parametrize("fichier", ["coordinator.py", "sensor.py", "binary_sensor.py"])
def test_toutes_les_cles_utilisees_sont_importees(fichier):
    """Régression : KEY_DEBUG_BROADCAST utilisé sans import → NameError au chargement."""
    source = (COMPONENT / fichier).read_text(encoding="utf-8")
    arbre = ast.parse(source)

    importees = {
        alias.name
        for noeud in ast.walk(arbre)
        if isinstance(noeud, ast.ImportFrom) and noeud.module == ".const" or
           (isinstance(noeud, ast.ImportFrom) and noeud.module == "const")
        for alias in noeud.names
    }
    # ast.ImportFrom pour "from .const import X" a module="const" et level=1
    importees |= {
        alias.name
        for noeud in ast.walk(arbre)
        if isinstance(noeud, ast.ImportFrom) and noeud.level == 1
        for alias in noeud.names
    }

    utilisees = {
        noeud.id
        for noeud in ast.walk(arbre)
        if isinstance(noeud, ast.Name) and noeud.id.startswith("KEY_")
    }
    manquantes = utilisees - importees
    assert not manquantes, f"{fichier} : clés utilisées sans import → {manquantes}"


def test_pas_dimport_local_redondant():
    """Les imports locaux masquent les patches et dupliquent le haut de fichier."""
    source = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
    arbre = ast.parse(source)

    globaux = {
        alias.asname or alias.name
        for noeud in arbre.body
        if isinstance(noeud, (ast.Import, ast.ImportFrom))
        for alias in noeud.names
    }
    locaux = []
    for noeud in ast.walk(arbre):
        if isinstance(noeud, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for sous in ast.walk(noeud):
                if isinstance(sous, (ast.Import, ast.ImportFrom)) and sous is not noeud:
                    for alias in sous.names:
                        if alias.name in globaux:
                            locaux.append(f"{noeud.name}: {alias.name}")
    assert not locaux, f"imports locaux redondants → {locaux}"


# ── Manifest ─────────────────────────────────────────────────────────────────

def test_manifest_champs_requis():
    m = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    for champ in ("domain", "name", "codeowners", "documentation",
                  "iot_class", "issue_tracker", "version", "config_flow"):
        assert champ in m, f"champ manquant : {champ}"
    assert m["domain"] == "bwt_aqa_perla_ble"
    assert m["iot_class"] == "local_polling"
    assert m["codeowners"], "codeowners ne doit pas être vide (requis par HACS)"


def test_manifest_version_semver():
    m = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    parties = m["version"].split(".")
    assert len(parties) >= 2 and all(p.isdigit() for p in parties)


# ── Traductions ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("langue", LANGUES)
def test_traduction_json_valide(langue):
    charger(langue)


@pytest.mark.parametrize("langue", LANGUES)
def test_meme_jeu_de_capteurs(langue):
    ref = set(charger("en")["entity"]["sensor"])
    autre = set(charger(langue)["entity"]["sensor"])
    assert autre == ref, f"{langue} : écart → {ref ^ autre}"


def test_chaque_capteur_a_une_traduction():
    from custom_components.bwt_aqa_perla_ble import sensor as sensor_mod
    traduits = set(charger("en")["entity"]["sensor"])
    for desc in sensor_mod.SENSORS:
        assert desc.translation_key in traduits, f"non traduit : {desc.translation_key}"


def test_chaque_binary_sensor_a_une_traduction():
    from custom_components.bwt_aqa_perla_ble import binary_sensor as bs_mod
    traduits = set(charger("en").get("entity", {}).get("binary_sensor", {}))
    cles = {
        getattr(d, "translation_key", None)
        for d in getattr(bs_mod, "BINARY_SENSORS", [])
    } or {"salt_alarm"}
    for cle in cles:
        if cle:
            assert cle in traduits, f"binary_sensor non traduit : {cle}"


def test_strings_json_synchronise_avec_en():
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    assert strings == charger("en"), "strings.json désynchronisé de en.json"


@pytest.mark.parametrize("langue", LANGUES)
def test_aucun_nom_vide(langue):
    for cle, valeur in charger(langue)["entity"]["sensor"].items():
        assert valeur.get("name", "").strip(), f"{langue}/{cle} : nom vide"


# ── Services ─────────────────────────────────────────────────────────────────

def test_services_declares_et_implementes():
    source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    yaml_src = (COMPONENT / "services.yaml").read_text(encoding="utf-8")

    declares = {
        ligne.split(":")[0].strip()
        for ligne in yaml_src.splitlines()
        if ligne and not ligne.startswith((" ", "#") ) and ":" in ligne
    }
    for service in declares:
        assert service in source, f"{service} déclaré dans services.yaml mais non enregistré"


def test_services_ont_une_methode_coordinator():
    from custom_components.bwt_aqa_perla_ble.coordinator import BwtCoordinator
    for methode in ("service_total_consumption",
                    "service_history_consumption",
                    "service_history_regenerations"):
        assert hasattr(BwtCoordinator, methode), f"méthode absente : {methode}"


def test_pas_de_reference_a_get_full_history():
    """Régression : l'ancienne méthode a été remplacée par les 3 services."""
    for fichier in ("__init__.py", "coordinator.py", "services.yaml"):
        source = (COMPONENT / fichier).read_text(encoding="utf-8")
        assert "get_full_history" not in source, f"{fichier} référence get_full_history"


# ── Entités ──────────────────────────────────────────────────────────────────

def test_entite_debug_est_diagnostic_et_desactivee():
    from custom_components.bwt_aqa_perla_ble import sensor as sensor_mod
    from custom_components.bwt_aqa_perla_ble.const import KEY_DEBUG_BROADCAST
    desc = next(d for d in sensor_mod.SENSORS if d.key == KEY_DEBUG_BROADCAST)
    assert desc.entity_category == "diagnostic"
    assert desc.entity_registry_enabled_default is False


def test_cles_de_capteurs_uniques():
    from custom_components.bwt_aqa_perla_ble import sensor as sensor_mod
    cles = [d.key for d in sensor_mod.SENSORS]
    assert len(cles) == len(set(cles)), "clés dupliquées dans SENSORS"


def test_toutes_les_cles_const_sont_utilisees():
    """Détecte les constantes orphelines."""
    from custom_components.bwt_aqa_perla_ble import const
    cles = {n for n in dir(const) if n.startswith("KEY_")}
    sources = "".join(
        (COMPONENT / f).read_text(encoding="utf-8")
        for f in ("coordinator.py", "sensor.py", "binary_sensor.py")
    )
    orphelines = {c for c in cles if sources.count(c) <= 1}
    assert not orphelines, f"constantes définies mais inutilisées → {orphelines}"


def test_unites_traduites_ou_universelles():
    """Une unité est soit un symbole universel, soit traduite dans strings.json.

    Home Assistant sait traduire les unités depuis 2024.11, via la clé
    `unit_of_measurement` placée à côté du nom de l'entité. La documentation
    impose alors de ne pas définir `native_unit_of_measurement` : les deux
    mécanismes s'excluent.
    """
    from custom_components.bwt_aqa_perla_ble import sensor as sensor_mod

    universelles = {"%", "kg", "L"}
    traduites = {
        cle for cle, val in charger("en")["entity"]["sensor"].items()
        if "unit_of_measurement" in val
    }

    for desc in sensor_mod.SENSORS:
        native = getattr(desc, "native_unit_of_measurement", None)
        if native is not None:
            assert native in universelles, (
                f"{desc.key} : « {native} » n'est pas un symbole universel ; "
                "utiliser une unité traduite dans strings.json"
            )
            assert desc.translation_key not in traduites, (
                f"{desc.key} : unité définie deux fois — native et traduite"
            )


@pytest.mark.parametrize("langue", LANGUES)
def test_unites_traduites_dans_toutes_les_langues(langue):
    """Une unité traduite en anglais doit l'être dans les quatre langues."""
    ref = {
        cle for cle, val in charger("en")["entity"]["sensor"].items()
        if "unit_of_measurement" in val
    }
    autre = {
        cle for cle, val in charger(langue)["entity"]["sensor"].items()
        if "unit_of_measurement" in val
    }
    assert autre == ref, f"{langue} : unités manquantes → {ref - autre}"
