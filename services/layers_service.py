"""Moteur générique de résolution des colonnes du fichier de sortie.

Une seule fonction de résolution existe par `RuleType` (voir models.rules).
Ajouter ou corriger une colonne ne nécessite jamais de nouveau code, mais
uniquement une nouvelle entrée dans `config.COLUMN_RULES` — ce qui respecte
l'exigence du cahier des charges de ne jamais coder en dur les couches/URLs
à plusieurs endroits.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict

import config
from models.parcelle import Parcelle
from models.rules import ColumnRule, LayerRef, RuleType
from services.geoportail_service import ArcGISRestClient
from utils.logger import get_logger

_logger = get_logger("services.layers_service")

# Ordre de résolution : les règles conditionnelles dépendent de colonnes
# déjà résolues (trigger_column), elles sont donc toujours résolues en
# dernier.
_CONDITIONAL_TYPES = (RuleType.CONDITIONAL_FIXED_LINK, RuleType.CONDITIONAL_LOOKUP_LINK)


class LayersService:
    def __init__(self, client: ArcGISRestClient, liens_communaux_path: Path) -> None:
        self._client = client
        self._liens_communaux = self._load_liens_communaux(liens_communaux_path)

    @staticmethod
    def _load_liens_communaux(path: Path) -> Dict[str, Dict[str, str]]:
        if not path.exists():
            _logger.warning("Table de correspondance communale introuvable : %s", path)
            return {}
        with path.open(encoding="utf-8", newline="") as f:
            return {row["commune"].strip().lower(): row for row in csv.DictReader(f) if row.get("commune")}

    def resolve_all(self, parcelle: Parcelle) -> None:
        """Calcule et stocke dans `parcelle.valeurs` la valeur de chaque colonne."""
        ordered_rules = sorted(
            config.COLUMN_RULES.values(),
            key=lambda r: 1 if r.rule_type in _CONDITIONAL_TYPES else 0,
        )
        for rule in ordered_rules:
            try:
                valeur = self._resolve_one(rule, parcelle)
            except Exception:  # noqa: BLE001 - une couche en erreur ne doit jamais arrêter le traitement
                _logger.exception(
                    "Échec de résolution de la colonne %s (%s) pour la parcelle %s — "
                    "valeur 'ERREUR' appliquée, traitement poursuivi.",
                    rule.column, rule.header, parcelle.identifiant,
                )
                valeur = "ERREUR"
            parcelle.valeurs[rule.column] = valeur
            _logger.debug(
                "Parcelle %s | colonne %s (%s) = %r [règle=%s]",
                parcelle.identifiant, rule.column, rule.header, valeur, rule.rule_type.value,
            )

    def refresh_lookup_links(self, parcelle: Parcelle) -> bool:
        """Recalcule uniquement les colonnes CONDITIONAL_LOOKUP_LINK (AP,
        CL) d'une parcelle DÉJÀ résolue lors d'une exécution précédente
        (voir main.py, reprise incrémentale). Renvoie True si une valeur a
        changé (à re-persister dans ProgressStore par l'appelant).

        Toutes les autres colonnes d'une parcelle déjà traitée sont figées
        à raison — elles dépendent de données géographiques stables. Mais
        AP/CL dépendent de `data/liens_communaux.csv`, un fichier que
        l'utilisateur peut modifier À TOUT MOMENT, y compris après qu'une
        parcelle ait déjà été traitée avec la valeur "À COMPLÉTER
        MANUELLEMENT". Sans ce rafraîchissement, un lien ajouté après coup
        au CSV ne serait jamais repris pour les parcelles déjà résolues.
        Aucun appel réseau ici (juste une relecture du CSV, déjà en
        mémoire), donc sans coût à refaire à chaque exécution.
        """
        changed = False
        for rule in config.COLUMN_RULES.values():
            if rule.rule_type is not RuleType.CONDITIONAL_LOOKUP_LINK:
                continue
            nouvelle_valeur = self._resolve_conditional_lookup_link(rule, parcelle)
            if parcelle.valeurs.get(rule.column) != nouvelle_valeur:
                _logger.info(
                    "Parcelle %s | colonne %s : valeur rafraîchie depuis data/liens_communaux.csv "
                    "(%r -> %r).", parcelle.identifiant, rule.column,
                    parcelle.valeurs.get(rule.column), nouvelle_valeur,
                )
                parcelle.valeurs[rule.column] = nouvelle_valeur
                changed = True
        return changed

    def retry_erreurs(self, parcelle: Parcelle) -> int:
        """Retente la résolution de chaque colonne actuellement à
        `"ERREUR"` OU vide (absente de `parcelle.valeurs`) pour une
        parcelle DÉJÀ résolue lors d'une exécution précédente (voir
        main.py, reprise incrémentale).

        Contrairement aux autres colonnes d'une parcelle déjà traitée
        (figées à raison : données géographiques stables, aucune raison de
        refaire l'appel réseau), une valeur `"ERREUR"` ne reflète pas une
        donnée réelle mais un échec de résolution. Observé en pratique sur
        la commune Dour (2026-07-30) : plusieurs colonnes ont échoué en
        bloc pendant quelques minutes (incident transitoire côté serveur
        Wallonie) puis auraient réussi normalement dès l'exécution
        suivante si elles avaient été retentées — geler `"ERREUR"` pour
        toujours transforme donc un incident de quelques minutes en une
        correction manuelle permanente. On retente donc systématiquement,
        à chaque exécution, uniquement les colonnes concernées (jamais
        toute la parcelle : ne jamais re-solliciter les couches déjà
        résolues avec succès). Un échec qui se reproduit reste visible
        dans les logs à chaque tentative (aucun masquage d'un vrai bug de
        code), simplement sans jamais bloquer définitivement.

        Le cas vide couvre une cellule jamais écrite du tout lors d'une
        exécution antérieure — cas réel constaté sur Comines-Warneton :
        des centaines de lignes déjà traitées avaient une colonne (K pour
        un lot, Y pour un autre, jamais les deux à la fois) totalement
        absente de `valeurs`, pas à `"ERREUR"` — un bug de résolution deux
        fois plus ancien que ce garde-fou, pour une seule colonne à la
        fois, jamais rejoué depuis puisque `"ERREUR"` seul était retenté.
        Aucune règle du moteur ne produit jamais légitimement une chaîne
        vide (toujours O/N/ERREUR/une valeur fixe comme "/"), donc sans
        risque de considérer une cellule vide comme un échec à retenter.

        Nécessite que `parcelle.geometry` soit déjà renseignée (voir
        `CadastreService.rattacher_parcelle_cadastrale`, à appeler par
        l'appelant avant celle-ci pour les parcelles concernées).

        Renvoie le nombre de cellules effectivement corrigées (0 si
        aucune, à re-persister par l'appelant sinon)."""
        ordered_rules = sorted(
            config.COLUMN_RULES.values(),
            key=lambda r: 1 if r.rule_type in _CONDITIONAL_TYPES else 0,
        )
        cellules_corrigees = 0
        for rule in ordered_rules:
            valeur_actuelle = parcelle.valeurs.get(rule.column)
            if valeur_actuelle != "ERREUR" and valeur_actuelle:
                continue
            try:
                nouvelle_valeur = self._resolve_one(rule, parcelle)
            except Exception:  # noqa: BLE001 - un échec répété ne doit jamais arrêter le traitement
                _logger.exception(
                    "Nouvelle tentative échouée pour la colonne %s (%s) de la parcelle "
                    "%s — valeur %r conservée.", rule.column, rule.header, parcelle.identifiant, valeur_actuelle,
                )
                continue
            if nouvelle_valeur != "ERREUR":
                _logger.info(
                    "Parcelle %s | colonne %s (%s) : %s résolue au nouvel essai -> %r.",
                    parcelle.identifiant, rule.column, rule.header,
                    "cellule vide" if not valeur_actuelle else "erreur", nouvelle_valeur,
                )
                parcelle.valeurs[rule.column] = nouvelle_valeur
                cellules_corrigees += 1
        return cellules_corrigees

    def _resolve_one(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        resolver = {
            RuleType.FIXED_VALUE: self._resolve_fixed_value,
            RuleType.NO_SOURCE: self._resolve_no_source,
            RuleType.LAYER_PRESENCE: self._resolve_layer_presence,
            RuleType.LAYER_PRESENCE_ANY: self._resolve_layer_presence_any,
            RuleType.LAYER_FIELD_MATCH: self._resolve_layer_field_match,
            RuleType.LAYER_DOUBLE_FIELD_MATCH: self._resolve_layer_double_field_match,
            RuleType.RASTER_IDENTIFY_MATCH: self._resolve_raster_identify_match,
            RuleType.RASTER_PRESENCE: self._resolve_raster_presence,
            RuleType.RASTER_PRESENCE_ANY: self._resolve_raster_presence_any,
            RuleType.CONDITIONAL_FIXED_LINK: self._resolve_conditional_fixed_link,
            RuleType.CONDITIONAL_LOOKUP_LINK: self._resolve_conditional_lookup_link,
            RuleType.VECTOR_IDENTIFY_PRESENCE: self._resolve_vector_identify_presence,
            RuleType.VECTOR_IDENTIFY_FIELD_MATCH: self._resolve_vector_identify_field_match,
        }.get(rule.rule_type)
        if resolver is None:
            raise NotImplementedError(f"Type de règle non géré : {rule.rule_type}")
        return resolver(rule, parcelle)

    # -- Règles sans appel réseau -----------------------------------------

    def _resolve_fixed_value(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        return rule.fixed_value if rule.fixed_value is not None else ""

    def _resolve_no_source(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        _logger.warning(
            "Colonne %s (%s) : aucune couche source identifiée, valeur par défaut %r appliquée. %s",
            rule.column, rule.header, rule.default_value, rule.note or "",
        )
        return rule.default_value if rule.default_value is not None else "N"

    # -- Règles géométriques -----------------------------------------------

    def _service_url(self, service_key: str) -> str:
        try:
            return config.ARCGIS_SERVICES[service_key]
        except KeyError as exc:
            raise KeyError(f"Service '{service_key}' introuvable dans config.ARCGIS_SERVICES") from exc

    def _require_geometry(self, rule: ColumnRule, parcelle: Parcelle) -> bool:
        if parcelle.geometry is None:
            _logger.warning(
                "Parcelle %s sans géométrie : colonne %s = N par défaut", parcelle.identifiant, rule.column
            )
            return False
        return True

    def _intersects(self, layer: LayerRef, parcelle: Parcelle, out_fields: str = "OBJECTID") -> list:
        return self._client.query_layer(
            service_url=self._service_url(layer.service),
            layer_id=layer.layer_id,
            geometry=parcelle.geometry,
            out_fields=out_fields,
        )

    def _resolve_layer_presence(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        assert rule.layer is not None
        features = self._intersects(rule.layer, parcelle)
        return "O" if features else "N"

    def _resolve_layer_presence_any(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        assert rule.layers
        for layer in rule.layers:
            if self._intersects(layer, parcelle):
                return "O"
        return "N"

    def _resolve_layer_field_match(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        layer = rule.layer
        assert layer is not None and layer.field and layer.matching_codes
        features = self._intersects(layer, parcelle, out_fields=layer.field)
        codes = {str(f["attributes"].get(layer.field)) for f in features}
        return "O" if codes & set(layer.matching_codes) else "N"

    def _resolve_layer_double_field_match(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        layer = rule.layer
        assert layer is not None and layer.field and layer.matching_codes and layer.extra_field
        out_fields = f"{layer.field},{layer.extra_field}"
        features = self._intersects(layer, parcelle, out_fields=out_fields)
        for feature in features:
            attrs = feature.get("attributes", {})
            primary_ok = str(attrs.get(layer.field)) in set(layer.matching_codes)
            if not primary_ok:
                continue
            extra_value = str(attrs.get(layer.extra_field))
            extra_in_codes = extra_value in set(layer.extra_codes or [])
            extra_ok = (not extra_in_codes) if layer.extra_negate else extra_in_codes
            if extra_ok:
                return "O"
        return "N"

    def _resolve_vector_identify_presence(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        assert rule.layer is not None
        return "O" if self._identify_all_results(rule.layer, parcelle) else "N"

    def _resolve_vector_identify_field_match(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        layer = rule.layer
        assert layer is not None and layer.field and layer.matching_codes
        results = self._identify_all_results(layer, parcelle)
        codes = {str(r.get("attributes", {}).get(layer.field)) for r in results}
        return "O" if codes & set(layer.matching_codes) else "N"

    # Nombre maximal de sommets échantillonnés individuellement pour une
    # grande parcelle (voir `_identify_all_results`) — au-delà, sommets
    # répartis régulièrement plutôt que tous, pour borner le coût réseau.
    _MAX_RASTER_VERTEX_SAMPLES = 20

    def _resolve_raster_identify_match(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        layer = rule.layer
        assert layer is not None and layer.matching_codes
        results = self._identify_all_results(layer, parcelle)
        # "UniqueValue.Pixel Value" ne porte qu'un code numérique brut (ex: "130") ;
        # le libellé humain ("Aléa très faible", etc.) est dans "Raster.VALEUR" —
        # vérifié sur une vraie réponse identify (voir historique de conversation).
        labels = {
            r.get("attributes", {}).get("Raster.VALEUR", "")
            for r in results
        }
        return "O" if labels & set(layer.matching_codes) else "N"

    def _identify_has_value(self, layer: LayerRef, parcelle: Parcelle) -> bool:
        results = self._identify_all_results(layer, parcelle)
        return any(
            r.get("attributes", {}).get("UniqueValue.Pixel Value", "NoData") != "NoData"
            for r in results
        )

    def _identify_all_results(self, layer: LayerRef, parcelle: Parcelle) -> list:
        """Interroge une couche (raster ou vectorielle, voir `ArcGISRestClient.identify`)
        de façon robuste aux petits recouvrements, via `identify` plutôt que `/query`.

        `identify` sur un GRAND polygone renvoie la classe/l'élément DOMINANT
        de la zone interrogée, pas nécessairement une petite classe qui ne
        touche qu'un coin de la parcelle — confirmé par un test dédié (zone
        "Aléa élevé" connue ne touchant qu'un coin d'un polygone 200x200m :
        la classe renvoyée était "Aléa faible", la classe dominante). Pour ne
        jamais manquer un recouvrement même minime, on interroge donc
        systématiquement : (1) le polygone entier (capte l'élément
        dominant), ET (2) chacun de ses sommets individuellement comme
        point (capte tout élément touchant la limite de la parcelle, même
        sur une infime partie) — puis on combine tous les résultats.
        """
        geometry = parcelle.geometry
        service_url = self._service_url(layer.service)
        all_results = list(
            self._client.identify(
                service_url=service_url, layer_id=layer.layer_id, geometry=geometry,
            )
        )

        rings = geometry.get("rings") if isinstance(geometry, dict) else None
        if not rings:
            return all_results  # géométrie ponctuelle : rien de plus à échantillonner

        vertices = rings[0]
        if len(vertices) > self._MAX_RASTER_VERTEX_SAMPLES:
            step = len(vertices) / self._MAX_RASTER_VERTEX_SAMPLES
            vertices = [vertices[int(i * step)] for i in range(self._MAX_RASTER_VERTEX_SAMPLES)]

        for x, y in vertices:
            point = {"x": x, "y": y, "spatialReference": geometry.get("spatialReference")}
            all_results.extend(
                self._client.identify(
                    service_url=service_url, layer_id=layer.layer_id,
                    geometry=point, geometry_type="esriGeometryPoint",
                )
            )
        return all_results

    def _resolve_raster_presence(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        assert rule.layer is not None
        return "O" if self._identify_has_value(rule.layer, parcelle) else "N"

    def _resolve_raster_presence_any(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        if not self._require_geometry(rule, parcelle):
            return "N"
        assert rule.layers
        for layer in rule.layers:
            if self._identify_has_value(layer, parcelle):
                return "O"
        return "N"

    # -- Règles conditionnelles (liens) -------------------------------------

    def _resolve_conditional_fixed_link(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        assert rule.trigger_column is not None
        if parcelle.valeurs.get(rule.trigger_column) == rule.trigger_value:
            return rule.link_url or "/"
        return "/"

    def _resolve_conditional_lookup_link(self, rule: ColumnRule, parcelle: Parcelle) -> str:
        assert rule.trigger_column is not None and rule.lookup_field is not None
        if parcelle.valeurs.get(rule.trigger_column) != rule.trigger_value:
            return "/"
        commune_row = self._liens_communaux.get(parcelle.commune.strip().lower())
        if not commune_row or not commune_row.get(rule.lookup_field):
            _logger.warning(
                "Commune '%s' absente de data/liens_communaux.csv (colonne %s) : à compléter manuellement.",
                parcelle.commune, rule.column,
            )
            return "À COMPLÉTER MANUELLEMENT"
        return commune_row[rule.lookup_field]
