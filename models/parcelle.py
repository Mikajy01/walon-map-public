"""Modèle représentant une parcelle à traiter (une ligne du fichier de sortie)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Parcelle:
    """Une adresse/parcelle identifiée pour une rue d'une commune.

    `geometry` est la géométrie ArcGIS (dict JSON, ex: polygone en Lambert
    72 / EPSG:31370) de la parcelle cadastrale, utilisée pour toutes les
    requêtes d'intersection spatiale sur les autres couches.
    """

    pays: str
    code_postal: str
    commune: str
    rue: str
    numero: Optional[str] = None
    adr_id: Optional[str] = None
    numero_cadastral: Optional[str] = None
    capakey: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    geometry: Optional[Dict[str, Any]] = None

    # Côté ("G"/"D") et position (mètres depuis le début du tracé) le long
    # de la rue — voir utils/geometrie.py. Sert au tri de sortie (toutes
    # les parcelles d'un côté, dans l'ordre où on les croise en marchant,
    # puis celles de l'autre côté), calculé pareil pour une parcelle avec
    # ou sans adresse (indépendant du numéro pair/impair).
    cote: Optional[str] = None
    position_rue: Optional[float] = None

    # Résultats calculés : {lettre_colonne: valeur}
    valeurs: Dict[str, str] = field(default_factory=dict)

    @property
    def identifiant(self) -> str:
        """Identifiant stable utilisé pour le cache et la reprise après interruption.

        Construit à partir de champs connus dès la découverte — jamais à
        partir de `numero_cadastral` seul pour les parcelles AVEC adresse
        ICAR (adr_id), qui n'est connu qu'après rattachement cadastral (un
        appel réseau coûteux) : ça permet de savoir qu'une telle parcelle a
        déjà été traitée AVANT de faire ce rattachement (voir main.py, mode
        incrémental avec --limit).

        Les parcelles SANS adresse ICAR (découvertes directement via
        CADMAP le long du tracé de la rue — voir
        CadastreService._parcelles_cadastrales_sans_adresse, toutes avec
        `numero = "/"`) utilisent `capakey` à la place : c'est le seul
        identifiant réellement unique disponible pour elles (rue+"/"
        serait identique pour toutes les parcelles sans adresse d'une même
        rue), et il est déjà connu au moment de la découverte pour ce cas
        précis (pas de rattachement différé nécessaire)."""
        if self.adr_id:
            return f"{self.commune}|ICAR:{self.adr_id}"
        if self.capakey:
            return f"{self.commune}|CAPAKEY:{self.capakey}"
        return f"{self.commune}|{self.rue}|{self.numero or '/'}"
