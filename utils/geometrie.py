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
Chemin = List[Sequence[float]]

# Distance en dessous de laquelle deux extrémités de tronçons PICC sont
# considérées comme le même point (un vrai raccord) — les raccords réels
# observés en conditions réelles sont à <1m (imprécision de digitalisation),
# largement sous ce seuil ; un vrai tronçon disjoint (rue interrompue) en
# serait bien plus loin.
_TOLERANCE_RACCORD_M = 2.0


def _distance(p1: Sequence[float], p2: Sequence[float]) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def _rechainer_chemins(chemins: Sequence[Chemin]) -> List[Chemin]:
    """Réordonne et réoriente une liste de chemins (chacun une polyligne
    [[x,y], ...]) pour qu'ils s'enchaînent bout à bout de façon continue et
    cohérente — nécessaire car une rue est souvent découpée en plusieurs
    tronçons PICC indépendants (ex: coupés à chaque carrefour), renvoyés par
    le service dans un ordre quelconque et parfois digitalisés en sens
    opposés. Sans ce recollement, concaténer les tronçons tels quels produit
    des positions qui font des sauts incohérents, et un côté (gauche/droite,
    calculé sur la direction locale de chaque tronçon) qui peut s'inverser
    d'un tronçon à l'autre alors que c'est géométriquement le même côté
    physique de la rue (cas réel observé : Avenue Jules Sartieaux, Dour, 3
    tronçons dont deux qu'il fallait inverser pour se raccorder correctement
    au troisième).

    Algorithme glouton : part d'un chemin (si possible un dont une
    extrémité n'a aucun voisin proche parmi les autres — donc un vrai bout
    de la rue, pour ancrer la position "0" dessus plutôt que sur un point
    de raccord arbitraire), puis cherche à chaque étape parmi les chemins
    restants celui dont une extrémité est la plus proche de la fin du
    chemin courant (à moins de `_TOLERANCE_RACCORD_M`), l'ajoute (inversé
    si c'est sa fin qui se raccorde), recommence. Un chemin sans aucun
    voisin proche démarre une nouvelle chaîne accolée à la suite plutôt que
    de planter — une rue peut être réellement interrompue."""
    if len(chemins) <= 1:
        return list(chemins)

    def _plus_proche_voisin(point: Sequence[float], exclure: int) -> Optional[float]:
        meilleure: Optional[float] = None
        for j, c in enumerate(chemins):
            if j == exclure:
                continue
            for extremite in (c[0], c[-1]):
                d = _distance(point, extremite)
                if meilleure is None or d < meilleure:
                    meilleure = d
        return meilleure

    restants = list(range(len(chemins)))
    depart = 0
    inverser_depart = False
    for i in range(len(chemins)):
        d_debut = _plus_proche_voisin(chemins[i][0], i)
        d_fin = _plus_proche_voisin(chemins[i][-1], i)
        debut_isole = d_debut is None or d_debut > _TOLERANCE_RACCORD_M
        fin_isolee = d_fin is None or d_fin > _TOLERANCE_RACCORD_M
        if debut_isole and not fin_isolee:
            depart, inverser_depart = i, False
            break
        if fin_isolee and not debut_isole:
            depart, inverser_depart = i, True
            break

    restants.remove(depart)
    premier = list(reversed(chemins[depart])) if inverser_depart else list(chemins[depart])
    chaine: List[Chemin] = [premier]

    while restants:
        fin_actuelle = chaine[-1][-1]
        meilleur_idx = None
        meilleure_distance = None
        meilleur_inverser = False
        for pos, j in enumerate(restants):
            d_debut = _distance(fin_actuelle, chemins[j][0])
            if meilleure_distance is None or d_debut < meilleure_distance:
                meilleure_distance, meilleur_idx, meilleur_inverser = d_debut, pos, False
            d_fin = _distance(fin_actuelle, chemins[j][-1])
            if d_fin < meilleure_distance:
                meilleure_distance, meilleur_idx, meilleur_inverser = d_fin, pos, True
        j = restants.pop(meilleur_idx)
        suivant = list(reversed(chemins[j])) if meilleur_inverser else list(chemins[j])
        chaine.append(suivant)

    return chaine


def _segments_depuis_chemins(chemins: Sequence[Chemin]) -> List[Segment]:
    """Aplatit une liste de chemins déjà dans le bon ordre/sens en une
    liste de segments avec distance cumulée — partie commune à
    `construire_segments` et à `_reorienter_vers_plus_petit_numero` (qui a
    besoin de mesurer une position AVANT de décider s'il faut retourner la
    chaîne)."""
    segments: List[Segment] = []
    distance_cumulee = 0.0
    for chemin in chemins:
        for i in range(len(chemin) - 1):
            x1, y1 = chemin[i]
            x2, y2 = chemin[i + 1]
            longueur = math.hypot(x2 - x1, y2 - y1)
            if longueur == 0:
                continue
            segments.append((x1, y1, x2, y2, distance_cumulee))
            distance_cumulee += longueur
    return segments


def _reorienter_vers_plus_petit_numero(
    chemins: List[Chemin], point_reference: Optional[Tuple[float, float]],
) -> List[Chemin]:
    """Retourne la chaîne de chemins (déjà recollée dans le bon ordre/sens
    par `_rechainer_chemins`) si besoin, pour que `point_reference` (le
    point de la parcelle au numéro de rue connu le plus bas — voir
    l'appelant) tombe dans la première moitié de la rue plutôt que la
    seconde. Sans ça, la position "0" reste celle du sens de digitalisation
    PICC brut, qui ne correspond à rien de significatif pour l'utilisateur
    (cas réel : Avenue Hyacinthe Harmegnies, tronçon unique donc rien à
    recoller, mais la position 0 tombait à ~200m du numéro 11 — sans doute
    vers l'autre bout de la rue). Sans effet si `point_reference` est
    `None` (aucun numéro connu pour cette rue pour le moment) ou si le
    tracé est vide."""
    if point_reference is None:
        return chemins
    segments = _segments_depuis_chemins(chemins)
    if not segments:
        return chemins
    dernier = segments[-1]
    longueur_totale = dernier[4] + math.hypot(dernier[2] - dernier[0], dernier[3] - dernier[1])
    resultat = cote_et_position(point_reference[0], point_reference[1], segments)
    if resultat is None:
        return chemins
    _, position = resultat
    if position <= longueur_totale / 2:
        return chemins
    return [list(reversed(chemin)) for chemin in reversed(chemins)]


def construire_segments(
    troncons: Sequence[Dict[str, Any]],
    point_reference: Optional[Tuple[float, float]] = None,
) -> List[Segment]:
    """Aplatit les tronçons PICC (une ou plusieurs polylignes) en une seule
    liste de segments, avec la distance cumulée parcourue avant chaque
    segment — sert de repère linéaire unique pour toute la rue, même
    composée de plusieurs tronçons (ex: coupée par un carrefour). Les
    chemins sont d'abord recollés dans le bon ordre/sens (voir
    `_rechainer_chemins`) avant d'être aplatis.

    `point_reference` (x, y de la parcelle au numéro de rue connu le plus
    bas, si disponible) permet en plus d'ancrer la position "0" du bon côté
    de la rue plutôt que de dépendre du sens de digitalisation PICC brut
    (voir `_reorienter_vers_plus_petit_numero`) — omis (`None`) pour les
    usages qui ne se soucient pas de l'orientation globale (ex: distance
    minimale à une rue pour départager la rue la plus proche, insensible à
    l'orientation)."""
    chemins = [
        chemin
        for troncon in troncons
        for chemin in (troncon.get("geometry") or {}).get("paths", [])
        if len(chemin) >= 2
    ]
    chemins = _rechainer_chemins(chemins)
    chemins = _reorienter_vers_plus_petit_numero(chemins, point_reference)
    return _segments_depuis_chemins(chemins)


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


def _distance_point_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Distance minimale entre un point et un segment [x1,y1]-[x2,y2]
    (projection bornée, comme dans `cote_et_position` — factorisé ici pour
    être réutilisé par `distance_min_polygone_rue`)."""
    dx, dy = x2 - x1, y2 - y1
    longueur_carre = dx * dx + dy * dy
    if longueur_carre == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / longueur_carre))
    proj_x, proj_y = x1 + t * dx, y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def distance_min_polygone_rue(
    rings: Sequence[Sequence[Sequence[float]]], segments: Sequence[Segment],
) -> Optional[float]:
    """Distance minimale entre le CONTOUR d'un polygone cadastral (chaque
    côté, pas seulement son centre) et le tracé d'une rue — sert à
    départager, pour une parcelle sans adresse trouvée candidate sur
    PLUSIEURS rues à la fois (cas réel : parcelle 91S2, Dour — bordant à la
    fois Avenue Hyacinthe Harmegnies et une rue voisine dans la marge de
    recherche des deux), laquelle est réellement la plus proche (voir
    main.py::recalculer_cote_position). Plus fiable qu'une distance au seul
    centre du polygone (déjà utilisé pour côté/position, mais insuffisant
    ici) : un côté du polygone proche d'une rue compte pleinement, même si
    le centre de la parcelle en est loin (parcelle allongée ou irrégulière).

    Distance segment-à-segment classique (minimum des 4 distances
    point-à-segment entre les extrémités de chaque paire de segments) —
    correcte tant que les deux segments ne se croisent pas, ce qui n'arrive
    jamais ici (une parcelle ne chevauche pas le tracé d'une rue).
    `None` si le polygone ou la rue n'a aucun côté/segment exploitable."""
    meilleure: Optional[float] = None
    for ring in rings:
        n = len(ring)
        if n < 2:
            continue
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            for sx1, sy1, sx2, sy2, _ in segments:
                d = min(
                    _distance_point_segment(x1, y1, sx1, sy1, sx2, sy2),
                    _distance_point_segment(x2, y2, sx1, sy1, sx2, sy2),
                    _distance_point_segment(sx1, sy1, x1, y1, x2, y2),
                    _distance_point_segment(sx2, sy2, x1, y1, x2, y2),
                )
                if meilleure is None or d < meilleure:
                    meilleure = d
    return meilleure
