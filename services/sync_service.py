"""Synchronisation d'un fichier Excel déjà rempli — et éventuellement
corrigé ou complété à la main — avec la progression enregistrée
(`ProgressStore`).

Deux cas traités :
1. Ligne déjà connue en base (reconnue par Rue + Numéro + Numéro
   cadastral) : seules les colonnes valant actuellement `"ERREUR"` sont
   corrigées si l'Excel a maintenant une vraie valeur (voir
   `services/layers_service.py`, qui gèle `"ERREUR"` pour toujours une fois
   la parcelle marquée traitée). Aucune autre colonne n'est jamais touchée,
   même si elle diffère de l'Excel — une frappe accidentelle de
   l'utilisateur ne doit jamais corrompre une donnée déjà correcte.
2. Ligne PAS encore en base (ex: l'utilisateur a ajouté une parcelle à la
   main dans l'Excel) : avant de l'ajouter, son existence réelle est
   vérifiée au registre ICAR (rue puis numéro) — jamais de donnée
   inventée à partir du seul texte de l'Excel. Un contrôle de redondance
   (identifiant réel recalculé via ICAR/CADMAP, pas seulement le texte
   Rue/Numéro/Cadastral) évite qu'une même parcelle déjà en base sous une
   orthographe ou un numéro cadastral légèrement différent ne soit
   dupliquée ; dans ce cas elle est traitée comme le cas 1 (correction des
   cellules ERREUR) plutôt qu'ajoutée une seconde fois. Les colonnes
   d'identification (A-F) d'une ligne réellement nouvelle sont toujours
   recalculées depuis les registres officiels (ICAR/CADMAP), jamais copiées
   du texte Excel ; les colonnes de données (G et suivantes) sont en
   revanche reprises telles quelles depuis l'Excel — c'est tout l'intérêt
   de cet import.

Aucun changement de schéma SQLite ni de la logique d'écriture/lecture
existante : compatible avec les anciens `http_cache.sqlite3` et les anciens
fichiers Excel de sortie.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from openpyxl.worksheet.worksheet import Worksheet

import config
from models.parcelle import Parcelle
from services.cache_service import ProgressStore
from services.cadastre_service import CadastreService
from services.excel_service import ExcelService
from utils.logger import get_logger
from utils.tri import cle_tri_parcelle

_logger = get_logger("services.sync_service")

# Colonnes formant la clé de correspondance entre une ligne Excel et une
# entrée de ProgressStore : toutes visibles dans le fichier, contrairement
# à l'identifiant interne (ICAR:{adr_id}). Rue + Numéro suffisent presque
# toujours ; le numéro cadastral départage les rares doublons (plusieurs
# adresses "/" sans numéro sur une même rue).
_CLE_COLONNES = ("D", "E", "F")


@dataclass
class RapportSynchronisation:
    lignes_excel: int = 0
    lignes_reconnues: int = 0
    cellules_corrigees: int = 0
    lignes_ajoutees: int = 0
    lignes_doublons: int = 0
    lignes_invalides: int = 0
    parcelles_en_base: int = 0

    def resume(self) -> str:
        msg = (
            f"{self.lignes_excel} ligne(s) lues dans l'Excel importé, "
            f"{self.lignes_reconnues} déjà connue(s) en base ({self.cellules_corrigees} "
            f"cellule(s) 'ERREUR' corrigée(s)), {self.lignes_ajoutees} nouvelle(s) "
            f"parcelle(s) ajoutée(s) à la base (existence vérifiée au registre ICAR), "
            f"{self.lignes_doublons} doublon(s) détecté(s) (déjà en base sous un "
            f"identifiant réel malgré un texte différent — traité(s) comme correction), "
            f"{self.lignes_invalides} ligne(s) invalide(s) (rue ou numéro introuvable au "
            f"registre ICAR — ignorée(s), aucune donnée inventée)."
        )
        if self.lignes_excel != self.parcelles_en_base:
            msg += (
                f" Pour information : {self.lignes_excel} ligne(s) dans l'Excel importé, "
                f"{self.parcelles_en_base} parcelle(s) en base au total pour cette commune "
                f"après synchronisation (nombres différents attendus si des lignes ont été "
                f"ajoutées, ignorées, ou si l'Excel ne couvrait qu'une partie de la commune)."
            )
        return msg


def _corriger_cellules_erreur(
    valeurs_base: Dict[str, str], ligne: Dict[str, str], column_order: List[str],
    identifiant: str, rapport: RapportSynchronisation,
) -> bool:
    """Corrige dans `valeurs_base` toute colonne valant `"ERREUR"` pour
    laquelle `ligne` (Excel) a une valeur différente et non vide. Renvoie
    True si au moins une cellule a changé."""
    changed = False
    for col in column_order:
        if valeurs_base.get(col) != "ERREUR":
            continue
        nouvelle_valeur = ligne.get(col, "")
        if nouvelle_valeur and nouvelle_valeur != "ERREUR":
            _logger.info(
                "Parcelle %s | colonne %s : correction manuelle reprise "
                "depuis l'Excel importé ('ERREUR' -> %r).", identifiant, col, nouvelle_valeur,
            )
            valeurs_base[col] = nouvelle_valeur
            changed = True
            rapport.cellules_corrigees += 1
    return changed


def synchroniser_depuis_excel(
    commune: str,
    pays: str,
    ws: Worksheet,
    excel_service: ExcelService,
    progress_store: ProgressStore,
    cadastre_service: CadastreService,
) -> RapportSynchronisation:
    """Synchronise `progress_store` à partir d'un Excel importé (voir
    docstring du module pour les deux cas traités)."""
    from main import _set_identification_columns  # import tardif : évite un cycle main <-> sync_service

    rapport = RapportSynchronisation()
    column_order = ["A", "B", "C", "D", "E", "F"] + list(config.COLUMN_RULES.keys())

    lignes_excel = excel_service.lire_donnees_existantes(ws, column_order)
    rapport.lignes_excel = len(lignes_excel)

    base = progress_store.all_for_commune(commune)
    index_textuel = {
        tuple(valeurs.get(c, "") for c in _CLE_COLONNES): identifiant
        for identifiant, valeurs in base.items()
    }

    rues_officielles = {r.lower(): r for r in cadastre_service.lister_rues(commune)}
    parcelles_icar_par_rue: Dict[str, List[Parcelle]] = {}
    identifiants_traites: set = set()

    for ligne in lignes_excel:
        cle = tuple(ligne.get(c, "") for c in _CLE_COLONNES)
        identifiant = index_textuel.get(cle)

        if identifiant is not None:
            # Ligne déjà connue (correspondance textuelle directe) : seules
            # les cellules ERREUR sont éventuellement corrigées.
            rapport.lignes_reconnues += 1
            if identifiant in identifiants_traites:
                rapport.lignes_doublons += 1
                continue
            identifiants_traites.add(identifiant)
            valeurs_base = base[identifiant]
            if _corriger_cellules_erreur(valeurs_base, ligne, column_order, identifiant, rapport):
                progress_store.set(commune, identifiant, valeurs_base)
            continue

        # Ligne non reconnue par (Rue, Numéro, Cadastral) : peut-être une
        # parcelle jamais traitée par l'outil, ajoutée à la main dans
        # l'Excel. Avant de l'accepter, on vérifie son existence réelle au
        # registre ICAR (jamais de donnée inventée à partir du seul texte).
        rue_excel = (ligne.get("D") or "").strip()
        numero_excel = (ligne.get("E") or "").strip() or "/"
        rue_officielle = rues_officielles.get(rue_excel.lower())
        if not rue_excel or rue_officielle is None:
            rapport.lignes_invalides += 1
            _logger.warning(
                "Ligne Excel ignorée : rue %r introuvable au registre ICAR pour la commune "
                "'%s'.", rue_excel, commune,
            )
            continue

        if rue_officielle not in parcelles_icar_par_rue:
            parcelles_icar_par_rue[rue_officielle] = cadastre_service.lister_parcelles(
                pays, commune, rue_officielle
            )
        parcelle_icar = next(
            (p for p in parcelles_icar_par_rue[rue_officielle] if (p.numero or "/") == numero_excel),
            None,
        )
        if parcelle_icar is None:
            rapport.lignes_invalides += 1
            _logger.warning(
                "Ligne Excel ignorée : numéro %r introuvable au registre ICAR pour la rue "
                "'%s' (commune '%s').", numero_excel, rue_officielle, commune,
            )
            continue

        # Rattachement cadastral : donne le VRAI numéro cadastral et la
        # géométrie (jamais ceux de l'Excel, potentiellement une frappe
        # erronée), et permet de calculer l'identifiant définitif pour le
        # contrôle de redondance ci-dessous.
        cadastre_service.rattacher_parcelle_cadastrale(parcelle_icar)
        identifiant_reel = parcelle_icar.identifiant

        if identifiant_reel in identifiants_traites:
            rapport.lignes_doublons += 1
            continue
        identifiants_traites.add(identifiant_reel)

        if identifiant_reel in base:
            # Déjà en base sous un identifiant réel, malgré un texte
            # (Rue/Numéro/Cadastral) différent de l'Excel — typiquement un
            # numéro cadastral orthographié différemment. Traité comme une
            # correction sur l'existant, jamais comme une ligne en double.
            rapport.lignes_doublons += 1
            valeurs_base = base[identifiant_reel]
            if _corriger_cellules_erreur(valeurs_base, ligne, column_order, identifiant_reel, rapport):
                progress_store.set(commune, identifiant_reel, valeurs_base)
            continue

        # Parcelle réellement nouvelle, existence confirmée : colonnes
        # d'identification (A-F) recalculées depuis les registres officiels,
        # colonnes de données reprises telles quelles depuis l'Excel.
        parcelle_icar.pays = pays
        _set_identification_columns(parcelle_icar)
        for col in config.COLUMN_RULES:
            parcelle_icar.valeurs[col] = ligne.get(col, "")
        progress_store.set(commune, identifiant_reel, parcelle_icar.valeurs)
        base[identifiant_reel] = parcelle_icar.valeurs
        rapport.lignes_ajoutees += 1
        _logger.info(
            "Parcelle %s (rue=%r, numéro=%r) : nouvelle ligne ajoutée à la base depuis "
            "l'Excel importé (existence vérifiée au registre ICAR).",
            identifiant_reel, rue_officielle, numero_excel,
        )

    rapport.parcelles_en_base = len(progress_store.all_for_commune(commune))
    _logger.info(rapport.resume())
    return rapport


def reecrire_excel_depuis_base(
    commune: str,
    excel_service: ExcelService,
    progress_store: ProgressStore,
    output_path: Path,
    tentatives: int = 3,
    delai_secondes: float = 0.7,
) -> None:
    """Régénère le fichier de sortie (sur place, même chemin que l'import)
    à partir de l'état actuel de la base — pour que l'utilisateur voie
    immédiatement les corrections sans relancer un traitement complet.

    La correction en base (`synchroniser_depuis_excel`) est déjà faite
    avant cet appel : si l'écriture échoue ici (typiquement le fichier
    encore ouvert dans Excel — `PermissionError` sur Windows), les
    corrections ne sont PAS perdues, seule la réécriture du fichier l'est.
    Quelques réessais brefs absorbent un verrou transitoire (antivirus,
    indexation) ; au-delà, l'exception remonte à l'appelant (voir
    `gui.py::_executer_synchronisation`, qui distingue ce cas pour
    afficher un message clair plutôt que l'erreur brute)."""
    toutes_valeurs = list(progress_store.all_for_commune(commune).values())
    toutes_valeurs.sort(key=cle_tri_parcelle)

    wb = excel_service.load_output_workbook()
    ws = excel_service.get_active_sheet(wb)
    column_order = ["A", "B", "C", "D", "E", "F"] + list(config.COLUMN_RULES.keys())
    excel_service.write_parcelles(ws, toutes_valeurs, column_order)

    for tentative in range(1, tentatives + 1):
        try:
            excel_service.save(wb, output_path)
            return
        except PermissionError:
            if tentative == tentatives:
                raise
            _logger.warning(
                "Impossible d'écrire '%s' (probablement ouvert dans un autre programme) "
                "— nouvel essai %d/%d dans %.1fs...",
                output_path, tentative + 1, tentatives, delai_secondes,
            )
            time.sleep(delai_secondes)
