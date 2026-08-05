"""Découverte des rues/adresses d'une commune (registre ICAR), complétion
par les parcelles cadastrales sans adresse (CADMAP + tracé réel de la rue,
PICC), et rattachement de chaque adresse à sa parcelle cadastrale (CADMAP).

Conformément à la règle métier du document Word ("Recenser TOUTES les
parcelles composant la rue [...] pour les parcelles sans numéro d'adresse,
compléter par / les cellules" — confirmé par l'exemplaire de référence, où
57% des lignes ont "/" comme numéro) : une rue n'est pas résumée à ses
adresses ICAR. Chaque rue est complétée par les parcelles cadastrales qui
la bordent mais n'ont pas d'adresse enregistrée, avec `numero = "/"`.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

import config
from models.parcelle import Parcelle
from services.cache_service import ProgressStore
from services.geoportail_service import ArcGISRestClient
from utils.geometrie import cote_et_position, construire_segments
from utils.logger import get_logger

_logger = get_logger("services.cadastre_service")

_ICAR_ADRESSES_LAYER_ID = 1  # "Points d'adresses"
_CADMAP_LAYER_ID = 0  # "Parcelles cadastrales"
_PICC_VOIRIE_AXE_LAYER_ID = 21  # "Voirie - Axe" (tracé réel des rues)

# Distance entre deux points échantillonnés le long du tracé d'une rue, et
# marge de la fenêtre carrée interrogée sur CADMAP à chaque point. Choisis
# pour que les fenêtres consécutives se recouvrent (marge >= la moitié du
# pas) et ne laissent aucun trou le long de la rue, tout en limitant le
# nombre de requêtes réseau (vérifié en conditions réelles sur Avenue
# Docteur Schweitzer, Colfontaine : 154 points à 15m/20m contre une
# vérité de référence à 143 parcelles trouvées).
_PAS_ECHANTILLONNAGE_RUE_M = 30.0
_MARGE_FENETRE_PARCELLES_M = 25.0


def _point_dans_polygone(x: float, y: float, rings: Sequence[Sequence[Sequence[float]]]) -> bool:
    """Test point-dans-polygone (ray casting, règle pair-impair), sans
    dépendance géospatiale externe (shapely, etc. — voir choix technique
    documenté dans README.md). Fonctionne correctement pour un polygone à
    trous (ex: bâtiment intérieur exclu d'une parcelle) : un point dans un
    trou est traversé un nombre pair de fois au total sur tous les
    anneaux, donc compté comme "hors polygone", sans traitement spécial
    du sens de parcours des anneaux."""
    dedans = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                dedans = not dedans
            j = i
    return dedans


def _centre_polygone(rings: Sequence[Sequence[Sequence[float]]]) -> Optional[Tuple[float, float]]:
    """Moyenne simple des sommets du premier anneau — pas un vrai centroïde
    géométrique, mais suffisant pour situer une parcelle par rapport au
    tracé d'une rue (voir utils/geometrie.py), sans dépendance externe."""
    if not rings or not rings[0]:
        return None
    xs = [pt[0] for pt in rings[0]]
    ys = [pt[1] for pt in rings[0]]
    return sum(xs) / len(xs), sum(ys) / len(ys)


class CadastreService:
    def __init__(self, client: ArcGISRestClient, progress_store: Optional[ProgressStore] = None) -> None:
        self._client = client
        # Optionnel (rétrocompatible) : sans lui, la découverte des
        # parcelles sans adresse est refaite intégralement à chaque appel
        # (voir _parcelles_cadastrales_sans_adresse) — utile pour un usage
        # ponctuel, mais coûteux pour un traitement complet de commune.
        self._progress_store = progress_store
        # Cache mémoire (voir _adresses_commune) : la même commune est
        # revisitée à chaque rue traitée (potentiellement des centaines de
        # fois par exécution) — évite de reconstruire la liste de points à
        # chaque appel (la requête réseau elle-même est déjà servie par le
        # cache HTTP après le premier appel, mais pas la reconstruction de
        # cette liste Python).
        self._adresses_commune_cache: Dict[str, List[Tuple[float, float]]] = {}

    def _adresses_commune(self, commune: str) -> List[Tuple[float, float]]:
        """Coordonnées de TOUTES les adresses ICAR de la commune, toutes
        rues confondues — sert à `a_une_adresse_ailleurs` pour ne jamais
        marquer "/" une parcelle qui a en réalité une adresse sur une AUTRE
        rue que celle en cours de traitement (voir
        `_parcelles_cadastrales_sans_adresse`, qui ne vérifiait auparavant
        que les adresses de la rue en cours — cas réel découvert à Dour :
        la parcelle "91P3" a une adresse ICAR réelle, "170", mais rattachée
        à Rue Moranfayt, pas à Avenue Hyacinthe Harmegnies qu'elle borde
        aussi ; elle apparaissait donc à tort deux fois dans le fichier
        final)."""
        if commune not in self._adresses_commune_cache:
            features = self._client.query_layer(
                service_url=config.ARCGIS_SERVICES["ICAR_ADR_PT"],
                layer_id=_ICAR_ADRESSES_LAYER_ID,
                where=f"COM_NM = '{self._escape(commune)}'",
                out_fields="ADR_ID",
                return_geometry=True,
                order_by="ADR_ID",
            )
            self._adresses_commune_cache[commune] = [
                (f["geometry"]["x"], f["geometry"]["y"]) for f in features if f.get("geometry")
            ]
        return self._adresses_commune_cache[commune]

    def a_une_adresse_ailleurs(self, commune: str, rings: Sequence[Sequence[Sequence[float]]]) -> bool:
        """True si au moins une adresse ICAR de la commune (peu importe la
        rue) tombe dans ce polygone cadastral — voir `_adresses_commune`.
        Utilisé à la fois par la découverte (`_parcelles_cadastrales_sans_adresse`)
        et par le rattrapage qui nettoie les rues déjà vérifiées avant ce
        changement (voir main.py::nettoyer_doublons_sans_adresse)."""
        return any(_point_dans_polygone(x, y, rings) for x, y in self._adresses_commune(commune))

    # -- Découverte des rues et adresses (ICAR) -----------------------------

    def lister_rues(self, commune: str) -> List[str]:
        """Toutes les rues connues du registre ICAR pour une commune.

        `order_by="ADR_ID"` : requête sans filtre géométrique ni RUE_NM,
        donc potentiellement multi-pages pour une grande commune (vérifié
        en conditions réelles sur Dour : 7976 adresses, 4 pages) — sans
        tri explicite, la pagination ArcGIS n'est pas stable d'une page à
        l'autre (voir ArcGISRestClient.query_layer), ce qui peut faire
        disparaître des rues entières du résultat sans que rien ne le
        signale (même nombre total de features, ensemble différent)."""
        features = self._client.query_layer(
            service_url=config.ARCGIS_SERVICES["ICAR_ADR_PT"],
            layer_id=_ICAR_ADRESSES_LAYER_ID,
            where=f"COM_NM = '{self._escape(commune)}'",
            out_fields="RUE_NM",
            return_geometry=False,
            order_by="ADR_ID",
        )
        rues = sorted(
            {f["attributes"]["RUE_NM"] for f in features if f["attributes"].get("RUE_NM")}
        )
        _logger.info("%d rue(s) trouvée(s) pour la commune '%s'", len(rues), commune)
        return rues

    def lister_parcelles(
        self, pays: str, commune: str, rue: str, codes_postaux: Optional[List[str]] = None,
    ) -> List[Parcelle]:
        """Construit une `Parcelle` par entrée ICAR de la rue, PUIS complète
        avec les parcelles cadastrales qui bordent la rue mais n'ont pas
        d'adresse (voir `_parcelles_cadastrales_sans_adresse` — obligatoire
        d'après le document Word, voir docstring du module). Découverte
        pure, SANS rattachement cadastral pour les parcelles AVEC adresse
        (voir `rattacher_parcelle_cadastrale`, volontairement séparé : c'est
        l'étape coûteuse en requêtes réseau, une par adresse, à ne
        déclencher que pour les parcelles réellement traitées à cette
        exécution — voir main.py, mode incrémental). Les parcelles SANS
        adresse ont en revanche déjà leur géométrie/numéro cadastral au
        retour de cette méthode : elles sont trouvées directement via
        CADMAP, aucun rattachement différé n'est nécessaire pour elles.

        `codes_postaux`, si fourni, permet de sauter la découverte des
        parcelles sans adresse (coûteuse : PICC + plusieurs requêtes CADMAP
        par rue) pour une rue dont AUCUNE adresse ICAR ne correspond à ce
        filtre — inutile de payer ce coût pour une rue que main.py va de
        toute façon filtrer entièrement juste après (voir traiter_commune).
        Une rue sans aucune adresse ICAR (donc sans code postal connu à
        l'avance) n'est jamais sautée par prudence : impossible de savoir
        sans le vérifier si elle est dans le filtre.

        Le code postal n'est jamais demandé en entrée : il est lu directement
        sur chaque enregistrement ICAR (champ CODE_POSTAL), seule source
        fiable pour une commune fusionnée pouvant couvrir plusieurs codes
        postaux (ex: anciennes communes/sections rattachées)."""
        features = self._client.query_layer(
            service_url=config.ARCGIS_SERVICES["ICAR_ADR_PT"],
            layer_id=_ICAR_ADRESSES_LAYER_ID,
            where=f"COM_NM = '{self._escape(commune)}' AND RUE_NM = '{self._escape(rue)}'",
            out_fields="ADR_ID,ADR_NUMERO,RUE_NM,COM_NM,CODE_POSTAL",
            return_geometry=True,
        )

        parcelles: List[Parcelle] = []
        for feature in features:
            attrs = feature["attributes"]
            geometry = feature.get("geometry")
            numero = attrs.get("ADR_NUMERO")
            code_postal = attrs.get("CODE_POSTAL")
            if not code_postal:
                _logger.warning(
                    "Adresse ICAR sans code postal (%s %s, %s)", rue, numero, commune
                )

            parcelle = Parcelle(
                pays=pays,
                code_postal=code_postal or "/",
                commune=commune,
                rue=rue,
                numero=numero if numero else "/",
                adr_id=str(attrs.get("ADR_ID")) if attrs.get("ADR_ID") is not None else None,
                x=geometry.get("x") if geometry else None,
                y=geometry.get("y") if geometry else None,
            )
            if parcelle.x is None or parcelle.y is None:
                _logger.warning(
                    "Adresse sans géométrie ICAR (%s %s, %s) : rattachement cadastral impossible",
                    rue, numero, commune,
                )
            parcelles.append(parcelle)

        if codes_postaux is not None:
            codes_rue = {p.code_postal for p in parcelles if p.code_postal and p.code_postal != "/"}
            if codes_rue and not (codes_rue & set(codes_postaux)):
                return parcelles

        sans_adresse = self._parcelles_cadastrales_sans_adresse(pays, commune, rue, parcelles)

        # Côté ("G"/"D") et position le long de la rue (voir
        # utils/geometrie.py) — calculés pour les parcelles AVEC adresse ici
        # (celles sans adresse l'ont déjà, calculé/mis en cache dans
        # `_parcelles_cadastrales_sans_adresse`). Un HTTP GET identique au
        # tracé déjà interrogé pour la découverte sans adresse (ou déjà en
        # cache si cette rue est marquée vérifiée) : pas de coût réseau
        # supplémentaire réel, juste une lecture de cache dans la majorité
        # des cas.
        troncons = self.recuperer_troncons(commune, rue)
        segments = construire_segments(troncons)
        for p in parcelles:
            if p.x is None or p.y is None:
                continue
            resultat = cote_et_position(p.x, p.y, segments)
            if resultat is not None:
                p.cote, p.position_rue = resultat

        parcelles.extend(sans_adresse)
        return parcelles

    def recuperer_troncons(self, commune: str, rue: str) -> List[Dict[str, Any]]:
        """Tronçons PICC ("Voirie - Axe") du tracé réel de la rue — voir
        `utils/geometrie.py`. Factorisé car utilisé à la fois pour la
        découverte des parcelles sans adresse et pour situer les parcelles
        AVEC adresse (côté/position)."""
        return self._client.query_layer(
            service_url=config.ARCGIS_SERVICES["PICC_VOIRIE"],
            layer_id=_PICC_VOIRIE_AXE_LAYER_ID,
            where=f"RUE_NOM1 = '{self._escape(rue)}' AND COMMU_NOM1 = '{self._escape(commune)}'",
            out_fields="OBJECTID",
            return_geometry=True,
        )

    def point_pour_identifiant(self, identifiant: str) -> Optional[Tuple[float, float]]:
        """Retrouve un point représentatif (x, y) pour une parcelle DÉJÀ
        résolue, à partir de son seul `identifiant` — sert au rattrapage
        côté/position (voir main.py::recalculer_cote_position) pour les
        parcelles traitées avant l'ajout de ce calcul, dont la géométrie
        n'a jamais été conservée dans `parcelle.valeurs` (seules les
        colonnes du tableau Excel le sont).

        `identifiant` encode soit un `adr_id` ICAR (parcelles avec
        adresse : "Commune|ICAR:12345"), soit un `capakey` CADMAP
        (parcelles sans adresse : "Commune|CAPAKEY:...") — voir
        `models.parcelle.Parcelle.identifiant`. Une seule requête ciblée
        (par ADR_ID ou par CAPAKEY), jamais une redécouverte de toute la
        rue. `None` pour un identifiant au format le plus ancien (ni
        ICAR ni CAPAKEY — rue+numéro seul), trop ambigu pour être
        retrouvé de façon fiable."""
        if "|ICAR:" in identifiant:
            adr_id = identifiant.rsplit("|ICAR:", 1)[1]
            features = self._client.query_layer(
                service_url=config.ARCGIS_SERVICES["ICAR_ADR_PT"],
                layer_id=_ICAR_ADRESSES_LAYER_ID,
                where=f"ADR_ID = {adr_id}",
                out_fields="ADR_ID",
                return_geometry=True,
            )
            if features and features[0].get("geometry"):
                geom = features[0]["geometry"]
                return geom["x"], geom["y"]
            return None

        if "|CAPAKEY:" in identifiant:
            capakey = identifiant.rsplit("|CAPAKEY:", 1)[1]
            features = self._client.query_layer(
                service_url=config.ARCGIS_SERVICES["CADMAP_PARCELLES"],
                layer_id=_CADMAP_LAYER_ID,
                where=f"CAPAKEY = '{self._escape(capakey)}'",
                out_fields="CAPAKEY",
                return_geometry=True,
            )
            if features and features[0].get("geometry"):
                return _centre_polygone(features[0]["geometry"].get("rings", []))
            return None

        return None

    # -- Parcelles cadastrales sans adresse (complétion CADMAP + PICC) ------

    def _parcelles_cadastrales_sans_adresse(
        self, pays: str, commune: str, rue: str, parcelles_icar: List[Parcelle],
    ) -> List[Parcelle]:
        """Parcelles CADMAP bordant la rue (tracé réel, couche PICC "Voirie -
        Axe") qui ne correspondent à AUCUNE adresse ICAR déjà connue de
        cette rue — ajoutées avec `numero = "/"`, conformément à la règle
        métier. Contrairement aux parcelles avec adresse, leur géométrie et
        numéro cadastral sont déjà connus ici (trouvés directement via
        CADMAP), sans rattachement différé.

        Le tracé réel de la rue (polylignes, pas juste une boîte englobante
        des adresses) évite de ratisser les parcelles d'une rue voisine —
        vérifié en conditions réelles : une boîte englobante simple sur
        Avenue Docteur Schweitzer (Colfontaine) trouvait 489 parcelles,
        contre 143 avec le tracé réel échantillonné — la différence est
        purement des parcelles hors de la rue elle-même.

        Si `self._progress_store` est fourni et que cette rue a déjà été
        vérifiée lors d'un appel précédent (même une exécution antérieure,
        persisté en base — voir ProgressStore.rue_deja_verifiee), le
        résultat déjà connu est directement relu, sans jamais refaire
        l'appel PICC + les requêtes CADMAP (~20-30s/rue, potentiellement
        plusieurs dizaines de minutes pour une commune entière si refait à
        chaque exécution — observé en conditions réelles)."""
        if self._progress_store is not None and self._progress_store.rue_deja_verifiee(commune, rue):
            return [
                Parcelle(
                    pays=pays, code_postal=p["code_postal"] or "/", commune=commune, rue=rue,
                    numero="/", capakey=p["capakey"], numero_cadastral=p["numero_cadastral"],
                    geometry=p["geometry"], cote=p.get("cote"), position_rue=p.get("position_rue"),
                )
                for p in self._progress_store.parcelles_sans_adresse(commune, rue)
            ]

        troncons = self.recuperer_troncons(commune, rue)
        if not troncons:
            if self._progress_store is not None:
                self._progress_store.enregistrer_parcelles_sans_adresse(commune, rue, [])
            return []

        points_echantillon: List[Tuple[float, float]] = []
        for troncon in troncons:
            for chemin in (troncon.get("geometry") or {}).get("paths", []):
                points_echantillon.extend(self._echantillonner_segment(chemin))

        capakeys_vus: Dict[str, Dict[str, Any]] = {}
        for x, y in points_echantillon:
            marge = _MARGE_FENETRE_PARCELLES_M
            enveloppe = {
                "rings": [[
                    [x - marge, y - marge], [x + marge, y - marge],
                    [x + marge, y + marge], [x - marge, y + marge], [x - marge, y - marge],
                ]],
                "spatialReference": {"wkid": 31370},
            }
            features = self._client.query_layer(
                service_url=config.ARCGIS_SERVICES["CADMAP_PARCELLES"],
                layer_id=_CADMAP_LAYER_ID,
                geometry=enveloppe, geometry_type="esriGeometryPolygon",
                out_fields="CAPAKEY,RADICAL,BIS,EXPOSANT,PUISSANCE",
                return_geometry=True,
            )
            for feature in features:
                capakey = feature["attributes"].get("CAPAKEY")
                if capakey and capakey not in capakeys_vus:
                    capakeys_vus[capakey] = feature

        code_postal = self._code_postal_le_plus_frequent(parcelles_icar)
        segments = construire_segments(troncons)

        nouvelles: List[Parcelle] = []
        for capakey, feature in capakeys_vus.items():
            rings = (feature.get("geometry") or {}).get("rings", [])
            if self.a_une_adresse_ailleurs(commune, rings):
                continue  # a déjà une adresse ICAR quelque part dans la commune
            attrs = feature["attributes"]
            parcelle = Parcelle(
                pays=pays,
                code_postal=code_postal,
                commune=commune,
                rue=rue,
                numero="/",
                capakey=capakey,
                numero_cadastral=self._format_numero_cadastral(attrs),
                geometry=feature.get("geometry"),
            )
            centre = _centre_polygone(rings)
            if centre is not None:
                resultat = cote_et_position(centre[0], centre[1], segments)
                if resultat is not None:
                    parcelle.cote, parcelle.position_rue = resultat
            nouvelles.append(parcelle)
        _logger.info(
            "%d parcelle(s) cadastrale(s) sans adresse trouvée(s) pour la rue '%s' (commune '%s').",
            len(nouvelles), rue, commune,
        )
        if self._progress_store is not None:
            self._progress_store.enregistrer_parcelles_sans_adresse(commune, rue, [
                {
                    "capakey": n.capakey, "numero_cadastral": n.numero_cadastral,
                    "code_postal": n.code_postal, "geometry": n.geometry,
                    "cote": n.cote, "position_rue": n.position_rue,
                }
                for n in nouvelles
            ])
        return nouvelles

    @staticmethod
    def _echantillonner_segment(chemin: Sequence[Sequence[float]]) -> List[Tuple[float, float]]:
        """Points régulièrement espacés (`_PAS_ECHANTILLONNAGE_RUE_M`) le
        long d'une polyligne (liste de [x, y])."""
        points: List[Tuple[float, float]] = []
        for i in range(len(chemin) - 1):
            x1, y1 = chemin[i]
            x2, y2 = chemin[i + 1]
            longueur = math.hypot(x2 - x1, y2 - y1)
            n = max(1, int(longueur // _PAS_ECHANTILLONNAGE_RUE_M))
            for k in range(n + 1):
                t = k / n if n else 0.0
                points.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        return points

    @staticmethod
    def _code_postal_le_plus_frequent(parcelles: List[Parcelle]) -> str:
        """Code postal à assigner à une parcelle sans adresse ICAR (donc
        sans son propre champ CODE_POSTAL) : celui des adresses ICAR déjà
        connues de la même rue, qui partagent presque toujours le même
        code postal. "/" si la rue n'a aucune adresse ICAR du tout."""
        codes = [p.code_postal for p in parcelles if p.code_postal and p.code_postal != "/"]
        if not codes:
            return "/"
        return Counter(codes).most_common(1)[0][0]

    # -- Rattachement à la parcelle cadastrale (CADMAP) ---------------------
    # Étape coûteuse (1 requête réseau par parcelle) : à appeler seulement
    # pour les parcelles réellement sélectionnées pour traitement.

    def rattacher_parcelle_cadastrale(self, parcelle: Parcelle) -> None:
        if parcelle.x is None or parcelle.y is None:
            return
        point_geom = {
            "x": parcelle.x,
            "y": parcelle.y,
            "spatialReference": {"wkid": 31370},
        }
        features = self._client.query_layer(
            service_url=config.ARCGIS_SERVICES["CADMAP_PARCELLES"],
            layer_id=_CADMAP_LAYER_ID,
            geometry=point_geom,
            geometry_type="esriGeometryPoint",
            out_fields="CAPAKEY,RADICAL,BIS,EXPOSANT,PUISSANCE",
            return_geometry=True,
        )
        if not features:
            _logger.warning(
                "Aucune parcelle cadastrale trouvée sous l'adresse %s", parcelle.identifiant
            )
            return

        attrs = features[0]["attributes"]
        parcelle.capakey = attrs.get("CAPAKEY")
        parcelle.numero_cadastral = self._format_numero_cadastral(attrs)
        parcelle.geometry = features[0].get("geometry")

    @staticmethod
    def _format_numero_cadastral(attrs: Dict[str, Any]) -> str:
        """Reconstitue le numéro cadastral lisible à partir des champs bruts
        CADMAP (RADICAL, BIS, EXPOSANT, PUISSANCE).

        Cas BIS="00" (pas de subdivision), vérifié contre le fichier exemple
        (format attendu, jamais utilisé comme source de données) sur 3
        adresses réelles de Crisnée :
        RADICAL="0014" BIS="00" EXPOSANT="H" PUISSANCE="002" -> "14H2"
        RADICAL="0034" BIS="00" EXPOSANT="D" PUISSANCE="001" -> "34D1"
        Les zéros de tête sont supprimés sur RADICAL et PUISSANCE.

        Cas BIS≠"00" (subdivision, fréquent — ~2000 parcelles trouvées en
        une seule requête d'échantillon) : aucun exemple de ce cas n'existe
        dans le fichier exemple pour le confirmer, mais la notation
        cadastrale belge standard insère un "/" entre le radical et le bis
        (visible dans le CAPAKEY lui-même, ex: "2075/02A000") — ex.
        RADICAL="2075" BIS="02" EXPOSANT="A" -> "2075/2A". Cette partie du
        format reste donc une extrapolation, pas une valeur confirmée par un
        exemple réel — à vérifier si possible avec un cas de référence.

        EXPOSANT="_" est un espace réservé ArcGIS pour "aucun exposant" (vu
        sur de vraies parcelles, ex. "2075/03_000") et est traité comme vide."""
        radical = (attrs.get("RADICAL") or "").lstrip("0") or "0"
        bis = (attrs.get("BIS") or "").lstrip("0")
        exposant = (attrs.get("EXPOSANT") or "").strip()
        if exposant == "_":
            exposant = ""
        puissance = (attrs.get("PUISSANCE") or "").lstrip("0")
        if bis:
            return f"{radical}/{bis}{exposant}{puissance}"
        return f"{radical}{exposant}{puissance}"

    @staticmethod
    def _escape(value: str) -> str:
        """Échappe les apostrophes pour une clause WHERE ArcGIS SQL sûre."""
        return value.replace("'", "''")
