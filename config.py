"""Configuration centrale du projet : chemins, paramètres réseau, URLs des
services ArcGIS REST du Géoportail de Wallonie, et règle de remplissage de
chaque colonne du fichier de sortie.

Aucune URL de service, aucun identifiant de couche, aucun nom de champ
ArcGIS n'est codé en dur ailleurs dans le projet : tout passe par ce
fichier (`ARCGIS_SERVICES` et `COLUMN_RULES`), conformément au cahier des
charges.
"""

from __future__ import annotations

import sys
from pathlib import Path

from models.rules import ColumnRule, LayerRef, RuleType

# ---------------------------------------------------------------------------
# Version de l'application
# ---------------------------------------------------------------------------

# Affichée dans l'interface (voir gui.py) et incluse dans le nom de
# l'exécutable construit (voir build-onefile.bat/build-onedir.bat) — pour
# qu'un utilisateur ne se
# retrouve jamais à utiliser une ancienne version sans le savoir (cas réel :
# une rue déjà marquée "redécouverte" par une ancienne version ne profite
# d'aucun correctif ultérieur tant que l'app n'est pas mise à jour, sans
# aucun signe visible de la version utilisée jusqu'ici). À incrémenter à
# chaque nouvelle construction distribuée aux collaborateurs.
APP_VERSION = "1.2.5"

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

# En exécutable PyInstaller --onefile, `__file__` pointe vers le dossier
# d'extraction temporaire (_MEIxxxxxx), effacé à la fermeture du programme :
# écrire cache/logs/output/data là-dedans les ferait disparaître à chaque
# lancement. `sys.frozen` + `sys.executable` donnent à la place le dossier
# du .exe lui-même, condition nécessaire à la portabilité (déplacer le .exe
# doit déplacer toutes ses données avec lui).
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# Valeur par défaut pour le CLI (main.py) uniquement — l'interface graphique
# (gui.py) demande toujours explicitement le fichier gabarit à l'utilisateur
# et n'utilise jamais cette constante.
TEMPLATE_PATH = BASE_DIR.parent / "Entête walonmap (avec colonnes en plus).xlsx"
OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = BASE_DIR / "cache"
LOGS_DIR = BASE_DIR / "logs"
LIENS_COMMUNAUX_PATH = BASE_DIR / "data" / "liens_communaux.csv"

# ---------------------------------------------------------------------------
# Paramètres réseau
# ---------------------------------------------------------------------------

MAX_REQUESTS_PER_SECOND = 5.0
HTTP_TIMEOUT_SECONDS = 30
HTTP_MAX_ATTEMPTS = 5
DEBUG = False  # surchargé par l'option --debug de main.py

# ---------------------------------------------------------------------------
# Services ArcGIS REST (Géoportail de Wallonie, sauf mention contraire)
# ---------------------------------------------------------------------------

_ARCGIS_BASE = "https://geoservices.wallonie.be/arcgis/rest/services"

ARCGIS_SERVICES = {
    # Référentiels adresses / cadastre
    "ICAR_ADR_PT": f"{_ARCGIS_BASE}/DONNEES_BASE/ICAR_ADR_PT/MapServer",
    "CADMAP_PARCELLES": f"{_ARCGIS_BASE}/PLAN_REGLEMENT/CADMAP_PARCELLES/MapServer",
    # Tracé réel des rues (couche "Voirie - Axe", polylignes) — sert à
    # trouver les parcelles cadastrales bordant une rue mais sans adresse
    # ICAR (voir services/cadastre_service.py::_parcelles_cadastrales_sans_adresse).
    "PICC_VOIRIE": f"{_ARCGIS_BASE}/TOPOGRAPHIE/PICC_VDIFF/MapServer",
    # Plan de secteur, schémas, guides d'urbanisme
    "PDS": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/PDS/MapServer",
    "SDC": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/SDC/MapServer",
    "SOL": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/SOL/MapServer",
    "GRU": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/GRU/MapServer",
    "GCU": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/GCU/MapServer",
    # Eau / risques naturels
    "ALEA_INOND": f"{_ARCGIS_BASE}/EAU/ALEA_INOND/MapServer",
    "ERRUISSOL_RUISSELLEMENT": f"{_ARCGIS_BASE}/SOL_SOUS_SOL/ERRUISSOL__RUISSELLEMENT_DIFFUS/MapServer",
    "ERRUISSOL_EROSION": f"{_ARCGIS_BASE}/SOL_SOUS_SOL/ERRUISSOL__EROSION_DIFFUSE/MapServer",
    "LIDAXES": f"{_ARCGIS_BASE}/EAU/LIDAXES/MapServer",
    "PROTECT_CAPT": f"{_ARCGIS_BASE}/EAU/PROTECT_CAPT/MapServer",
    # PASH : service externe SPGE (pas sur geoservices.wallonie.be), validé explicitement
    "PASH": "https://sig.digiteaux.be/digiserver/rest/services/SPGE/WebPASH/MapServer",
    # Patrimoine
    "CAW": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/CAW/MapServer",
    "IPIC": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/IPIC/MapServer",
    "PAT_MND_UNESCO": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/PAT_MND_UNESCO/MapServer",
    "BC_PAT": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/BC_PAT/MapServer",
    "PAT_LSTSAV": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/PAT_LSTSAV/MapServer",
    "PAT_EXC": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/PAT_EXC/MapServer",
    # Urbanisme
    "LOT": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/LOT/MapServer",
    "PLANHP": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/PLANHP/MapServer",
    "RMBMT_URB": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/RMBMT_URB/MapServer",
    "SAR": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/SAR/MapServer",
    "ISA": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/ISA/MapServer",
    "SEVESO": f"{_ARCGIS_BASE}/INDUSTRIES_SERVICES/SEVESO/MapServer",
    "RENOV_URB": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/RENOV_URB/MapServer",
    "REVIT_URB": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/REVIT_URB/MapServer",
    "PRE": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/PRE/MapServer",
    "TNU_INZH": f"{_ARCGIS_BASE}/HABITAT/TNU_INZH/MapServer",
    "TERRILS": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/TERRILS/MapServer",
    "ZIP": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/ZIP/MapServer",
    "PPR": f"{_ARCGIS_BASE}/AMENAGEMENT_TERRITOIRE/PPR/MapServer",
    # Environnement
    "AHREM": f"{_ARCGIS_BASE}/FAUNE_FLORE/AHREM/MapServer",
    "NATURA2000": f"{_ARCGIS_BASE}/FAUNE_FLORE/NATURA2000/MapServer",
    "MASQUES_FORESTIERS": f"{_ARCGIS_BASE}/FORET/MASQUES_FORESTIERS/MapServer",
    "BDES_INVENTAIRE": f"{_ARCGIS_BASE}/SOL_SOUS_SOL/BDES_INVENTAIRE/MapServer",
}

# Service de géocodage ICAR (hors ArcGIS REST — API dédiée), utilisé
# uniquement en secours si un point d'adresse ICAR (couche ci-dessus) ne
# porte pas de géométrie exploitable. Voir services/cadastre_service.py.
ICAR_GEOCODE_URL = "https://geoservices.wallonie.be/geocodeWS/geocode"

# Liens fixes de catalogue (règle explicite du document Word métier, à
# utiliser tels quels pour les colonnes CU-CX plutôt qu'un lien dynamique
# de téléchargement — décision validée).
GRU_CATALOGUE_URL = "https://geoportail.wallonie.be/catalogue/4ed33135-c29a-4a92-abff-cfc69a24c350.html"
GCU_CATALOGUE_URL = "https://geoportail.wallonie.be/catalogue/e140607a-cfeb-445f-a551-22816c06c72f.html"


def _lr(service: str, layer_id: int, **kwargs) -> LayerRef:
    return LayerRef(service=service, layer_id=layer_id, **kwargs)


# ---------------------------------------------------------------------------
# Règles de remplissage — une entrée par colonne (G à CX).
#
# Les colonnes A-F (Pays, Code Postal, Communes, Rue, Numéro, Numéro
# cadastral) sont renseignées directement par services/cadastre_service.py
# à partir du registre ICAR et du plan cadastral, pas via ce moteur de
# règles génériques.
# ---------------------------------------------------------------------------

COLUMN_RULES: dict[str, ColumnRule] = {}


def _add(rule: ColumnRule) -> None:
    COLUMN_RULES[rule.column] = rule


# --- G-AF : zones d'affectation (plan de secteur, PDS layer 22, champ AFFECT) ---
_PDS_AFFECT = "PDS"
_PDS_AFFECT_LAYER = 22

_ZONES_AFFECTATION = [
    ("G", "Zone d'habitat", ["H01"], "En-tête source corrompu dans le gabarit "
     "(\"zone d'habita+D+G:AC\") — traité comme \"Zone d'habitat\", confirmé."),
    ("H", "Habitat à caractère rural", ["H02"], None),
    ("I", "Habitat vert", ["H03"], None),
    ("J", "Services publics et équipements communautaires", ["P01"], None),
    ("K", "Zone de loisirs", ["L01", "L11", "L12", "L13"], None),
    ("L", "Activité économique mixte", ["A01"], None),
    ("M", "Activité économique industrielle", ["A02"], None),
    ("N", "Activité économique spécifique agro-économique OU grande distribution", ["A11", "A12"], None),
    ("O", "Activité économique spécifique Risque majeur", ["A13"], None),
    ("P", "Zones de dépendances Extraction à destination agricole", ["XR1"], None),
    ("Q", "Zones de dépendances Extraction à destination forestière", ["XR2"], None),
    ("R", "Zones de dépendances Extraction à destination espaces verts", ["XR3"], None),
    ("S", "Zones de dépendances Extraction à destination zone naturelle", ["XR4"], None),
    ("T", "Zone d'aménagement communautaire concerté", ["D01"], None),
    ("U", "Zone d'enjeu communal", ["ZEC"], None),
    ("V", "Zone d'enjeu régional", ["ZER"], None),
    ("W", "Zone d'aménagement communautaire concerté à caractère économique", ["D02"], None),
    ("X", "Zone agricole", ["R01"], None),
    ("Y", "Zone forestière", ["R02"], None),
    ("Z", "Zone d'espaces verts", ["R03"], None),
    ("AA", "Zone naturelle", ["R04"], None),
    ("AB", "Zone de parc", ["R05"], None),
    ("AE", "Zone d'extraction", ["X01"], "Le code X01 est officiellement libellé "
     "\"Dépendances d'extraction\" dans le service ; c'est néanmoins le seul code "
     "de la famille \"extraction\" non déjà attribué (XR1-4 couvrent les colonnes "
     "P-S \"à destination\"), retenu par élimination pour cette colonne."),
    ("AF", "Zone non affectée", ["P12", "V01", "V02"], None),
]
for _col, _header, _codes, _note in _ZONES_AFFECTATION:
    _add(ColumnRule(
        column=_col, header=_header, rule_type=RuleType.LAYER_FIELD_MATCH,
        layer=_lr(_PDS_AFFECT, _PDS_AFFECT_LAYER, field="AFFECT", matching_codes=_codes),
        note=_note,
    ))

# AC, AD : aucune couche source identifiée (domaine AFFECT vérifié exhaustivement,
# 34 codes, aucun ne correspond) — décision validée : N par défaut, loggé.
_add(ColumnRule(
    column="AC", header="Zone de recul", rule_type=RuleType.NO_SOURCE, default_value="N",
    note="Aucun code du champ AFFECT (PDS) ne correspond à cette notion (vérifié "
         "exhaustivement sur les 34 valeurs). Décision validée avec l'utilisateur.",
))
_add(ColumnRule(
    column="AD", header="Zone d'accès et de stationnement", rule_type=RuleType.NO_SOURCE, default_value="N",
    note="Idem AC : aucune couche source identifiée. Décision validée avec l'utilisateur.",
))

# --- AG-AM : périmètres de protection (PDS, groupe layer 14, une sous-couche par colonne) ---
_PERIMETRES_PROTECTION = [
    ("AG", "Points de vue remarquable", 15),
    ("AH", "Périmètres de points de vue remarquable", 16),
    ("AI", "Intérêt paysager", 17),
    ("AJ", "Intérêt culturel, historique ou esthétique", 18),
    ("AK", "Liaisons écologiques", 19),
    ("AL", "Réservation d'infrastructure principale", 20),
    ("AM", "Extension de zone d'extraction", 21),
]
for _col, _header, _layer_id in _PERIMETRES_PROTECTION:
    _add(ColumnRule(
        column=_col, header=_header, rule_type=RuleType.LAYER_PRESENCE,
        layer=_lr(_PDS_AFFECT, _layer_id),
    ))

# --- AN : toujours "/" (décision validée) ---
_add(ColumnRule(column="AN", header="Aire d'habitat", rule_type=RuleType.FIXED_VALUE, fixed_value="/"))

# --- AO-AQ : schémas ---
_add(ColumnRule(
    column="AO", header="Schéma de développement communal", rule_type=RuleType.LAYER_PRESENCE,
    layer=_lr("SDC", 0),
))
_add(ColumnRule(
    column="AP", header="Lien Schéma de développement communal", rule_type=RuleType.CONDITIONAL_LOOKUP_LINK,
    trigger_column="AO", trigger_value="O", lookup_field="lien_schema_developpement_communal",
))
_add(ColumnRule(
    column="AQ", header="Schéma d'orientation locale", rule_type=RuleType.LAYER_PRESENCE,
    layer=_lr("SOL", 0),
))

# --- AR-AU : guides d'urbanisme ---
_add(ColumnRule(
    column="AR", header="Guide régional d'urbanisme — Zones Protégées en matière d'Urbanisme",
    rule_type=RuleType.LAYER_PRESENCE, layer=_lr("GRU", 5),
))
_add(ColumnRule(
    column="AS", header="Guide régional d'urbanisme — Règlement Général sur les Bâtisses en Site Rural",
    rule_type=RuleType.LAYER_PRESENCE, layer=_lr("GRU", 4),
))
_add(ColumnRule(
    column="AT", header="Guide régional d'urbanisme — Qualité acoustique des constructions",
    rule_type=RuleType.LAYER_PRESENCE, layer=_lr("GRU", 3),
))
_add(ColumnRule(
    column="AU", header="Guide communal d'urbanisme", rule_type=RuleType.LAYER_PRESENCE,
    layer=_lr("GCU", 0),
))

# --- AV-AY : aléa d'inondation (couche RASTER, 4 classes, identify) ---
_ALEA_NOTE = ("Incohérence du document Word résolue avec l'utilisateur : la couche "
              "vectorielle (échelle 1:25.000-1:5.000) n'a que 3 classes (faible/moyen/"
              "élevé). La couche RASTER 1 (visible en dézoomant, comme l'indique le "
              "document Word) porte les 4 classes officielles, dont \"très faible\".")
_ALEA_CLASSES = [
    ("AV", "Aléa très faible", "Aléa très faible"),
    ("AW", "Aléa faible", "Aléa faible"),
    ("AX", "Aléa moyen", "Aléa moyen"),
    ("AY", "Aléa élevé", "Aléa élevé"),
]
for _col, _header, _label in _ALEA_CLASSES:
    _add(ColumnRule(
        column=_col, header=_header, rule_type=RuleType.RASTER_IDENTIFY_MATCH,
        layer=_lr("ALEA_INOND", 1, matching_codes=[_label]),
        note=_ALEA_NOTE,
    ))

# --- AZ, BA : ruissellement ---
_add(ColumnRule(
    column="AZ", header="Risque de ruissellement diffus (ERRUISOL)", rule_type=RuleType.RASTER_PRESENCE,
    layer=_lr("ERRUISSOL_RUISSELLEMENT", 0),
))
_add(ColumnRule(
    column="BA", header="Axe de concentration de ruissellement (LIDAXES)", rule_type=RuleType.LAYER_PRESENCE,
    layer=_lr("LIDAXES", 1),  # layer 1 = version vectorielle ; layer 0 est le groupe parent
))

# --- BB-BF : protection des captages ---
_add(ColumnRule(column="BB", header="Zone de surveillance", rule_type=RuleType.LAYER_PRESENCE,
                 layer=_lr("PROTECT_CAPT", 0)))
_add(ColumnRule(column="BC", header="Zone de prévention rapprochée IIa", rule_type=RuleType.LAYER_PRESENCE,
                 layer=_lr("PROTECT_CAPT", 2)))
_add(ColumnRule(column="BD", header="Zone de prévention éloignée IIb", rule_type=RuleType.LAYER_PRESENCE,
                 layer=_lr("PROTECT_CAPT", 3)))
_add(ColumnRule(column="BE", header="Zones de prévention forfaitaires rapprochée IIa",
                 rule_type=RuleType.LAYER_FIELD_MATCH,
                 layer=_lr("PROTECT_CAPT", 4, field="TYPE_CODE", matching_codes=["IIa"])))
_add(ColumnRule(column="BF", header="Zones de prévention forfaitaires éloignée IIb",
                 rule_type=RuleType.LAYER_FIELD_MATCH,
                 layer=_lr("PROTECT_CAPT", 4, field="TYPE_CODE", matching_codes=["IIb"])))

# --- BG-BJ : PASH — régime d'assainissement (service externe SPGE) ---
_PASH_LAYER_ID = 6
_add(ColumnRule(
    column="BG", header="Collectif", rule_type=RuleType.LAYER_DOUBLE_FIELD_MATCH,
    layer=_lr("PASH", _PASH_LAYER_ID, field="pashcode", matching_codes=["I"],
              extra_field="pdsaffectcode", extra_codes=["HZU"], extra_negate=True),
))
_add(ColumnRule(
    column="BH", header="Collectif hors zone urbanisable", rule_type=RuleType.LAYER_DOUBLE_FIELD_MATCH,
    layer=_lr("PASH", _PASH_LAYER_ID, field="pashcode", matching_codes=["I"],
              extra_field="pdsaffectcode", extra_codes=["HZU"], extra_negate=False),
    note="Croisement pashcode='I' ET pdsaffectcode='HZU', décision validée avec l'utilisateur.",
))
_add(ColumnRule(
    column="BI", header="Autonome", rule_type=RuleType.LAYER_FIELD_MATCH,
    layer=_lr("PASH", _PASH_LAYER_ID, field="pashcode", matching_codes=["II"]),
))
_add(ColumnRule(
    column="BJ", header="Transitoire", rule_type=RuleType.LAYER_FIELD_MATCH,
    layer=_lr("PASH", _PASH_LAYER_ID, field="pashcode", matching_codes=["III"]),
))

# --- BK-BP : patrimoine ---
_add(ColumnRule(column="BK", header="Zone archéologique (carte archéologique)",
                 rule_type=RuleType.LAYER_PRESENCE, layer=_lr("CAW", 0)))
_add(ColumnRule(column="BL", header="Patrimoine immobilier culturel (inventaire régional)",
                 rule_type=RuleType.LAYER_PRESENCE, layer=_lr("IPIC", 0)))
_add(ColumnRule(column="BM", header="Biens mondiaux (Unesco)",
                 rule_type=RuleType.LAYER_PRESENCE, layer=_lr("PAT_MND_UNESCO", 0)))
_add(ColumnRule(
    column="BN", header="Bien classé et zone de protection", rule_type=RuleType.LAYER_PRESENCE_ANY,
    layers=[_lr("BC_PAT", i) for i in range(5)],  # 0-3 biens classés + 4 zone de protection (header l'exige)
))
_add(ColumnRule(
    column="BO", header="Bien en liste de sauvegarde", rule_type=RuleType.LAYER_PRESENCE_ANY,
    layers=[_lr("PAT_LSTSAV", i) for i in range(4)],  # 0-3 seulement, en-tête ne mentionne pas "zone de protection"
))
_add(ColumnRule(
    column="BP", header="Biens exceptionnels", rule_type=RuleType.LAYER_PRESENCE_ANY,
    layers=[_lr("PAT_EXC", i) for i in range(4)],  # idem BO
))

# --- BQ-CG : urbanisme ---
_add(ColumnRule(column="BQ", header="Permis d'urbanisation", rule_type=RuleType.LAYER_PRESENCE,
                 layer=_lr("LOT", 0)))
# BR, BS, BV, BW, BX, BZ, CA : résolues via `identify` (VECTOR_IDENTIFY_*)
# plutôt que `/query` (LAYER_PRESENCE/LAYER_FIELD_MATCH habituels) — ces
# couches (PLANHP, RMBMT_URB, RENOV_URB, REVIT_URB, PRE, TERRILS) se sont
# montrées sujettes en conditions réelles à un incident intermittent côté
# serveur Wallonie sur `/query` ("not a Geographic Coordinate System",
# apparaissant et disparaissant sans changement de requête), alors
# qu'`identify` sur ces mêmes couches, à ce moment précis, restait fiable.
# Décision validée avec l'utilisateur (2026-08-01).
_add(ColumnRule(column="BR", header="Plan Habitat permanent", rule_type=RuleType.VECTOR_IDENTIFY_PRESENCE,
                 layer=_lr("PLANHP", 0)))
_add(ColumnRule(column="BS", header="Périmètre de remembrement urbain", rule_type=RuleType.VECTOR_IDENTIFY_PRESENCE,
                 layer=_lr("RMBMT_URB", 0)))
_add(ColumnRule(column="BT", header="Site à réaménager de droit", rule_type=RuleType.LAYER_PRESENCE,
                 layer=_lr("SAR", 0)))
_add(ColumnRule(column="BU", header="Sites SEVESO", rule_type=RuleType.LAYER_PRESENCE,
                 layer=_lr("SEVESO", 1)))
_add(ColumnRule(column="BV", header="Rénovation urbaine", rule_type=RuleType.VECTOR_IDENTIFY_PRESENCE,
                 layer=_lr("RENOV_URB", 0)))
_add(ColumnRule(column="BW", header="Revitalisation urbaine", rule_type=RuleType.VECTOR_IDENTIFY_PRESENCE,
                 layer=_lr("REVIT_URB", 0)))
_add(ColumnRule(
    column="BX", header="Périmètre de reconnaissance économique", rule_type=RuleType.VECTOR_IDENTIFY_FIELD_MATCH,
    layer=_lr("PRE", 0, field="NATURE", matching_codes=["PRE"]),
    note="Le service PRE combine 3 notions dans le même champ NATURE (PRE/DRPRE/PEX) ; filtré sur PRE uniquement.",
))
_add(ColumnRule(column="BY", header="Terrains non urbanisés en zones destinées au plan de secteur",
                 rule_type=RuleType.LAYER_PRESENCE, layer=_lr("TNU_INZH", 0)))
_add(ColumnRule(
    column="BZ", header="Terrils du point de vue aménagement et urbanisme — Non Majeur",
    rule_type=RuleType.VECTOR_IDENTIFY_FIELD_MATCH,
    layer=_lr("TERRILS", 0, field="MAJEUR", matching_codes=["Non"]),
))
_add(ColumnRule(
    column="CA", header="Terrils du point de vue aménagement et urbanisme — Majeur",
    rule_type=RuleType.VECTOR_IDENTIFY_FIELD_MATCH,
    layer=_lr("TERRILS", 0, field="MAJEUR", matching_codes=["Oui"]),
))
_SAR_INVENTAIRE = [
    ("CB", "Site à réaménager (Périmètres des sites d'activité)", 0),
    ("CC", "Site à réaménager (Activités antérieures et actuelles)", 1),
    ("CD", "Site à réaménager (Inventaire) - Potentiel de reconversion", 2),
    ("CE", "Site à réaménager (Inventaire) - Bâtiments", 3),
    ("CF", "Site à réaménager (Inventaire) - Site à Réaménager (sar)", 4),
]
for _col, _header, _layer_id in _SAR_INVENTAIRE:
    _add(ColumnRule(column=_col, header=_header, rule_type=RuleType.LAYER_PRESENCE,
                     layer=_lr("ISA", _layer_id)))
_add(ColumnRule(column="CG", header="Zones d'initiative privilégiée", rule_type=RuleType.VECTOR_IDENTIFY_PRESENCE,
                 layer=_lr("ZIP", 0)))

# --- CH-CK : environnement ---
_add(ColumnRule(
    column="CH", header="Risques d'érosion hydrique diffuse", rule_type=RuleType.RASTER_PRESENCE_ANY,
    layers=[_lr("ERRUISSOL_EROSION", i) for i in range(3)],  # prairie / non sarclée / sarclée combinés
    note="Combine les 3 scénarios d'occupation du sol disponibles (décision validée avec l'utilisateur).",
))
_add(ColumnRule(column="CI", header="Arbres et haies remarquables", rule_type=RuleType.LAYER_PRESENCE_ANY,
                 layers=[_lr("AHREM", i) for i in range(4)]))
_add(ColumnRule(column="CJ", header="Site Natura 2000", rule_type=RuleType.LAYER_PRESENCE_ANY,
                 layers=[_lr("NATURA2000", 9), _lr("NATURA2000", 10)]))
_add(ColumnRule(column="CK", header="Forêt (masque forestier)", rule_type=RuleType.RASTER_PRESENCE,
                 layer=_lr("MASQUES_FORESTIERS", 0)))

# --- CL : lien contrat de rénovation urbaine (si BV = O) ---
_add(ColumnRule(
    column="CL", header="Contrats de rénovation urbaine", rule_type=RuleType.CONDITIONAL_LOOKUP_LINK,
    trigger_column="BV", trigger_value="O", lookup_field="lien_contrat_renovation_urbaine",
))

# --- CM : zone de préemption ---
# PPR fait partie du même groupe de services touchés par l'incident
# intermittent décrit plus haut (BR, BS, BV, BW, BX, BZ, CA) — voir cette
# note.
_add(ColumnRule(column="CM", header="Zone de préemption", rule_type=RuleType.VECTOR_IDENTIFY_PRESENCE,
                 layer=_lr("PPR", 0)))

# --- CN, CO : BDES ---
_add(ColumnRule(column="CN", header="Parcelle nécessitant des démarches (BDES)",
                 rule_type=RuleType.LAYER_PRESENCE, layer=_lr("BDES_INVENTAIRE", 0)))
_add(ColumnRule(column="CO", header="Parcelle de nature indicative (BDES)",
                 rule_type=RuleType.LAYER_PRESENCE, layer=_lr("BDES_INVENTAIRE", 1)))

# --- CP-CT : toujours "/" (règle explicite du document Word) ---
for _col, _header in [
    ("CP", "Quartier à loyers majorés"),
    ("CQ", "Territoire du Canal"),
    ("CR", "Pôle de développement prioritaire"),
    ("CS", "Zone de revitalisation urbaine"),
    ("CT", "EDRLR"),
]:
    _add(ColumnRule(column=_col, header=_header, rule_type=RuleType.FIXED_VALUE, fixed_value="/"))

# --- CU-CX : liens conditionnels GRU / GCU (URL fixe de catalogue, règle Word doc) ---
_add(ColumnRule(column="CU", header="Lien Guide régional d'urbanisme (ZPU)",
                 rule_type=RuleType.CONDITIONAL_FIXED_LINK,
                 trigger_column="AR", link_url=GRU_CATALOGUE_URL))
_add(ColumnRule(column="CV", header="Lien Règlement général sur les bâtisses en site rural",
                 rule_type=RuleType.CONDITIONAL_FIXED_LINK,
                 trigger_column="AS", link_url=GRU_CATALOGUE_URL))
_add(ColumnRule(column="CW", header="Lien Règlement d'urbanisme qualité acoustique aéroports",
                 rule_type=RuleType.CONDITIONAL_FIXED_LINK,
                 trigger_column="AT", link_url=GRU_CATALOGUE_URL))
_add(ColumnRule(column="CX", header="Lien Guide communal d'urbanisme",
                 rule_type=RuleType.CONDITIONAL_FIXED_LINK,
                 trigger_column="AU", link_url=GCU_CATALOGUE_URL))
