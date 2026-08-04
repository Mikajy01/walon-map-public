"""Définitions déclaratives des règles de remplissage des colonnes Excel.

Chaque colonne du fichier de sortie est décrite par un `ColumnRule` unique.
Le moteur générique (`services.layers_service.LayersService`) interprète ces
règles pour ne jamais dupliquer la logique d'appel aux services ArcGIS REST :
une seule stratégie d'exécution existe par `RuleType`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence


class RuleType(Enum):
    """Stratégie de résolution d'une colonne."""

    # Intersection géométrique simple avec une couche : "O" si au moins un
    # élément intersecte la parcelle, sinon "N".
    LAYER_PRESENCE = "layer_presence"

    # Comme LAYER_PRESENCE, mais avec plusieurs (sous-)couches à combiner en
    # OU logique : "O" dès que l'une des couches intersecte la parcelle.
    # Utilisé quand une colonne Excel regroupe plusieurs sous-couches du
    # Géoportail (ex: "Bien classé et zone de protection").
    LAYER_PRESENCE_ANY = "layer_presence_any"

    # Intersection géométrique avec une couche, puis comparaison de la
    # valeur d'un champ attributaire à une liste de codes attendus pour
    # cette colonne (ex: champ AFFECT du plan de secteur).
    LAYER_FIELD_MATCH = "layer_field_match"

    # Valeur constante, sans aucun appel réseau (ex: colonnes "à compléter
    # par /" imposées par les règles métier).
    FIXED_VALUE = "fixed_value"

    # Aucune couche source identifiée lors de l'analyse : valeur par défaut
    # appliquée avec un avertissement journalisé à chaque utilisation.
    NO_SOURCE = "no_source"

    # Lien conditionnel : si la colonne déclencheuse vaut "O", la cellule
    # reçoit une URL fixe (catalogue Géoportail) ; sinon "/".
    CONDITIONAL_FIXED_LINK = "conditional_fixed_link"

    # Lien conditionnel : si la colonne déclencheuse vaut "O", la cellule
    # est renseignée par une table de correspondance communale maintenue
    # manuellement (data/liens_communaux.csv) ; sinon "/".
    CONDITIONAL_LOOKUP_LINK = "conditional_lookup_link"

    # Couche RASTER classifiée (ex: aléa d'inondation à petite échelle),
    # interrogée via l'opération ArcGIS `identify` (pas `query`). "O" si le
    # libellé de classe renvoyé correspond à `layer.matching_codes`.
    RASTER_IDENTIFY_MATCH = "raster_identify_match"

    # Couche RASTER binaire (masque de présence/absence, ex: masque
    # forestier) : "O" dès que l'`identify` renvoie une valeur (≠ NoData).
    RASTER_PRESENCE = "raster_presence"

    # Comme RASTER_PRESENCE mais combinant plusieurs couches raster en OU
    # logique (ex: risque d'érosion hydrique diffuse, disponible en 3
    # scénarios d'occupation du sol à combiner).
    RASTER_PRESENCE_ANY = "raster_presence_any"

    # Intersection géométrique avec une couche, puis comparaison de DEUX
    # champs attributaires (ex: régime PASH + affectation au plan de
    # secteur pour distinguer "Collectif" de "Collectif hors zone
    # urbanisable"). "O" si un même élément satisfait les deux conditions.
    LAYER_DOUBLE_FIELD_MATCH = "layer_double_field_match"

    # Comme LAYER_PRESENCE, mais via l'opération ArcGIS `identify`
    # (polygone + sommets échantillonnés) au lieu de `/query`. Réservé aux
    # couches VECTORIELLES dont `/query` s'est révélé, en conditions
    # réelles, sujet à un incident intermittent côté serveur Wallonie
    # (AMENAGEMENT_TERRITOIRE/REVIT_URB, RENOV_URB, PLANHP, RMBMT_URB, ZIP —
    # erreur "not a Geographic Coordinate System" apparaissant et
    # disparaissant sans changement de requête ; `/identify` sur ces mêmes
    # couches s'est montré fiable pendant l'incident). "O" dès que l'un des
    # échantillons renvoie un résultat.
    VECTOR_IDENTIFY_PRESENCE = "vector_identify_presence"

    # Comme LAYER_FIELD_MATCH, mais via `identify` au lieu de `/query` —
    # même raison que VECTOR_IDENTIFY_PRESENCE (AMENAGEMENT_TERRITOIRE/PRE,
    # TERRILS).
    VECTOR_IDENTIFY_FIELD_MATCH = "vector_identify_field_match"


@dataclass(frozen=True)
class LayerRef:
    """Référence à une (sous-)couche ArcGIS REST.

    `service` est la clé pointant vers une entrée de
    `config.ARCGIS_SERVICES` (jamais une URL codée en dur dans les règles).
    """

    service: str
    layer_id: int
    field: Optional[str] = None
    matching_codes: Optional[Sequence[str]] = None
    # Condition supplémentaire sur un second champ du même élément (utilisé
    # uniquement par RuleType.LAYER_DOUBLE_FIELD_MATCH). `extra_negate=True`
    # signifie "la valeur du champ n'est PAS dans extra_codes".
    extra_field: Optional[str] = None
    extra_codes: Optional[Sequence[str]] = None
    extra_negate: bool = False


@dataclass(frozen=True)
class ColumnRule:
    """Règle de remplissage d'une colonne du fichier Excel."""

    column: str
    header: str
    rule_type: RuleType
    layer: Optional[LayerRef] = None
    layers: Optional[Sequence[LayerRef]] = None  # RuleType.LAYER_PRESENCE_ANY
    fixed_value: Optional[str] = None
    default_value: Optional[str] = None
    trigger_column: Optional[str] = None
    trigger_value: str = "O"
    link_url: Optional[str] = None
    lookup_field: Optional[str] = None
    note: Optional[str] = None
