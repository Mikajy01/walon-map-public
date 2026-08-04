"""Point d'entrée : remplit le fichier Excel Walonmap pour une ou plusieurs
communes à partir des données du Géoportail de Wallonie.

Le traitement est pensé pour s'étaler sur plusieurs exécutions (le travail
manuel équivalent prenait un mois) : chaque commune a un fichier de sortie
stable (`output/<commune>.xlsx`, pas d'horodatage) qui s'enrichit au fil des
exécutions. `--limit` plafonne le nombre de *nouvelles* parcelles résolues à
cette exécution ; les parcelles déjà résolues lors d'exécutions précédentes
sont relues depuis la reprise (ProgressStore) sans être ni recalculées ni
dupliquées dans le fichier de sortie.

Usage :
    python main.py --commune Crisnée --limit 50
    python main.py --commune Crisnée   # sans limite : traite tout ce qui reste
    python main.py --commune Crisnée --commune Awans --debug
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

import config
from models.parcelle import Parcelle
from services.cache_service import CacheService, ProgressStore
from services.cadastre_service import CadastreService
from services.excel_service import ExcelService
from services.geoportail_service import ArcGISRestClient
from services.layers_service import LayersService
from utils.geometrie import construire_segments, cote_et_position
from utils.logger import get_logger, setup_logging
from utils.progress import progress
from utils.rate_limiter import RateLimiter
from utils.tri import cle_tri_parcelle

_logger = get_logger("main")

# (phase, actuel, total) -> None ; utilisé par gui.py pour piloter une barre
# de progression, sans que main.py dépende d'une bibliothèque UI.
ProgressCallback = Callable[[str, int, int], None]


@dataclass
class ResultatTraitement:
    """Résultat d'un appel à `traiter_commune` — permet à l'appelant (CLI ou
    GUI) de savoir s'il reste des adresses à traiter, sans avoir à relire
    les logs. `total_adresses`/`restantes` sont relatifs au périmètre de
    cette exécution (toute la commune, ou seulement le(s) code(s) postal
    (aux) filtré(s) si `--code-postal` était utilisé — voir `filtre_actif`)."""

    output_path: Path
    total_adresses: int
    parcelles_resolues: int
    restantes: int
    filtre_actif: bool

    @property
    def termine(self) -> bool:
        """True si plus aucune adresse ne reste à traiter dans le périmètre
        de cette exécution (toute la commune si `filtre_actif` est False)."""
        return self.restantes == 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remplit le fichier Excel Walonmap à partir du Géoportail de Wallonie."
    )
    parser.add_argument(
        "--commune", action="append", required=True, dest="communes",
        help="Nom de commune à traiter (répéter l'option pour en traiter plusieurs).",
    )
    parser.add_argument("--pays", default="Belgique", help="Valeur de la colonne Pays (par défaut : Belgique).")
    parser.add_argument(
        "--code-postal", action="append", dest="codes_postaux",
        help="Limite le traitement à ce(s) code(s) postal/postaux au sein de la commune "
             "(répéter l'option pour en spécifier plusieurs). Utile car une commune "
             "fusionnée peut couvrir plusieurs codes postaux. Omettre pour traiter "
             "tous les codes postaux de la commune.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Nombre maximum de NOUVELLES parcelles à résoudre à cette exécution "
             "(les parcelles déjà traitées lors d'exécutions précédentes ne comptent pas "
             "et sont conservées). Omettre pour traiter tout ce qui reste.",
    )
    parser.add_argument(
        "--rate-limit", type=float, default=config.MAX_REQUESTS_PER_SECOND,
        help=f"Nombre maximum de requêtes par seconde vers le Géoportail (défaut : "
             f"{config.MAX_REQUESTS_PER_SECOND}). Augmenter accélère le traitement mais "
             f"augmente le risque d'erreurs transitoires du serveur (déjà gérées par réessai).",
    )
    parser.add_argument("--debug", action="store_true", help="Active le mode DEBUG (journalisation détaillée).")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Désactive le cache HTTP local (déconseillé : perte de la reprise après interruption).",
    )
    parser.add_argument(
        "--template", type=Path, default=None,
        help="Chemin du fichier Excel gabarit (défaut : config.TEMPLATE_PATH, "
             "'<dossier parent>/Entête walonmap (avec colonnes en plus).xlsx'). "
             "Utile quand ce chemin par défaut n'existe pas (ex: exécution dans "
             "un dépôt Git / CI, où seul le contenu du dépôt est disponible).",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=None,
        help="Dossier de cache/reprise, si différent de config.CACHE_DIR (défaut). "
             "Utile pour isoler la base de progression d'une commune dans son propre "
             "fichier (ex: '--cache-dir cache/Chimay') — permet à plusieurs exécutions "
             "de communes différentes de tourner en parallèle (ex: GitHub Actions) sans "
             "jamais écrire dans le même fichier SQLite, que Git ne peut pas fusionner.",
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=None,
        help="Dossier de logs, si différent de config.LOGS_DIR (défaut). Même usage "
             "que --cache-dir : isoler le fichier de log par commune pour des exécutions "
             "en parallèle (ex: '--logs-dir logs/Chimay').",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Dossier de sortie, si différent de config.OUTPUT_DIR (défaut). Utile "
             "quand le code et les données vivent dans deux dépôts Git séparés (ex: "
             "dépôt de code public + dépôt de données privé en CI) : le fichier "
             "Excel doit alors être écrit dans le second, pas dans le checkout du "
             "premier.",
    )
    parser.add_argument(
        "--recalculer-cote-position", action="store_true",
        help="Rattrapage, au lieu du traitement normal : calcule côté/position "
             "(voir utils/geometrie.py) pour les parcelles déjà résolues avant "
             "l'ajout de ce calcul. À lancer une fois par commune (ex: workflow "
             "GitHub Actions dédié) — les parcelles traitées normalement l'ont "
             "déjà, --limit et --code-postal sont ignorés dans ce mode.",
    )
    return parser.parse_args()


def _chemin_archive_disponible(base_dir: Path, commune: str) -> Path:
    """Chemin d'une copie datée du fichier de sortie, jamais écrasée : une
    par exécution, numérotée en cas de collision le même jour (ex:
    `Colfontaine-02-08-2026.xlsx`, puis `Colfontaine-02-08-2026 2.xlsx` si
    déjà présent, `... 3.xlsx` ensuite, etc. — reprend la convention déjà
    utilisée manuellement). Sert d'archive/historique à côté du fichier
    stable `<commune>.xlsx` (toujours à jour, jamais daté, source de vérité
    pour le total réel — voir `traiter_commune`), pour ne pas recréer la
    confusion « quel fichier contient le vrai total » observée avec des
    fichiers uniquement datés."""
    date_str = date.today().strftime("%d-%m-%Y")
    candidat = base_dir / f"{commune}-{date_str}.xlsx"
    if not candidat.exists():
        return candidat
    n = 2
    while True:
        candidat = base_dir / f"{commune}-{date_str} {n}.xlsx"
        if not candidat.exists():
            return candidat
        n += 1


def _set_identification_columns(parcelle: Parcelle) -> None:
    parcelle.valeurs["A"] = parcelle.pays
    parcelle.valeurs["B"] = parcelle.code_postal or "/"
    parcelle.valeurs["C"] = parcelle.commune
    parcelle.valeurs["D"] = parcelle.rue
    parcelle.valeurs["E"] = parcelle.numero or "/"
    parcelle.valeurs["F"] = parcelle.numero_cadastral or "/"
    # Clés internes (préfixe "_", jamais une vraie lettre de colonne — voir
    # excel_service.write_parcelles, qui n'écrit que column_order, ignore
    # tout le reste) : côté/position le long de la rue, utilisées
    # uniquement pour le tri de sortie (voir utils/tri.py). Absentes pour
    # les parcelles traitées avant l'ajout de ce calcul — le tri s'y adapte.
    if parcelle.cote is not None:
        parcelle.valeurs["_cote"] = parcelle.cote
    if parcelle.position_rue is not None:
        parcelle.valeurs["_position"] = parcelle.position_rue


def traiter_commune(
    commune: str,
    pays: str,
    cadastre_service: CadastreService,
    layers_service: LayersService,
    progress_store: ProgressStore,
    excel_service: ExcelService,
    limit: Optional[int] = None,
    codes_postaux: Optional[List[str]] = None,
    output_path: Optional[Path] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> ResultatTraitement:
    """Traite une commune de façon incrémentale.

    La découverte se fait en deux temps, pour que `limit` plafonne
    vraiment TOUT le travail coûteux d'une exécution (pas seulement la
    résolution des ~90 couches métier) :

    1. Lister les adresses de chaque rue (ICAR, une requête par rue —
       toujours fait en entier, c'est bon marché) et vérifier, pour
       chacune, si elle est déjà dans `progress_store` (identifiant
       connu sans aucun appel réseau supplémentaire, voir
       `models.Parcelle.identifiant`).
    2. Ne faire le rattachement cadastral (1 requête réseau par adresse,
       coûteux) et la résolution des colonnes QUE pour les parcelles pas
       encore traitées, jusqu'à concurrence de `limit` — puis s'arrêter,
       y compris pour les rues restantes.

    Le fichier de sortie est reconstruit à chaque exécution à partir de
    TOUTES les parcelles déjà résolues (précédentes + nouvelles) : chaque
    parcelle n'y apparaît qu'une seule fois, jamais en double, et les
    parcelles pas encore résolues n'y figurent simplement pas encore.

    `codes_postaux`, si fourni, restreint le traitement aux adresses dont
    le code postal (lu par adresse via ICAR, voir `cadastre_service`) est
    dans cette liste — une commune fusionnée peut couvrir plusieurs codes
    postaux, et ce filtre permet de n'en traiter qu'un sous-ensemble. Il ne
    restreint QUE ce qui est nouvellement résolu à cette exécution : le
    fichier de sortie est toujours reconstruit à partir de la totalité des
    parcelles déjà résolues pour la commune (voir `progress_store.all_for_commune`
    plus bas), qu'elles aient été traitées avec ou sans filtre lors
    d'exécutions précédentes — un filtre restrictif ne fait donc jamais
    disparaître de lignes déjà produites.

    `output_path`, si fourni, remplace le nom de fichier automatique
    (`output/<commune>.xlsx`) — utilisé par l'interface graphique pour
    laisser l'utilisateur choisir l'emplacement et le nom du fichier.
    `on_progress(phase, actuel, total)` est appelé à chaque étape des deux
    boucles (découverte puis remplissage) si fourni — utilisé par
    l'interface graphique pour piloter une barre de progression sans que
    ce module dépende d'une bibliothèque UI.
    """
    _logger.info("=== Traitement de la commune : %s ===", commune)
    if codes_postaux:
        _logger.info("Filtré sur le(s) code(s) postal(aux) : %s", ", ".join(codes_postaux))

    # Une rue déjà connue (présente dans la progression enregistrée) mais
    # absente de la réponse ICAR à CET appel précis (incident transitoire
    # observé en conditions réelles — ex: "Rue Courteville" à Dour,
    # 195/199 autres rues correctement renvoyées le même jour) ne doit
    # jamais faire disparaître ses parcelles déjà traitées de la boucle de
    # découverte : sans ça, elles ne sont plus jamais "visitées", donc plus
    # jamais retentées (ERREUR) ni rafraîchies (AP/CL) tant que la rue ne
    # réapparaît pas par chance à une exécution future. On complète donc
    # toujours la liste ICAR avec les rues déjà vues dans la base.
    rues_icar = cadastre_service.lister_rues(commune)
    rues_connues = {v.get("D") for v in progress_store.all_for_commune(commune).values() if v.get("D")}
    rues_manquantes = rues_connues - set(rues_icar)
    if rues_manquantes:
        _logger.warning(
            "%d rue(s) déjà connue(s) pour '%s' absente(s) du registre ICAR à cette "
            "exécution (incident probablement transitoire) — conservée(s) quand même : %s.",
            len(rues_manquantes), commune, ", ".join(sorted(rues_manquantes)),
        )
    rues = sorted(set(rues_icar) | rues_connues)
    if not rues:
        _logger.warning("Aucune rue trouvée pour la commune '%s' (registre ICAR).", commune)

    deja_faites: List[Parcelle] = []
    a_faire: List[Parcelle] = []
    total_adresses = 0
    parcelles_retentees = 0
    cellules_corrigees = 0
    for rue_index, rue in enumerate(
        progress(rues, total=len(rues), description=f"Découverte des adresses ({commune})")
    ):
        if on_progress:
            on_progress("découverte", rue_index + 1, len(rues))
        try:
            parcelles = cadastre_service.lister_parcelles(pays, commune, rue, codes_postaux=codes_postaux)
        except Exception:  # noqa: BLE001
            _logger.exception("Échec de découverte des adresses pour la rue '%s', rue ignorée.", rue)
            continue
        if codes_postaux is not None:
            parcelles = [p for p in parcelles if p.code_postal in codes_postaux]
        total_adresses += len(parcelles)
        for parcelle in parcelles:
            cached = progress_store.get(commune, parcelle.identifiant)
            if cached is not None:
                parcelle.valeurs = cached
                changed = False
                # Une valeur "ERREUR" ne reflète pas une donnée réelle mais
                # un échec de résolution (souvent un incident transitoire
                # côté serveur, voir LayersService.retry_erreurs) : on la
                # retente à chaque exécution plutôt que de la figer pour
                # toujours. Coût limité au rattachement cadastral (1
                # requête) + aux seules colonnes en erreur, pas toute la
                # parcelle — négligeable tant que le nombre de parcelles en
                # erreur reste faible devant le total de la commune.
                if any(v == "ERREUR" for v in cached.values()):
                    parcelles_retentees += 1
                    try:
                        cadastre_service.rattacher_parcelle_cadastrale(parcelle)
                        corrigees = layers_service.retry_erreurs(parcelle)
                    except Exception:  # noqa: BLE001
                        _logger.exception(
                            "Échec du rattachement cadastral pour la nouvelle tentative "
                            "des colonnes en erreur de la parcelle %s.", parcelle.identifiant,
                        )
                        corrigees = 0
                    if corrigees:
                        cellules_corrigees += corrigees
                        changed = True
                # AP/CL dépendent de data/liens_communaux.csv, modifiable
                # par l'utilisateur après coup — à revalider à chaque
                # exécution même pour une parcelle déjà traitée (gratuit,
                # aucun appel réseau), sinon un lien ajouté après coup
                # resterait bloqué sur "À COMPLÉTER MANUELLEMENT".
                if layers_service.refresh_lookup_links(parcelle):
                    changed = True
                if changed:
                    progress_store.set(commune, parcelle.identifiant, parcelle.valeurs)
                deja_faites.append(parcelle)
            else:
                a_faire.append(parcelle)

    objectif = limit if limit is not None else len(a_faire)
    _logger.info(
        "Commune '%s' : %d parcelle(s) au total, %d déjà traitée(s), "
        "objectif de %d nouvelle(s) parcelle(s) réussie(s) à cette exécution%s.",
        commune, total_adresses, len(deja_faites), min(objectif, len(a_faire)),
        "" if limit is None else f" (limite={limit})",
    )
    if parcelles_retentees:
        _logger.info(
            "Commune '%s' : %d parcelle(s) déjà traitée(s) avaient au moins une cellule "
            "'ERREUR', %d cellule(s) corrigée(s) au nouvel essai.",
            commune, parcelles_retentees, cellules_corrigees,
        )

    # Une parcelle qui échoue (`Exception` inattendue, ex: incident réseau
    # après épuisement des réessais) est remplacée par la suivante dans la
    # file plutôt que de faire simplement baisser le total de cette
    # exécution : sans ça, `--limit 550` avec 18 échecs donnait 532
    # nouvelles parcelles au lieu de 550 (observé en conditions réelles),
    # même si la parcelle manquée est de toute façon retentée un jour
    # (jamais enregistrée, donc redécouverte au prochain lancement — voir
    # plus haut) — autant ne pas attendre une exécution de plus quand il
    # reste des parcelles disponibles pour combler l'écart tout de suite.
    #
    # La compensation est bornée à 2x l'objectif plutôt qu'illimitée
    # (jusqu'à épuisement de `a_faire`) : un run GitHub Actions est
    # plafonné à 6h par GitHub (voir timeout-minutes dans le workflow),
    # indépendamment du quota de minutes/mois (qui lui ne s'applique plus,
    # dépôt public) — un taux d'échec anormalement élevé (ex: panne réseau
    # partielle côté Géoportail) ne doit pas faire tenter la totalité de la
    # file d'une commune en un seul run et risquer de ne rien terminer
    # avant la coupure. 2x couvre largement un taux d'échec réaliste (18
    # échecs sur 550, soit ~3%, dans le cas observé ci-dessus) sans risque
    # d'emballement ; aucune parcelle n'est perdue si le plafond est
    # atteint avant l'objectif, elle est simplement retentée au run
    # suivant (jamais enregistrée tant qu'elle n'a pas réussi).
    nouvellement_traitees: List[Parcelle] = []
    parcelles_tentees = 0
    cible = min(objectif, len(a_faire))
    plafond_tentatives = min(len(a_faire), objectif * 2)

    def _traiter_jusqu_a_objectif():
        nonlocal parcelles_tentees
        for parcelle in a_faire:
            if len(nouvellement_traitees) >= objectif or parcelles_tentees >= plafond_tentatives:
                return
            parcelles_tentees += 1
            try:
                cadastre_service.rattacher_parcelle_cadastrale(parcelle)
                _set_identification_columns(parcelle)
                layers_service.resolve_all(parcelle)
            except Exception:  # noqa: BLE001 - ne jamais interrompre le traitement des autres parcelles
                _logger.exception("Échec inattendu pour la parcelle %s, ligne ignorée.", parcelle.identifiant)
                continue
            progress_store.set(commune, parcelle.identifiant, parcelle.valeurs)
            nouvellement_traitees.append(parcelle)
            yield parcelle

    for parcelle_index, _ in enumerate(
        progress(_traiter_jusqu_a_objectif(), total=cible, description=f"Remplissage ({commune})")
    ):
        if on_progress:
            on_progress("remplissage", parcelle_index + 1, cible)

    if parcelles_tentees > len(nouvellement_traitees):
        _logger.info(
            "Commune '%s' : %d parcelle(s) tentée(s) pour obtenir %d nouvelle(s) réussie(s) "
            "(%d échec(s) remplacé(s) automatiquement).",
            commune, parcelles_tentees, len(nouvellement_traitees),
            parcelles_tentees - len(nouvellement_traitees),
        )
        if len(nouvellement_traitees) < objectif and parcelles_tentees >= plafond_tentatives:
            _logger.warning(
                "Commune '%s' : plafond de compensation (%d tentative(s)) atteint avant d'obtenir "
                "les %d nouvelle(s) parcelle(s) demandée(s) — taux d'échec anormalement élevé "
                "sur cette exécution, %d obtenue(s) seulement. Le reste sera retenté au prochain "
                "lancement.", commune, plafond_tentatives, objectif, len(nouvellement_traitees),
            )

    parcelles_resolues = deja_faites + nouvellement_traitees
    restantes = total_adresses - len(parcelles_resolues)
    _logger.info(
        "Commune '%s' : %d/%d parcelle(s) résolue(s) au total (%d restante(s))%s.",
        commune, len(parcelles_resolues), total_adresses, restantes,
        "" if not codes_postaux else f" [dans le filtre code(s) postal(aux) {codes_postaux}]",
    )

    # Le fichier de sortie est TOUJOURS reconstruit à partir de la totalité
    # des parcelles déjà résolues pour la commune (indépendamment du filtre
    # --code-postal éventuel de cette exécution), pour ne jamais faire
    # disparaître du fichier des lignes résolues lors d'exécutions
    # précédentes avec un filtre différent (ou sans filtre).
    final_output_path = _reconstruire_fichier_sortie(commune, progress_store, excel_service, output_path)
    return ResultatTraitement(
        output_path=final_output_path,
        total_adresses=total_adresses,
        parcelles_resolues=len(parcelles_resolues),
        restantes=restantes,
        filtre_actif=bool(codes_postaux),
    )


def _reconstruire_fichier_sortie(
    commune: str,
    progress_store: ProgressStore,
    excel_service: ExcelService,
    output_path: Optional[Path] = None,
) -> Path:
    """Reconstruit le fichier Excel de sortie à partir de la totalité des
    parcelles déjà résolues pour la commune — appelé par `traiter_commune`
    après chaque exécution, et par `recalculer_cote_position` après avoir
    mis à jour côté/position sur des parcelles déjà résolues (même besoin :
    relire `progress_store`, trier, réécrire)."""
    toutes_valeurs_commune = list(progress_store.all_for_commune(commune).values())
    toutes_valeurs_commune.sort(key=cle_tri_parcelle)

    wb = excel_service.load_output_workbook()
    ws = excel_service.get_active_sheet(wb)
    column_order = list(config.COLUMN_RULES.keys())
    all_columns = ["A", "B", "C", "D", "E", "F"] + column_order
    excel_service.write_parcelles(ws, toutes_valeurs_commune, all_columns)

    # `output_path is None` : appelant utilisant le chemin par défaut (CLI,
    # GitHub Actions) — une copie datée est en plus archivée à côté du
    # fichier stable. Le GUI fournit toujours son propre `output_path`
    # (choisi explicitement via « Enregistrer sous… ») : pas d'archive
    # automatique dans ce cas, l'utilisateur maîtrise déjà le nommage.
    utilise_chemin_par_defaut = output_path is None
    final_output_path = output_path or (config.OUTPUT_DIR / f"{commune}.xlsx")
    excel_service.save(wb, final_output_path)
    if utilise_chemin_par_defaut:
        chemin_archive = _chemin_archive_disponible(final_output_path.parent, commune)
        shutil.copy2(final_output_path, chemin_archive)
        _logger.info("Copie datée archivée : %s", chemin_archive)
    return final_output_path


def recalculer_cote_position(
    commune: str,
    cadastre_service: CadastreService,
    progress_store: ProgressStore,
    excel_service: ExcelService,
    output_path: Optional[Path] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> int:
    """Rattrapage pour les parcelles déjà résolues AVANT l'ajout du calcul
    côté/position (voir utils/geometrie.py) — les parcelles traitées par
    `traiter_commune` depuis cet ajout l'ont déjà (voir
    `_set_identification_columns`), inutile de les repasser ici.

    Regroupe les parcelles à corriger par rue pour ne récupérer le tracé
    PICC (`recuperer_troncons`) qu'une seule fois par rue plutôt qu'une
    fois par parcelle. Renvoie le nombre de parcelles effectivement
    corrigées (0 si la commune n'a rien à rattraper)."""
    base = progress_store.all_for_commune(commune)
    a_corriger: dict = {}
    for identifiant, valeurs in base.items():
        if "_cote" in valeurs and "_position" in valeurs:
            continue
        a_corriger.setdefault(valeurs.get("D", ""), []).append((identifiant, valeurs))

    total_corrigees = 0
    rues = list(a_corriger.items())
    for rue_index, (rue, entrees) in enumerate(rues):
        if on_progress:
            on_progress("recalcul_côté_position", rue_index + 1, len(rues))
        troncons = cadastre_service.recuperer_troncons(commune, rue)
        segments = construire_segments(troncons)
        if not segments:
            _logger.warning(
                "Rue '%s' (%s) : tracé PICC introuvable, côté/position non calculables "
                "pour %d parcelle(s).", rue, commune, len(entrees),
            )
            continue
        for identifiant, valeurs in entrees:
            point = cadastre_service.point_pour_identifiant(identifiant)
            if point is None:
                continue
            resultat = cote_et_position(point[0], point[1], segments)
            if resultat is None:
                continue
            cote, position = resultat
            valeurs["_cote"] = cote
            valeurs["_position"] = position
            progress_store.set(commune, identifiant, valeurs)
            # Les parcelles sans adresse ICAR (identifiant "...|CAPAKEY:...",
            # voir Parcelle.identifiant) ont aussi une entrée dans le cache
            # de découverte dédié (parcelles_sans_adresse) : à garder en
            # cohérence, sinon une future découverte sur la même rue
            # réécraserait cote/position avec les None d'origine.
            if "|CAPAKEY:" in identifiant:
                capakey = identifiant.rsplit("CAPAKEY:", 1)[-1]
                progress_store.maj_cote_position_sans_adresse(commune, rue, capakey, cote, position)
            total_corrigees += 1

    if total_corrigees:
        _reconstruire_fichier_sortie(commune, progress_store, excel_service, output_path)
        _logger.info(
            "Commune '%s' : %d parcelle(s) corrigée(s) (côté/position calculés rétroactivement).",
            commune, total_corrigees,
        )
    else:
        _logger.info(
            "Commune '%s' : rien à corriger (toutes les parcelles déjà résolues ont "
            "déjà côté/position).", commune,
        )
    return total_corrigees


def main() -> int:
    args = parse_args()
    config.DEBUG = args.debug
    setup_logging(args.logs_dir or config.LOGS_DIR, debug=config.DEBUG)

    _logger.info("Démarrage — communes : %s", ", ".join(args.communes))

    cache_dir = args.cache_dir or config.CACHE_DIR
    cache_service = CacheService(cache_dir)
    progress_store = ProgressStore(cache_dir)
    rate_limiter = RateLimiter(args.rate_limit)
    client = ArcGISRestClient(
        cache=cache_service,
        rate_limiter=rate_limiter,
        timeout=config.HTTP_TIMEOUT_SECONDS,
        use_cache=not args.no_cache,
    )
    cadastre_service = CadastreService(client, progress_store)
    layers_service = LayersService(client, config.LIENS_COMMUNAUX_PATH)
    excel_service = ExcelService(args.template or config.TEMPLATE_PATH)

    resultats: List[ResultatTraitement] = []
    recalcul_reussi = False
    for commune in args.communes:
        try:
            output_path = (args.output_dir / f"{commune}.xlsx") if args.output_dir else None
            if args.recalculer_cote_position:
                recalculer_cote_position(commune, cadastre_service, progress_store, excel_service, output_path=output_path)
                recalcul_reussi = True
                continue
            resultat = traiter_commune(
                commune, args.pays,
                cadastre_service, layers_service, progress_store, excel_service,
                limit=args.limit, codes_postaux=args.codes_postaux, output_path=output_path,
            )
            resultats.append(resultat)
        except Exception:  # noqa: BLE001 - une commune en erreur ne doit pas bloquer les suivantes
            _logger.exception("Échec du traitement de la commune '%s'.", commune)

    for resultat in resultats:
        _logger.info("Fichier généré : %s", resultat.output_path)
        if resultat.termine:
            _logger.info(
                "Commune terminée : plus aucune adresse à traiter%s.",
                "" if not resultat.filtre_actif else " dans le(s) code(s) postal(aux) filtré(s)",
            )
        else:
            _logger.info(
                "Commune non terminée : %d adresse(s) restante(s) sur %d — relancez la même "
                "commande pour continuer.", resultat.restantes, resultat.total_adresses,
            )

    return 0 if (resultats or recalcul_reussi) else 1


if __name__ == "__main__":
    sys.exit(main())
