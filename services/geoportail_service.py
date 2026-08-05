"""Client générique pour les services ArcGIS REST (MapServer) du Géoportail.

Toute la logique HTTP (cache, limitation de débit, réessai, journalisation
DEBUG) est centralisée ici. Les autres services (cadastre, couches
métier) ne construisent jamais de requêtes `requests.get(...)` eux-mêmes :
ils passent systématiquement par `ArcGISRestClient`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from services.cache_service import CacheService
from services.exceptions import ArcGISServiceError, ArcGISTransientError
from utils.logger import get_logger
from utils.rate_limiter import RateLimiter
from utils.retry import http_retry

_logger = get_logger("services.geoportail_service")

# Garde-fou pour la pagination de query_layer (voir plus bas) : au-delà de
# ce nombre de pages, on arrête et on journalise un avertissement plutôt
# que de boucler indéfiniment si un service continue de signaler
# exceededTransferLimit sans jamais se stabiliser. 50 pages à 2000
# résultats (limite observée sur ICAR_ADR_PT) représentent déjà 100 000
# éléments, largement au-delà de toute commune wallonne.
_MAX_PAGES_QUERY = 50

# Longueur d'URL (hors domaine) au-delà de laquelle on bascule en POST plutôt
# que GET. Observé en production (parcelle Dour|ICAR:1429799, géométrie
# cadastrale à plusieurs dizaines d'anneaux/trous) : une géométrie assez
# complexe produit une URL de plus de 30 000 caractères, rejetée par certains
# serveurs (ex: sig.digiteaux.be, service PASH) avec "414 Request-URI Too
# Large" — une erreur définitive (la même requête échoue à chaque réessai),
# jamais transitoire. Passer en POST (mêmes paramètres, en corps de requête)
# supprime toute limite de longueur d'URL ; marge large sous la limite
# typique (2048-8192 selon serveurs/proxies) pour rester sûr même sur des
# services plus stricts.
_MAX_URL_LENGTH_BEFORE_POST = 1800


class ArcGISRestClient:
    def __init__(
        self,
        cache: CacheService,
        rate_limiter: RateLimiter,
        timeout: int = 30,
        use_cache: bool = True,
    ) -> None:
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._timeout = timeout
        self._use_cache = use_cache
        self._session = requests.Session()

    @http_retry()
    def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._use_cache:
            cached = self._cache.get(url, params)
            if cached is not None:
                return cached

        self._rate_limiter.wait()
        if len(urlencode(params, doseq=True)) > _MAX_URL_LENGTH_BEFORE_POST:
            _logger.debug("POST %s params=%s (URL trop longue pour GET)", url, params)
            response = self._session.post(url, data=params, timeout=self._timeout)
        else:
            _logger.debug("GET %s params=%s", url, params)
            response = self._session.get(url, params=params, timeout=self._timeout)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and "error" in data:
            error = data["error"]
            _logger.error("Erreur ArcGIS pour %s: %s", url, error)
            code = error.get("code") if isinstance(error, dict) else None
            texte_erreur = " ".join(
                [str(error.get("message", ""))] + [str(d) for d in (error.get("details") or [])]
            ) if isinstance(error, dict) else ""
            # ArcGIS répond parfois en HTTP 200 avec un code d'erreur de
            # type serveur (>= 500) dans le corps JSON plutôt qu'un vrai
            # statut HTTP 5xx — raise_for_status() ne le voit donc jamais.
            # Observé en production : "Service not started.", "Error
            # performing query operation" — transitoire, à réessayer.
            #
            # Le message ci-dessous ("...not a Geographic Coordinate
            # System"), bien que renvoyé avec un code 400 (habituellement
            # définitif), s'est révélé transitoire à plusieurs reprises en
            # conditions réelles (services AMENAGEMENT_TERRITOIRE/REVIT_URB,
            # PRE, TERRILS, ZIP, PPR, PLANHP, RMBMT_URB, RENOV_URB —
            # confirmé par des requêtes identiques échouant puis réussissant
            # quelques minutes plus tard, sans aucun changement côté
            # client). Traité comme transitoire pour être réessayé
            # immédiatement (au lieu d'attendre le prochain lancement via
            # LayersService.retry_erreurs).
            signatures_transitoires = ("not a Geographic Coordinate System",)
            est_transitoire = (isinstance(code, int) and code >= 500) or any(
                sig in texte_erreur for sig in signatures_transitoires
            )
            exc_cls = ArcGISTransientError if est_transitoire else ArcGISServiceError
            raise exc_cls(f"{url} -> {error}")

        _logger.debug(
            "Réponse %s: %s", url, json.dumps(data)[:2000]
        )

        if self._use_cache:
            self._cache.set(url, params, data)
        return data

    def query_layer(
        self,
        service_url: str,
        layer_id: int,
        geometry: Optional[Dict[str, Any]] = None,
        geometry_type: str = "esriGeometryPolygon",
        spatial_rel: str = "esriSpatialRelIntersects",
        where: str = "1=1",
        out_fields: str = "*",
        return_geometry: bool = False,
        in_sr: int = 31370,
        out_sr: int = 31370,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Interroge une sous-couche ArcGIS REST (`/query`) et renvoie TOUTES
        ses features, en paginant automatiquement si le service tronque la
        réponse (`exceededTransferLimit`, ex: `maxRecordCount=2000` observé
        sur ICAR_ADR_PT — une commune comme Dour a largement plus de 2000
        points d'adresse).

        `order_by` (ex: `"OBJECTID"`) devrait être fourni pour toute requête
        susceptible de dépasser une seule page SANS filtre géométrique
        étroit (ex: toutes les adresses d'une commune entière) : vérifié en
        conditions réelles sur ICAR_ADR_PT — sans tri explicite, l'ordre
        renvoyé par page n'est PAS stable d'une page à l'autre. Deux appels
        avec le même total de features (donc en apparence corrects) peuvent
        pourtant contenir des ensembles différents : certaines lignes
        dupliquées d'une page à l'autre, d'autres absentes des deux (cas
        réel : commune Dour, 7976 adresses sur 4 pages, une adresse précise
        absente du résultat sans tri, présente et le total inchangé avec
        `order_by='ADR_ID'`). Sans ce tri, la pagination protège contre un
        résultat tronqué mais pas contre un résultat incomplet.

        `geometry` doit être un dict de géométrie Esri (ex: polygone de
        parcelle) en Lambert 72 (EPSG:31370), système utilisé par la quasi-
        totalité des services du Géoportail de Wallonie utilisés ici — mais
        pas tous : PICC_VDIFF (tracé des rues), par exemple, a pour système
        natif Lambert 2008 (EPSG:3812). `outSR` est donc systématiquement
        forcé à 31370 (avec ou sans filtre géométrique en entrée), pour que
        toute géométrie renvoyée soit toujours exploitable telle quelle par
        le reste du code sans jamais avoir à deviner le système natif de
        CHAQUE couche interrogée (bug réel rencontré : géométrie PICC
        renvoyée en 3812 sans `outSR` explicite, coordonnées ~612000/624000
        au lieu de ~110000/120000, tout calcul de distance/tampon ensuite
        silencieusement faux).
        """
        url = f"{service_url}/{layer_id}/query"
        base_params: Dict[str, Any] = {
            "f": "json",
            "where": where,
            "outFields": out_fields,
            "returnGeometry": return_geometry,
            "outSR": out_sr,
        }
        if order_by:
            base_params["orderByFields"] = order_by
        if geometry is not None:
            base_params.update(
                {
                    "geometry": json.dumps(geometry),
                    "geometryType": geometry_type,
                    "inSR": in_sr,
                    "spatialRel": spatial_rel,
                }
            )

        all_features: List[Dict[str, Any]] = []
        offset = 0
        nb_pages = 0
        for nb_pages in range(1, _MAX_PAGES_QUERY + 1):
            # `resultOffset` n'est ajouté qu'à partir de la 2e page : certains
            # services (ex: AMENAGEMENT_TERRITOIRE/REVIT_URB, PRE, TERRILS,
            # ZIP, PPR — observé en conditions réelles) rejettent purement et
            # simplement toute requête portant ce paramètre, même à 0, avec
            # "Pagination is not supported." Comme l'écrasante majorité des
            # couches ne dépassent jamais une seule page, ne l'envoyer que
            # lorsqu'il est réellement nécessaire (page 2+) évite ce rejet
            # sans rien perdre côté pagination réelle (ICAR_ADR_PT, etc.).
            params = dict(base_params, resultOffset=offset) if offset else dict(base_params)
            data = self._get_json(url, params)
            features = data.get("features", [])
            all_features.extend(features)
            if not data.get("exceededTransferLimit") or not features:
                break
            offset += len(features)
        else:
            _logger.warning(
                "Couche %s/%s : pagination arrêtée après %d pages (%d features) — "
                "le service continue de signaler exceededTransferLimit, résultat "
                "possiblement incomplet.", service_url, layer_id, _MAX_PAGES_QUERY, len(all_features),
            )

        _logger.debug(
            "Couche %s/%s : %d intersection(s) trouvée(s) (%d page%s)",
            service_url, layer_id, len(all_features), nb_pages, "s" if nb_pages > 1 else "",
        )
        return all_features

    # Taille d'image fixe utilisée pour toutes les requêtes `identify` sur
    # raster. Sert à convertir une échelle cible (mapExtent/imageDisplay) en
    # marge réelle (mètres) — voir `_scale_safe_margin`.
    _IDENTIFY_IMAGE_PX = 600
    _IDENTIFY_DPI = 96
    _METERS_PER_SCALE_UNIT = (_IDENTIFY_IMAGE_PX / _IDENTIFY_DPI * 0.0254) / 2

    def identify(
        self,
        service_url: str,
        layer_id: int,
        geometry: Dict[str, Any],
        geometry_type: str = "esriGeometryPolygon",
        in_sr: int = 31370,
    ) -> List[Dict[str, Any]]:
        """Interroge une couche (raster OU vectorielle) via l'opération
        `identify`. Utilisé pour les rasters classifiés (ArcGIS ne supporte
        pas `/query` dessus, ex: aléa d'inondation, ruissellement, érosion),
        et depuis peu aussi pour certaines couches vectorielles dont
        `/query` s'est révélé, en conditions réelles, sujet à un incident
        intermittent côté serveur Wallonie (voir
        services/layers_service.py::_resolve_vector_identify_presence —
        AMENAGEMENT_TERRITOIRE/REVIT_URB, PRE, TERRILS, ZIP, etc. : erreur
        "not a Geographic Coordinate System" apparaissant et disparaissant
        sans aucun changement de requête ; `/identify` sur ces mêmes couches
        s'est montré fiable pendant l'incident, y compris quand `/query`
        échouait au même moment sur la même géométrie).

        `mapExtent`/`imageDisplay` sont obligatoires pour `identify`, et leur
        ratio détermine l'échelle à laquelle ArcGIS considère la couche.
        Si cette échelle tombe hors de la fenêtre de visibilité propre à la
        couche (`minScale`/`maxScale`), ArcGIS renvoie un résultat VIDE —
        confirmé empiriquement sur deux couches différentes avec des
        fenêtres de visibilité très différentes (ALEA_INOND : minScale=0
        maxScale=24999 ; ERRUISSOL : minScale=20032 maxScale=4500 — une
        marge fixe qui convient à l'une échoue systématiquement sur
        l'autre). La marge est donc calculée dynamiquement à partir de la
        fenêtre de visibilité réelle de CHAQUE couche (`_scale_safe_margin`),
        récupérée une fois puis mise en cache HTTP comme le reste.
        """
        margin = self._scale_safe_margin(service_url, layer_id)
        xmin, ymin, xmax, ymax = self._bounding_box(geometry, margin=margin)
        url = f"{service_url}/identify"
        params: Dict[str, Any] = {
            "f": "json",
            "geometry": json.dumps(geometry),
            "geometryType": geometry_type,
            "sr": in_sr,
            "layers": f"visible:{layer_id}",
            "tolerance": 0,
            "mapExtent": f"{xmin},{ymin},{xmax},{ymax}",
            "imageDisplay": f"{self._IDENTIFY_IMAGE_PX},{self._IDENTIFY_IMAGE_PX},{self._IDENTIFY_DPI}",
            "returnGeometry": False,
        }
        data = self._get_json(url, params)
        results = data.get("results", [])
        _logger.debug(
            "Identify %s/%s (marge=%.0fm) : %d résultat(s)",
            service_url, layer_id, margin, len(results),
        )
        return results

    def _scale_safe_margin(self, service_url: str, layer_id: int) -> float:
        """Marge (mètres) donnant une échelle mapExtent/imageDisplay comprise
        dans la fenêtre [maxScale, minScale] de la couche (0 = illimité côté
        ArcGIS). Cible le double de maxScale (marge confortable au-dessus du
        seuil bas), plafonné à 90% de minScale si celui-ci est borné."""
        layer_info = self._get_json(f"{service_url}/{layer_id}", {"f": "json"})
        min_scale = layer_info.get("minScale") or 0
        max_scale = layer_info.get("maxScale") or 0
        target_scale = max(max_scale, 1000) * 2
        if min_scale > 0:
            target_scale = min(target_scale, min_scale * 0.9)
        return target_scale * self._METERS_PER_SCALE_UNIT

    @staticmethod
    def _bounding_box(geometry: Dict[str, Any], margin: float) -> tuple:
        xs: List[float] = []
        ys: List[float] = []
        if "x" in geometry and "y" in geometry:
            xs, ys = [geometry["x"]], [geometry["y"]]
        else:
            rings = geometry.get("rings") or geometry.get("paths") or []
            for ring in rings:
                for x, y in ring:
                    xs.append(x)
                    ys.append(y)
        if not xs or not ys:
            raise ValueError(f"Géométrie sans coordonnées exploitables : {geometry}")
        return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)

    def get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Appel générique pour les endpoints hors `/query` (ex: geocodeWS)."""
        return self._get_json(url, params)
