"""Tri des lignes du fichier de sortie : d'abord par code postal, puis par
rue, puis par côté (toutes les parcelles d'un côté de la rue, dans l'ordre
où on les croise en marchant, puis toutes celles de l'autre côté) — voir
utils/geometrie.py.

Le tri par rue/côté est demandé par le client à la place d'un tri par
simple numéro : plus fidèle à une vérification manuelle sur le terrain, et
fonctionne aussi bien pour les parcelles sans adresse (numéro = "/",
conformes à la règle métier du document Word) que pour celles avec
adresse, puisque le côté/la position sont calculés géométriquement,
indépendamment du numéro pair/impair.

Le regroupement par code postal en premier sert les communes fusionnées
couvrant plusieurs codes postaux (ex: Comines-Warneton, 7780-7784) : sans
lui, les rues de codes postaux différents s'entremêlent dans l'ordre
alphabétique (une rue "7780" entre deux rues "7781"), rendant impossible
de vérifier un code postal à la fois même en les traitant un par un — le
tri final ne reflétait jamais l'ordre de traitement. Ne change rien à
l'intérieur d'un même code postal : le tri par rue/côté/position déjà
demandé par le client y reste exactement identique.
"""

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
    exposant, puissance) — pas un tri texte. Sert de repli pour les lignes
    traitées avant l'ajout du calcul côté/position (voir `cle_tri_parcelle`),
    et de repli final pour départager deux parcelles au même côté/position
    exacts (rare)."""
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
    """Clé de tri (code postal, rue, côté, position). `_cote`/`_position`
    (colonnes internes, jamais écrites dans l'Excel — voir main.py::
    _set_identification_columns) ne sont disponibles que pour les
    parcelles traitées après l'ajout de ce calcul ; une ligne qui ne les a
    pas encore (à recalculer — voir main.py::recalculer_cote_position) est
    triée après toutes celles qui les ont, par numéro cadastral entre
    elles comme avant. Toutes les clés renvoyées ont la même forme
    (comparable entre elles sans erreur de type), qu'une ligne ait ou non
    déjà son côté/position."""
    code_postal = valeurs.get("B", "")
    rue = valeurs.get("D", "")
    cote = valeurs.get("_cote")
    position = valeurs.get("_position")

    a_cote_position = cote is not None and position is not None
    groupe = 0 if a_cote_position else 1
    cle_cadastral = _cle_numero_cadastral(valeurs.get("F", "")) if not a_cote_position else (0, 0, 0, "", 0, "")

    return (
        code_postal,
        rue,
        groupe,
        cote if cote is not None else "",
        float(position) if position is not None else 0.0,
        *cle_cadastral,
    )
