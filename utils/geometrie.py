"""Position d'une parcelle par rapport au tracé d'une rue : de quel côté
(gauche/droite) et à quelle distance depuis le début de la rue.

Sert à trier une rue "toutes les parcelles de gauche, puis toutes celles de
droite, dans l'ordre où on les croise en marchant" — demandé par le client,
plus fidèle à une vérification manuelle sur le terrain qu'un tri par simple
numéro. Fonctionne pareil pour les parcelles avec ou sans adresse (voir
services/cadastre_service.py), puisqu'il ne dépend que de la géométrie, pas
du numéro — nécessaire pour les parcelles sans adresse, qui n'ont pas de
pair/impair à exploiter.

Aucune dépendance géospatiale externe (shapely, etc.) — voir le choix
technique déjà documenté dans README.md : la géométrie de rue (PICC) et des
parcelles (CADMAP/ICAR) est simple (polylignes/polygones en coordonnées
planes Lambert 72), une projection point-sur-segment classique suffit.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

Segment = Tuple[float, float, float, float, float]  # x1, y1, x2, y2, distance_cumulee_avant_ce_segment


def construire_segments(troncons: Sequence[Dict[str, Any]]) -> List[Segment]:
    """Aplatit les tronçons PICC (une ou plusieurs polylignes) en une seule
    liste de segments, avec la distance cumulée parcourue avant chaque
    segment — sert de repère linéaire unique pour toute la rue, même
    composée de plusieurs tronçons (ex: coupée par un carrefour)."""
    segments: List[Segment] = []
    distance_cumulee = 0.0
    for troncon in troncons:
        for chemin in (troncon.get("geometry") or {}).get("paths", []):
            for i in range(len(chemin) - 1):
                x1, y1 = chemin[i]
                x2, y2 = chemin[i + 1]
                longueur = math.hypot(x2 - x1, y2 - y1)
                if longueur == 0:
                    continue
                segments.append((x1, y1, x2, y2, distance_cumulee))
                distance_cumulee += longueur
    return segments


def cote_et_position(x: float, y: float, segments: Sequence[Segment]) -> Optional[Tuple[str, float]]:
    """Côté ("G"/"D") et position (mètres depuis le début de la rue) du
    point (x, y) le plus proche, par rapport au segment le plus proche de
    la rue. `None` si la rue n'a aucun segment exploitable (tronçon PICC
    introuvable — voir l'appelant, qui garde alors le comportement
    précédent, sans côté/position, plutôt que de planter)."""
    meilleure_distance: Optional[float] = None
    meilleur_cote = "D"
    meilleure_position = 0.0

    for x1, y1, x2, y2, distance_avant in segments:
        dx, dy = x2 - x1, y2 - y1
        longueur_carre = dx * dx + dy * dy
        if longueur_carre == 0:
            continue
        px, py = x - x1, y - y1
        t = max(0.0, min(1.0, (px * dx + py * dy) / longueur_carre))
        proj_x, proj_y = x1 + t * dx, y1 + t * dy
        distance_perp = math.hypot(x - proj_x, y - proj_y)

        if meilleure_distance is None or distance_perp < meilleure_distance:
            meilleure_distance = distance_perp
            longueur = math.sqrt(longueur_carre)
            # Produit vectoriel (dx,dy) x (px,py) : signe positif = point à
            # gauche du segment en avançant de (x1,y1) vers (x2,y2).
            cote_signe = dx * py - dy * px
            meilleur_cote = "G" if cote_signe > 0 else "D"
            meilleure_position = distance_avant + t * longueur

    if meilleure_distance is None:
        return None
    return meilleur_cote, meilleure_position
