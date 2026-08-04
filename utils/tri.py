"""Tri des lignes du fichier de sortie par rue puis numéro cadastral."""

from __future__ import annotations

import re
from typing import Dict

# RADICAL (chiffres) + BIS optionnel ("/chiffres") + EXPOSANT (lettres) +
# PUISSANCE optionnelle (chiffres) — reflète exactement la construction du
# numéro cadastral dans CadastreService._format_numero_cadastral (ex:
# "14H2", "919W", "670/3R", "2075/2A").
_MOTIF_NUMERO_CADASTRAL = re.compile(r"^(\d+)(?:/(\d+))?([A-Za-z]*)(\d*)$")


def _cle_numero_cadastral(numero_cadastral: str) -> tuple:
    """Clé de tri numérique pour un numéro cadastral (radical, bis,
    exposant, puissance) — pas un tri texte, qui donnerait par exemple
    "1..." avant "14..." avant "2...", mélangeant les radicaux au lieu de
    les ordonner 1, 2, 3, ..., 14 (observé en conditions réelles, réclamé
    par un relecteur client : "tous les numéros cadastraux sont dans le
    désordre" — commune Colfontaine). Une valeur qui ne correspond pas au
    format attendu (ex: "/", rattachement cadastral introuvable) est
    triée après toutes les valeurs reconnues, par texte entre elles."""
    m = _MOTIF_NUMERO_CADASTRAL.match(numero_cadastral or "")
    if not m:
        return (1, 0, 0, "", 0, numero_cadastral or "")
    radical, bis, exposant, puissance = m.groups()
    return (
        0,
        int(radical),
        int(bis) if bis else 0,
        exposant or "",
        int(puissance) if puissance else 0,
        "",
    )


def cle_tri_parcelle(valeurs: Dict[str, str]) -> tuple:
    """Clé de tri (rue, numéro cadastral) — voir `_cle_numero_cadastral`.
    Le numéro cadastral (colonne F), pas le numéro d'adresse (colonne E),
    est le critère de tri : c'est celui que le document Word et le
    relecteur utilisent pour vérifier le travail, et c'est le seul critère
    disponible pour les parcelles sans adresse (numéro = "/", conformes à
    la règle métier — voir CadastreService)."""
    rue = valeurs.get("D", "")
    return (rue, *_cle_numero_cadastral(valeurs.get("F", "")))
