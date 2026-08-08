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
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import config
from models.parcelle import Parcelle
from services.cache_service import CacheService, ProgressStore
from services.cadastre_service import CadastreService
from services.excel_service import ExcelService
from services.geoportail_service import ArcGISRestClient
from services.layers_service import LayersService
from utils.geometrie import construire_segments, cote_et_position, distance_min_polygone_rue
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
    (aux) filtré(s) si `--code-postal` était utilisé — voir `filtre_actif`).

    `echecs` : nombre de parcelles qui ont échoué PENDANT cette exécution et
    ont été automatiquement remplacées par une autre de la MÊME rue pour ne
    pas faire baisser le total obtenu (voir `_traiter_jusqu_a_objectif`) —
    jamais enregistrées comme traitées, donc jamais perdues, seulement
    reportées au prochain lancement. Exposé ici (plutôt que seulement
    journalisé) pour que l'appelant (GUI) puisse le mettre en évidence dans
    le résumé final plutôt que de le laisser uniquement dans le journal qui
    défile — demandé après qu'un utilisateur ait douté que ce décompte soit
    vraiment signalé quelque part.

    `parcelles_manquantes_par_rue` : {rue: nombre de parcelles encore non
    résolues} pour chaque rue déjà commencée (au moins une parcelle
    résolue) mais pas encore complète — ne devrait plus survenir que sur
    échec (voir `echecs`) ou tant qu'une rue plus ancienne n'a pas encore
    été rattrapée au rayon élargi, `_traiter_jusqu_a_objectif` n'entamant
    plus jamais une nouvelle rue tant qu'une précédente reste incomplète.
    Dict vide si aucune rue commencée n'est incomplète — signal explicite
    pour le suivi manuel (une rue "en haut" du fichier ne doit jamais
    rester incomplète pendant qu'une rue plus bas a déjà avancé)."""

    output_path: Path
    total_adresses: int
    parcelles_resolues: int
    restantes: int
    filtre_actif: bool
    echecs: int = 0
    parcelles_manquantes_par_rue: Dict[str, int] = field(default_factory=dict)

    @property
    def termine(self) -> bool:
        """True si plus aucune adresse ne reste à traiter dans le périmètre
        de cette exécution (toute la commune si `filtre_actif` est False)."""
        return self.restantes == 0


@dataclass
class RapportEchecs:
    """Résultat d'un appel à `retraiter_echecs` — permet à l'appelant (CLI,
    GUI, GitHub Actions) de savoir combien de parcelles en échec restent,
    pour décider s'il faut relancer une nouvelle fois ce rattrapage ciblé."""

    tentees: int
    reussies: int
    restantes: int

    @property
    def termine(self) -> bool:
        """True si plus aucune parcelle en échec ne reste pour cette commune."""
        return self.restantes == 0


@dataclass
class RapportRecalcul:
    """Résultat d'un appel à `recalculer_cote_position` — un compteur par
    étape plutôt qu'un seul total, pour que l'appelant (CLI, GUI, GitHub
    Actions) puisse afficher un vrai récapitulatif (combien redécouvertes,
    combien recollées, combien supprimées et pour laquelle des 3 raisons)
    au lieu d'un chiffre agrégé opaque — demandé après qu'un utilisateur
    ait cherché à savoir combien de lignes avaient été supprimées sans
    pouvoir le déduire du seul total."""

    parcelles_redecouvertes: int = 0  # étape 1 : nouvelles parcelles trouvées au rayon élargi
    troncons_recolles: int = 0  # étape 2 : côté/position recalculé après recollement des tronçons
    cote_position_corriges: int = 0  # étape 3 : côté/position manquants complétés
    supprimees_adresse_ailleurs: int = 0  # étape 4
    supprimees_doublon_rue: int = 0  # étape 5
    supprimees_commune_voisine: int = 0  # étape 6

    @property
    def total_supprimees(self) -> int:
        return self.supprimees_adresse_ailleurs + self.supprimees_doublon_rue + self.supprimees_commune_voisine

    @property
    def total(self) -> int:
        return (
            self.parcelles_redecouvertes + self.troncons_recolles + self.cote_position_corriges
            + self.total_supprimees
        )

    def resume(self, commune: str) -> str:
        if not self.total:
            return (
                f"Commune '{commune}' : rien à corriger (aucune nouvelle parcelle au rayon "
                f"élargi, aucune parcelle sans côté/position, aucun doublon détecté, aucune "
                f"parcelle d'une commune voisine détectée)."
            )
        return (
            f"Commune '{commune}' : {self.total} correction(s) au total — "
            f"{self.parcelles_redecouvertes} nouvelle(s) parcelle(s) trouvée(s) (rayon élargi), "
            f"{self.troncons_recolles} côté/position recalculé(s) (tronçons recollés), "
            f"{self.cote_position_corriges} côté/position complété(s) (manquants), "
            f"{self.total_supprimees} parcelle(s) supprimée(s) au total "
            f"({self.supprimees_adresse_ailleurs} adresse ailleurs, "
            f"{self.supprimees_doublon_rue} doublon rue la plus proche, "
            f"{self.supprimees_commune_voisine} commune voisine)."
        )


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
        help="Rattrapage géométrique, au lieu du traitement normal : calcule "
             "côté/position (voir utils/geometrie.py) pour les parcelles déjà "
             "résolues qui ne l'ont pas encore, retire les parcelles 'sans "
             "adresse' ajoutées à tort alors qu'elles ont une adresse ailleurs "
             "dans la commune, et pour celles candidates sur plusieurs rues à "
             "la fois ne garde que la plus proche géométriquement (voir "
             "main.py::recalculer_cote_position). À lancer une fois par "
             "commune (ex: workflow GitHub Actions dédié "
             "\"Recalculer côté/position\") — les parcelles "
             "traitées normalement n'ont déjà plus ces problèmes. --limit est "
             "ignoré dans ce mode ; --code-postal, lui, est respecté (utile "
             "pour une commune fusionnée dont on ne gère qu'un sous-ensemble "
             "des codes postaux, ex: Comines-Warneton).",
    )
    parser.add_argument(
        "--forcer-redecouverte", action="store_true",
        help="Avec --recalculer-cote-position : refait la redécouverte au "
             "rayon élargi pour TOUTES les rues déjà vérifiées, y compris "
             "celles déjà marquées redécouvertes lors d'un appel précédent "
             "(sans ce forçage, une rue déjà marquée n'est plus jamais "
             "retentée, même après une mise à jour de l'application qui "
             "changerait la recherche). Ne supprime jamais rien, seulement "
             "plus lent qu'un recalcul normal — à réserver à un doute "
             "concret (ex: rue traitée avec une ancienne version de l'app).",
    )
    parser.add_argument(
        "--retraiter-echecs", action="store_true",
        help="Rattrapage, au lieu du traitement normal : retente UNIQUEMENT les "
             "parcelles enregistrées en échec lors d'une exécution précédente "
             "(voir main.py::retraiter_echecs), sans les mélanger avec le reste de "
             "la commune non encore traitée. Peut être relancé plusieurs fois de "
             "suite jusqu'à ce qu'il n'y ait plus aucun échec. --limit et "
             "--code-postal sont ignorés dans ce mode.",
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


_ARCHIVES_A_CONSERVER = 5
_RE_SUFFIXE_ARCHIVE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})(?: (\d+))?\.xlsx$")


def _purger_archives_excedentaires(base_dir: Path, commune: str, garder: int = _ARCHIVES_A_CONSERVER) -> None:
    """Supprime les copies datées les plus anciennes de cette commune
    au-delà des `garder` dernières — sans ça, le dossier (et le dépôt
    walon-map-data une fois committé) grossirait indéfiniment, environ 1 Mo
    par copie archivée à chaque exécution.

    Tri par date/numéro extraits DU NOM DE FICHIER (voir
    `_chemin_archive_disponible`), jamais par date de modification du
    fichier sur disque : sur un clone Git fraîchement récupéré (le cas
    normal en CI), tous les fichiers partagent le même horodatage de
    checkout, qui ne reflète en rien l'ordre chronologique réel des
    exécutions passées."""
    prefixe = f"{commune}-"
    candidats: list = []
    for chemin in base_dir.glob(f"{commune}-*.xlsx"):
        m = _RE_SUFFIXE_ARCHIVE.match(chemin.name[len(prefixe):])
        if not m:
            continue
        jour, mois, annee, n = m.groups()
        candidats.append(((annee, mois, jour, int(n or 0)), chemin))
    candidats.sort(key=lambda c: c[0], reverse=True)
    for _, ancien in candidats[garder:]:
        ancien.unlink()
        _logger.info(
            "Commune '%s' : archive excédentaire supprimée (au-delà des %d dernière(s) conservée(s)) : %s",
            commune, garder, ancien.name,
        )


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


def _resoudre_une_parcelle(
    parcelle: Parcelle, cadastre_service: CadastreService, layers_service: LayersService,
) -> None:
    """Tente de résoudre entièrement UNE parcelle (rattachement cadastral +
    toutes les colonnes) — utilisé par la boucle normale de remplissage
    (`traiter_commune`) et par le retraitement ciblé des échecs
    (`retraiter_echecs`), pour ne jamais dupliquer cette séquence. Ne
    capture PAS les exceptions : à l'appelant de décider quoi faire d'un
    échec (remplacer par une autre parcelle, enregistrer l'échec, etc.)."""
    cadastre_service.rattacher_parcelle_cadastrale(parcelle)
    _set_identification_columns(parcelle)
    layers_service.resolve_all(parcelle)


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
    archiver_copie_datee: bool = True,
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
        # Une rue jamais encore traitée découvre déjà tout au rayon ACTUEL
        # (`force=False` suffit, `rue_deja_verifiee` est alors False de
        # toute façon) et se retrouve marquée `rue_redecouverte_rayon_elargi`
        # dès cette première découverte (voir CadastreService.
        # _parcelles_cadastrales_sans_adresse) — donc aucun coût
        # supplémentaire ici pour les rues à venir. Seules les rues déjà
        # vérifiées AVANT un élargissement du rayon (25m→200m) restent
        # bloquées sur leur ancien résultat, potentiellement incomplet, tant
        # que rien ne force explicitement une redécouverte — jusqu'ici
        # seulement via le rattrapage séparé `recalculer_cote_position`
        # (étape 1), qui peut tourner bien après que le traitement normal a
        # déjà avancé sur les rues suivantes (cas réel : Rue Moranfayt,
        # Dour — parcelles 1222R/1222S retrouvées seulement après coup,
        # alors que le traitement avait déjà atteint Rue Mouligneau). Forcer
        # ici comble cet écart directement dans le flux normal, une seule
        # fois par rue (le marquage empêche toute redécouverte répétée).
        force_redecouverte = not progress_store.rue_redecouverte_rayon_elargi(commune, rue)
        try:
            parcelles = cadastre_service.lister_parcelles(
                pays, commune, rue, codes_postaux=codes_postaux, force=force_redecouverte,
            )
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
                # toujours. Une colonne totalement absente de `cached`
                # (jamais écrite du tout par une exécution antérieure —
                # cas réel constaté sur Comines-Warneton, colonnes K/Y)
                # est traitée pareil : `retry_erreurs` la retente aussi
                # (voir sa docstring). Coût limité au rattachement
                # cadastral (1 requête) + aux seules colonnes concernées,
                # pas toute la parcelle — négligeable tant que le nombre de
                # parcelles concernées reste faible devant le total de la
                # commune.
                if any(v == "ERREUR" for v in cached.values()) or any(
                    not cached.get(col) for col in config.COLUMN_RULES
                ):
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

    # `a_faire` est déjà groupée par rue dans l'ordre alphabétique (voir la
    # boucle de découverte ci-dessus, qui itère `rues` triées) — la boucle
    # ci-dessous en profite pour ne JAMAIS entamer une nouvelle rue tant que
    # la précédente n'est pas entièrement résolue : sur un échec (`Exception`
    # inattendue, ex: incident réseau après épuisement des réessais), elle
    # continue à essayer les parcelles suivantes de la MÊME rue (pour
    # compenser sans pour autant dépasser une frontière de rue), mais
    # s'arrête complètement dès que la rue suivante serait sur le point de
    # commencer alors que la précédente a eu au moins un échec — quitte à
    # résoudre moins que l'objectif demandé cette exécution. Décision
    # explicite de l'utilisateur : un suivi manuel fiable (une rue "en
    # haut" du fichier jamais laissée incomplète pendant qu'une rue plus
    # bas a déjà avancé) prime désormais sur le fait de toujours livrer
    # exactement l'objectif demandé (cas réel : Rue Moranfayt, Dour,
    # incomplète alors que Rue Mouligneau, plus bas, avait déjà des lignes
    # résolues). Une parcelle en échec n'est de toute façon jamais perdue
    # (jamais enregistrée comme traitée, redécouverte au prochain
    # lancement — voir plus haut).
    #
    # `plafond_tentatives` (2x objectif) reste comme garde-fou : un run
    # GitHub Actions est plafonné à 6h (voir timeout-minutes dans le
    # workflow) — un taux d'échec anormalement élevé au sein d'une seule
    # rue géante ne doit pas faire tenter indéfiniment la même rue. Rarement
    # atteint désormais, la compensation ne dépassant plus les frontières
    # de rue.
    nouvellement_traitees: List[Parcelle] = []
    parcelles_tentees = 0
    cible = min(objectif, len(a_faire))
    plafond_tentatives = min(len(a_faire), objectif * 2)

    def _traiter_jusqu_a_objectif():
        nonlocal parcelles_tentees
        rue_courante: Optional[str] = None
        echec_rue_courante = False
        for parcelle in a_faire:
            if parcelle.rue != rue_courante:
                if echec_rue_courante:
                    return
                rue_courante = parcelle.rue
                echec_rue_courante = False
            if len(nouvellement_traitees) >= objectif or parcelles_tentees >= plafond_tentatives:
                return
            parcelles_tentees += 1
            try:
                _resoudre_une_parcelle(parcelle, cadastre_service, layers_service)
            except Exception as exc:  # noqa: BLE001 - ne jamais interrompre le traitement des autres parcelles
                _logger.exception("Échec inattendu pour la parcelle %s, ligne ignorée.", parcelle.identifiant)
                progress_store.enregistrer_echec(commune, parcelle, str(exc))
                echec_rue_courante = True
                continue
            progress_store.set(commune, parcelle.identifiant, parcelle.valeurs)
            progress_store.supprimer_echec(commune, parcelle.identifiant)
            nouvellement_traitees.append(parcelle)
            yield parcelle

    for parcelle_index, _ in enumerate(
        progress(_traiter_jusqu_a_objectif(), total=cible, description=f"Remplissage ({commune})")
    ):
        if on_progress:
            on_progress("remplissage", parcelle_index + 1, cible)

    echecs = parcelles_tentees - len(nouvellement_traitees)
    if echecs > 0:
        _logger.info(
            "Commune '%s' : %d parcelle(s) tentée(s) pour obtenir %d nouvelle(s) réussie(s) "
            "(%d échec(s) remplacé(s) automatiquement).",
            commune, parcelles_tentees, len(nouvellement_traitees), echecs,
        )
        # Rassurance explicite, demandée après que des clients (pas les
        # collaborateurs qui font tourner l'outil, mais ceux à qui le
        # travail est vendu) se soient inquiétés en voyant des écarts de
        # comptage d'une exécution à l'autre, sans savoir que les
        # parcelles en échec sont automatiquement rattrapées : une
        # parcelle en échec n'est jamais enregistrée comme traitée (voir
        # `_traiter_jusqu_a_objectif` ci-dessus), donc rien n'est
        # réellement perdu, seulement reporté à l'exécution suivante.
        _logger.info(
            "Commune '%s' : les %d parcelle(s) en échec ne sont PAS perdues — jamais "
            "enregistrées comme traitées, elles seront automatiquement redétectées et "
            "retentées au prochain lancement, sans action manuelle nécessaire.",
            commune, echecs,
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

    # Récapitulatif dédié aux rues "en haut" du fichier (déjà commencées —
    # au moins une parcelle résolue avant ou pendant cette exécution) qui
    # ne sont malgré tout pas encore complètes — gratuit, aucun appel
    # réseau supplémentaire, à partir des données déjà collectées
    # ci-dessus. Avec l'ordre strict imposé par `_traiter_jusqu_a_objectif`,
    # ce cas ne devrait plus survenir qu'en présence d'échecs (une rue
    # s'arrête au milieu) ou tant que des rues plus anciennes n'ont pas
    # encore été rattrapées au rayon élargi (voir plus haut) — sert de
    # signal explicite pour le suivi manuel plutôt que de laisser deviner.
    nouvellement_traitees_ids = {p.identifiant for p in nouvellement_traitees}
    encore_a_faire = [p for p in a_faire if p.identifiant not in nouvellement_traitees_ids]
    rues_commencees = {p.rue for p in deja_faites} | {p.rue for p in nouvellement_traitees}
    parcelles_manquantes_par_rue: Dict[str, int] = {}
    for p in encore_a_faire:
        if p.rue in rues_commencees:
            parcelles_manquantes_par_rue[p.rue] = parcelles_manquantes_par_rue.get(p.rue, 0) + 1

    if parcelles_manquantes_par_rue:
        total_manquantes = sum(parcelles_manquantes_par_rue.values())
        detail = ", ".join(f"{rue} ({n})" for rue, n in sorted(parcelles_manquantes_par_rue.items()))
        _logger.warning(
            "Commune '%s' : %d parcelle(s) manquante(s) pour %d rue(s) déjà commencée(s) en haut "
            "du fichier : %s.", commune, total_manquantes, len(parcelles_manquantes_par_rue), detail,
        )
    else:
        _logger.info(
            "Commune '%s' : 0 parcelle manquante pour les rues en haut, le traitement peut "
            "continuer.", commune,
        )

    # Le fichier de sortie est TOUJOURS reconstruit à partir de la totalité
    # des parcelles déjà résolues pour la commune (indépendamment du filtre
    # --code-postal éventuel de cette exécution), pour ne jamais faire
    # disparaître du fichier des lignes résolues lors d'exécutions
    # précédentes avec un filtre différent (ou sans filtre).
    final_output_path = _reconstruire_fichier_sortie(
        commune, progress_store, excel_service, output_path, archiver_copie_datee=archiver_copie_datee,
    )
    return ResultatTraitement(
        output_path=final_output_path,
        total_adresses=total_adresses,
        parcelles_resolues=len(parcelles_resolues),
        restantes=restantes,
        filtre_actif=bool(codes_postaux),
        echecs=echecs,
        parcelles_manquantes_par_rue=parcelles_manquantes_par_rue,
    )


def _reconstruire_fichier_sortie(
    commune: str,
    progress_store: ProgressStore,
    excel_service: ExcelService,
    output_path: Optional[Path] = None,
    archiver_copie_datee: bool = True,
) -> Path:
    """Reconstruit le fichier Excel de sortie à partir de la totalité des
    parcelles déjà résolues pour la commune — appelé par `traiter_commune`
    après chaque exécution, et par `recalculer_cote_position` après avoir
    mis à jour côté/position sur des parcelles déjà résolues (même besoin :
    relire `progress_store`, trier, réécrire).

    `archiver_copie_datee=True` (par défaut, CLI et GitHub Actions) : une
    copie datée (voir `_chemin_archive_disponible`) est en plus archivée à
    côté du fichier stable, purgée au-delà des `_ARCHIVES_A_CONSERVER`
    dernières (voir `_purger_archives_excedentaires`) — sert de trace/audit
    indépendante de ce que chaque exécution a réellement produit. Incident
    réel qui a motivé son extension à `--output-dir`/GitHub Actions (avant,
    réservée au seul chemin par défaut via `output_path is None`, donc
    jamais déclenchée par la CI) : le workflow "Traiter une commune" a
    perdu silencieusement des heures de traitement réel sans qu'aucun
    fichier daté n'en garde la trace pour le remarquer plus tôt. Le GUI
    passe `False` (l'utilisateur maîtrise déjà le nommage via « Enregistrer
    sous… », pas d'archive automatique dans son dossier de travail)."""
    toutes_valeurs_commune = list(progress_store.all_for_commune(commune).values())
    toutes_valeurs_commune.sort(key=cle_tri_parcelle)

    wb = excel_service.load_output_workbook()
    ws = excel_service.get_active_sheet(wb)
    column_order = list(config.COLUMN_RULES.keys())
    all_columns = ["A", "B", "C", "D", "E", "F"] + column_order
    excel_service.write_parcelles(ws, toutes_valeurs_commune, all_columns)

    final_output_path = output_path or (config.OUTPUT_DIR / f"{commune}.xlsx")
    excel_service.save(wb, final_output_path)
    if archiver_copie_datee:
        chemin_archive = _chemin_archive_disponible(final_output_path.parent, commune)
        shutil.copy2(final_output_path, chemin_archive)
        _logger.info("Copie datée archivée : %s", chemin_archive)
        _purger_archives_excedentaires(final_output_path.parent, commune)
    return final_output_path


def recalculer_cote_position(
    commune: str,
    cadastre_service: CadastreService,
    progress_store: ProgressStore,
    excel_service: ExcelService,
    output_path: Optional[Path] = None,
    codes_postaux: Optional[List[str]] = None,
    on_progress: Optional[ProgressCallback] = None,
    forcer_redecouverte: bool = False,
    archiver_copie_datee: bool = True,
) -> RapportRecalcul:
    """Rattrapage géométrique unique, à lancer une fois par commune, qui
    corrige en une passe tout ce qui a changé après coup dans le calcul
    géométrique des parcelles déjà connues (un seul bouton/flag plutôt
    qu'un par correction, sur demande explicite — les cinq problèmes
    partagent le même besoin de base : reparcourir les rues déjà connues
    et récupérer leur tracé PICC) :

    1. Redécouvre avec le rayon de recherche élargi (voir
       `CadastreService._RAYON_RECHERCHE_PARCELLES_M`, 200m contre 25m à
       l'origine) les rues déjà vérifiées, pour capturer les parcelles
       "de deuxième rangée" que l'ancien rayon ratait (cas réel : fichier
       de référence de Crisnée, parcelles jusqu'à 176m du tracé). Nettement
       plus coûteux que les étapes suivantes (fenêtre de recherche ~64x
       plus grande) — pour une commune déjà largement traitée, à lancer
       de préférence via le workflow GitHub Actions dédié
       "Recalculer côté/position" plutôt qu'en local.
    2. Recolle les tronçons PICC d'une rue dans le bon ordre/sens (voir
       `utils/geometrie.py::_rechainer_chemins`) et recalcule côté/position
       pour TOUTES les parcelles déjà résolues des rues à plusieurs
       tronçons (une rue à tronçon unique n'est jamais affectée) — sans ce
       recollement, une rue coupée à un carrefour peut avoir un côté qui
       s'inverse et une position qui saute d'un tronçon à l'autre (cas
       réel : Avenue Jules Sartieaux, Dour, 3 tronçons dont deux
       digitalisés en sens inverse).
    3. Calcule côté/position (voir utils/geometrie.py) pour les parcelles
       déjà résolues qui ne l'ont pas encore (ajouté après coup — inclut
       les parcelles fraîchement trouvées par l'étape 1 une fois résolues).
    4. Retire les parcelles "sans adresse" ajoutées à tort alors qu'elles
       ont en réalité une adresse ICAR ailleurs dans la commune (voir
       `CadastreService.a_une_adresse_ailleurs` — cas réel : Dour, parcelle
       91P3, adresse "170" sur Rue Moranfayt mais aussi listée en "/" sous
       Avenue Hyacinthe Harmegnies qu'elle borde aussi).
    5. Pour une parcelle sans adresse trouvée candidate sur PLUSIEURS rues
       à la fois (cas réel : parcelle 91S2, candidate à la fois sur deux
       rues voisines — plus fréquent depuis l'élargissement du rayon de
       l'étape 1), ne garde que la rue dont le tracé est géométriquement
       le plus proche du polygone de la parcelle (voir
       `utils/geometrie.py::distance_min_polygone_rue`), retire les autres.
       Nettoie aussi bien les doublons hérités d'avant ce rattrapage que
       ceux introduits par l'étape 1 elle-même (volontairement en dernier).
    6. Retire les parcelles "sans adresse" qui appartiennent en réalité à
       une commune VOISINE (voir `CadastreService.commune_reelle`, champ
       NOM_COMMUNE de CADMAP) — la recherche par rayon de l'étape 1
       n'avait, avant ce correctif, aucun filtre de commune côté CADMAP
       (contrairement à ICAR/PICC) : une rue proche d'une frontière
       communale pouvait récupérer des parcelles d'une commune voisine, non
       détectées par les étapes 4/5 ci-dessus qui ne comparent qu'À
       L'INTÉRIEUR de la commune traitée. Cas réel : "Rue de la Frontière"
       (Dour), 20 parcelles de Frameries ajoutées à tort. Un appel réseau
       par parcelle sans-adresse déjà connue, suivi par rue comme les
       étapes coûteuses ci-dessus.

    Les parcelles traitées normalement (`traiter_commune`) depuis l'ajout
    de ces vérifications n'ont déjà plus ces problèmes ; relancer ceci ne
    fait rien. Renvoie un `RapportRecalcul` (un compteur par étape, voir sa
    docstring) plutôt qu'un simple total, pour permettre un vrai
    récapitulatif détaillé côté appelant.

    `codes_postaux`, si fourni, restreint les cinq étapes aux rues/
    parcelles de ces codes postaux — indispensable pour une commune
    fusionnée dont chaque collaborateur ne gère qu'un sous-ensemble des
    codes postaux (ex: Comines-Warneton, 7780-7784) : sans ce filtre, le
    rattrapage redécouvre et modifie TOUTE la commune, y compris des rues
    de codes postaux dont le collaborateur qui l'a lancé n'est pas
    responsable (incident réel constaté : collaboratrice en charge de
    Courcelles/6180 uniquement, correction remontée jusqu'à 6183). Une
    rue/parcelle dont le code postal n'est pas encore connu (jamais
    résolue) n'est par prudence jamais sautée, comme dans `traiter_commune`.

    `forcer_redecouverte`, si True, refait l'étape 1 pour TOUTES les rues déjà
    vérifiées, y compris celles déjà marquées "redécouvertes avec le rayon
    élargi" lors d'un appel précédent — sans ce forçage, une rue déjà marquée
    n'est plus jamais retentée, même après une mise à jour de l'application
    qui changerait la logique de découverte (rayon élargi, correctif de
    tracé, etc. — voir `CadastreService._RAYON_RECHERCHE_PARCELLES_M`). Cas
    réel : Rue Baudouin 1er (Courcelles), déjà marquée redécouverte avant
    l'élargissement du rayon à 200m, ne retrouvait plus jamais 6 parcelles
    pourtant à moins de 51m du tracé tant que ce forçage n'existait pas.
    Ne supprime jamais rien : `_parcelles_cadastrales_sans_adresse` fusionne
    toujours ses résultats avec l'existant (INSERT OR REPLACE par parcelle),
    ne purge jamais — une rue déjà correctement découverte ne perd aucune
    ligne en étant redécouverte une fois de plus, elle est seulement
    recomparée à la recherche actuelle. Nettement plus coûteux qu'un
    recalcul normal (refait l'étape 1 pour des rues qui n'en avaient pas
    besoin) : à réserver à un doute concret plutôt qu'à un usage
    systématique."""
    rapport = RapportRecalcul()
    segments_par_rue: Dict[str, list] = {}

    def _point_reference(rue: str) -> Optional[Tuple[float, float]]:
        """Point de la parcelle déjà résolue au plus petit numéro de rue
        connu pour `rue` (voir CadastreService._point_reference_numero,
        même principe mais à partir des entrées déjà en base plutôt que
        d'une liste ICAR fraîchement interrogée) — ancre la position "0"
        du tracé sur ce point plutôt que sur le sens de digitalisation PICC
        brut. `None` si aucun numéro purement numérique n'est encore connu
        pour cette rue (aucun changement de comportement dans ce cas)."""
        meilleur: Optional[Tuple[int, str]] = None
        for identifiant, valeurs in base.items():
            if valeurs.get("D") != rue:
                continue
            m = re.match(r"^(\d+)", valeurs.get("E") or "")
            if not m:
                continue
            valeur = int(m.group(1))
            if meilleur is None or valeur < meilleur[0]:
                meilleur = (valeur, identifiant)
        return cadastre_service.point_pour_identifiant(meilleur[1]) if meilleur is not None else None

    def _segments(rue: str) -> list:
        if rue not in segments_par_rue:
            troncons = cadastre_service.recuperer_troncons(commune, rue)
            segments_par_rue[rue] = construire_segments(troncons, point_reference=_point_reference(rue))
        return segments_par_rue[rue]

    # -- 1. Redécouvrir avec le rayon élargi les rues déjà vérifiées -------
    # `rue_redecouverte_rayon_elargi` (une fois marquée) rend cette étape
    # sans effet lors d'une relance sur une commune déjà redécouverte —
    # sans ce suivi, elle repayerait le coût de la redécouverte (~2 min/rue
    # observé en conditions réelles) à chaque exécution du rattrapage, pour
    # ne jamais rien retrouver de nouveau. `forcer_redecouverte` ignore ce
    # marqueur et retente TOUTES les rues déjà vérifiées (voir docstring).
    rues_a_redecouvrir = [
        rue for rue in progress_store.rues_verifiees_commune(commune)
        if forcer_redecouverte or not progress_store.rue_redecouverte_rayon_elargi(commune, rue)
    ]
    for rue_index, rue in enumerate(rues_a_redecouvrir):
        if on_progress:
            on_progress("redecouverte_rayon_elargi", rue_index + 1, len(rues_a_redecouvrir))
        avant = {e["capakey"] for e in progress_store.parcelles_sans_adresse(commune, rue)}
        cadastre_service.lister_parcelles("Belgique", commune, rue, codes_postaux=codes_postaux, force=True)
        nouvelles_capakeys = {
            e["capakey"] for e in progress_store.parcelles_sans_adresse(commune, rue)
        } - avant
        # `lister_parcelles(force=True)` marque déjà la rue via
        # CadastreService._parcelles_cadastrales_sans_adresse (voir
        # cadastre_service.py) — pas besoin de le refaire ici.
        if nouvelles_capakeys:
            _logger.info(
                "Commune '%s' : rue '%s' redécouverte avec le rayon élargi — %d nouvelle(s) "
                "parcelle(s) sans adresse trouvée(s).", commune, rue, len(nouvelles_capakeys),
            )
            rapport.parcelles_redecouvertes += len(nouvelles_capakeys)

    base = progress_store.all_for_commune(commune)

    # -- 2. Recoller les tronçons dans le bon ordre/sens (rues à plusieurs
    # tronçons uniquement) — voir utils/geometrie.py::_rechainer_chemins.
    # Une rue déjà résolue AVANT ce correctif peut avoir un côté/position
    # incohérent si son tracé est coupé en plusieurs tronçons PICC (cas
    # réel : Avenue Jules Sartieaux, Dour, 3 tronçons dont deux digitalisés
    # en sens inverse — la position faisait des sauts et le côté
    # s'inversait d'un tronçon à l'autre). Contrairement à l'étape suivante
    # (qui ne comble que les valeurs manquantes), celle-ci recalcule TOUTES
    # les parcelles déjà résolues de la rue, puisque leur côté/position
    # existant peut être faux, pas seulement absent. Une rue à un seul
    # tronçon n'est jamais affectée par ce recollement (marquée
    # immédiatement, rien à recalculer).
    rues_connues = set(progress_store.rues_verifiees_commune(commune))
    rues_connues.update(v.get("D", "") for v in base.values() if v.get("D"))
    rues_a_recoller = sorted(
        rue for rue in rues_connues if rue and not progress_store.rue_geometrie_recollee(commune, rue)
    )
    for rue_index, rue in enumerate(rues_a_recoller):
        if on_progress:
            on_progress("recollement_troncons", rue_index + 1, len(rues_a_recoller))
        troncons = cadastre_service.recuperer_troncons(commune, rue)
        nb_chemins = sum(len((t.get("geometry") or {}).get("paths", [])) for t in troncons)
        if nb_chemins <= 1:
            if codes_postaux is None:
                progress_store.marquer_rue_geometrie_recollee(commune, rue)
            continue

        segments = _segments(rue)
        entrees_rue = {ident: valeurs for ident, valeurs in base.items() if valeurs.get("D") == rue}
        filtre_a_ignore_une_entree = False
        corrigees_cette_rue = 0
        for identifiant, valeurs in entrees_rue.items():
            if codes_postaux is not None and valeurs.get("B") not in codes_postaux:
                filtre_a_ignore_une_entree = True
                continue
            point = cadastre_service.point_pour_identifiant(identifiant)
            if point is None:
                continue
            resultat = cote_et_position(point[0], point[1], segments)
            if resultat is None:
                continue
            cote, position = resultat
            if valeurs.get("_cote") == cote and valeurs.get("_position") == position:
                continue
            valeurs["_cote"] = cote
            valeurs["_position"] = position
            progress_store.set(commune, identifiant, valeurs)
            if "|CAPAKEY:" in identifiant:
                capakey = identifiant.rsplit("CAPAKEY:", 1)[-1]
                progress_store.maj_cote_position_sans_adresse(commune, rue, capakey, cote, position)
            corrigees_cette_rue += 1
        # Ne marque la rue "recollée" que si elle a été traitée sans filtre
        # de code postal actif (ou si le filtre couvrait déjà toutes ses
        # parcelles) — sinon une future exécution avec un filtre différent
        # ne la retraiterait jamais, laissant les parcelles ignorées avec
        # un côté/position potentiellement faux pour toujours (même
        # principe que l'étape 1 pour --code-postal).
        if not filtre_a_ignore_une_entree:
            progress_store.marquer_rue_geometrie_recollee(commune, rue)
        if corrigees_cette_rue:
            _logger.info(
                "Commune '%s' : rue '%s' recollée (%d tronçon(s)) — %d parcelle(s) "
                "côté/position recalculée(s).", commune, rue, nb_chemins, corrigees_cette_rue,
            )
            rapport.troncons_recolles += corrigees_cette_rue

    # -- 3. Côté/position manquants (parcelles déjà résolues) --------------
    a_corriger_cote_position: Dict[str, list] = {}
    for identifiant, valeurs in base.items():
        if codes_postaux is not None and valeurs.get("B") not in codes_postaux:
            continue
        if "_cote" not in valeurs or "_position" not in valeurs:
            a_corriger_cote_position.setdefault(valeurs.get("D", ""), []).append((identifiant, valeurs))

    rues_a_visiter = sorted(a_corriger_cote_position.keys())
    for rue_index, rue in enumerate(rues_a_visiter):
        if on_progress:
            on_progress("recalcul_côté_position", rue_index + 1, len(rues_a_visiter))
        segments = _segments(rue)
        entrees = a_corriger_cote_position[rue]
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
            rapport.cote_position_corriges += 1

    # -- 4. Parcelles "sans adresse" ayant en réalité une adresse ailleurs -
    rues_verifiees = progress_store.rues_verifiees_commune(commune)
    for rue_index, rue in enumerate(rues_verifiees):
        if on_progress:
            on_progress("nettoyage_adresse_ailleurs", rue_index + 1, len(rues_verifiees))
        for entree in progress_store.parcelles_sans_adresse(commune, rue):
            if codes_postaux is not None and entree.get("code_postal") not in codes_postaux:
                continue
            rings = (entree.get("geometry") or {}).get("rings", [])
            if not rings or not cadastre_service.a_une_adresse_ailleurs(commune, rings):
                continue
            capakey = entree["capakey"]
            progress_store.supprimer_parcelle_sans_adresse(commune, rue, capakey)
            progress_store.supprimer_resultat(commune, f"{commune}|CAPAKEY:{capakey}")
            _logger.info(
                "Commune '%s' : parcelle %s (rue '%s') retirée — a en réalité une adresse "
                "sur une autre rue.", commune, capakey, rue,
            )
            rapport.supprimees_adresse_ailleurs += 1

    # -- 5. Parcelles "sans adresse" candidates sur plusieurs rues à la fois
    # (relire rues_verifiees + parcelles_sans_adresse APRÈS l'étape 3 :
    # une parcelle qui vient d'être retirée là-haut ne doit pas être
    # comparée ici, elle n'est de toute façon plus "sans adresse")
    rues_candidates_par_capakey: Dict[str, list] = {}
    for rue in progress_store.rues_verifiees_commune(commune):
        for entree in progress_store.parcelles_sans_adresse(commune, rue):
            if codes_postaux is not None and entree.get("code_postal") not in codes_postaux:
                continue
            rues_candidates_par_capakey.setdefault(entree["capakey"], []).append(rue)

    doublons = {capakey: rues for capakey, rues in rues_candidates_par_capakey.items() if len(rues) > 1}
    for capakey_index, (capakey, rues_candidates) in enumerate(doublons.items()):
        if on_progress:
            on_progress("nettoyage_rue_plus_proche", capakey_index + 1, len(doublons))
        meilleure_rue: Optional[str] = None
        meilleure_distance: Optional[float] = None
        for rue in rues_candidates:
            entree = next(
                (e for e in progress_store.parcelles_sans_adresse(commune, rue) if e["capakey"] == capakey), None,
            )
            if entree is None or not entree.get("geometry"):
                continue
            rings = entree["geometry"].get("rings", [])
            distance = distance_min_polygone_rue(rings, _segments(rue))
            if distance is not None and (meilleure_distance is None or distance < meilleure_distance):
                meilleure_distance = distance
                meilleure_rue = rue
        if meilleure_rue is None:
            continue

        identifiant = f"{commune}|CAPAKEY:{capakey}"
        resultat_actuel = progress_store.get(commune, identifiant)
        changement = False
        if resultat_actuel is not None and resultat_actuel.get("D") != meilleure_rue:
            progress_store.supprimer_resultat(commune, identifiant)
            changement = True
        for rue in rues_candidates:
            if rue != meilleure_rue:
                progress_store.supprimer_parcelle_sans_adresse(commune, rue, capakey)
                changement = True
        if changement:
            _logger.info(
                "Commune '%s' : parcelle %s candidate sur %d rue(s) — rue la plus proche "
                "retenue : '%s'.", commune, capakey, len(rues_candidates), meilleure_rue,
            )
            rapport.supprimees_doublon_rue += 1

    # -- 6. Parcelles "sans adresse" appartenant en réalité à une commune
    # voisine (recherche par rayon avant le filtre NOM_COMMUNE ajouté à
    # `_parcelles_cadastrales_sans_adresse` — cas réel : "Rue de la
    # Frontière", Dour, 20 parcelles de Frameries ajoutées à tort). Un
    # appel réseau par parcelle (pas de raccourci géométrique possible),
    # donc suivi par rue comme les étapes coûteuses ci-dessus pour ne
    # jamais revérifier une rue déjà propre.
    rues_a_verifier_commune = [
        rue for rue in progress_store.rues_verifiees_commune(commune)
        if not progress_store.rue_commune_verifiee(commune, rue)
    ]
    for rue_index, rue in enumerate(rues_a_verifier_commune):
        if on_progress:
            on_progress("nettoyage_commune_voisine", rue_index + 1, len(rues_a_verifier_commune))
        filtre_a_ignore_une_entree = False
        for entree in progress_store.parcelles_sans_adresse(commune, rue):
            if codes_postaux is not None and entree.get("code_postal") not in codes_postaux:
                filtre_a_ignore_une_entree = True
                continue
            capakey = entree["capakey"]
            commune_reelle = cadastre_service.commune_reelle(capakey)
            if commune_reelle is None or commune_reelle.strip().lower() == commune.strip().lower():
                continue
            progress_store.supprimer_parcelle_sans_adresse(commune, rue, capakey)
            progress_store.supprimer_resultat(commune, f"{commune}|CAPAKEY:{capakey}")
            _logger.info(
                "Commune '%s' : parcelle %s (rue '%s') retirée — appartient en réalité à la "
                "commune '%s'.", commune, capakey, rue, commune_reelle,
            )
            rapport.supprimees_commune_voisine += 1
        if not filtre_a_ignore_une_entree:
            progress_store.marquer_rue_commune_verifiee(commune, rue)

    if rapport.total:
        _reconstruire_fichier_sortie(
            commune, progress_store, excel_service, output_path, archiver_copie_datee=archiver_copie_datee,
        )
    _logger.info(rapport.resume(commune))
    return rapport


def retraiter_echecs(
    commune: str,
    cadastre_service: CadastreService,
    layers_service: LayersService,
    progress_store: ProgressStore,
    excel_service: ExcelService,
    output_path: Optional[Path] = None,
    on_progress: Optional[ProgressCallback] = None,
    archiver_copie_datee: bool = True,
) -> RapportEchecs:
    """Retente UNIQUEMENT les parcelles enregistrées en échec par
    `_traiter_jusqu_a_objectif` (voir `ProgressStore.enregistrer_echec`),
    sans les mélanger avec le reste de la commune pas encore traitée —
    contrairement au traitement normal (`traiter_commune`), qui les
    rattrape aussi mais noyées parmi toute nouvelle parcelle jamais
    rencontrée. Pensé pour être relancé plusieurs fois de suite jusqu'à ce
    qu'il n'y ait plus aucun échec, sans devoir retraiter toute la commune
    entre-temps.

    Chaque parcelle en échec est reconstruite à partir des champs stockés
    au moment de l'échec (pays, code postal, rue, numéro, adr_id/capakey) ;
    sa position (x, y) est retrouvée via `CadastreService.point_pour_identifiant`
    (une requête ciblée par ADR_ID ou CAPAKEY, jamais une redécouverte de
    toute la rue)."""
    echecs = progress_store.echecs_commune(commune)
    total = len(echecs)
    reussies = 0
    for index, e in enumerate(echecs):
        if on_progress:
            on_progress("retraitement_echecs", index + 1, total)
        parcelle = Parcelle(
            pays=e["pays"], code_postal=e["code_postal"], commune=commune,
            rue=e["rue"], numero=e["numero"], adr_id=e["adr_id"], capakey=e["capakey"],
        )
        point = cadastre_service.point_pour_identifiant(parcelle.identifiant)
        if point is None:
            _logger.warning(
                "Parcelle %s : identifiant trop ancien pour être relocalisée directement, "
                "retirée du suivi des échecs (sera quand même redécouverte normalement au "
                "prochain traitement complet de la commune).", parcelle.identifiant,
            )
            progress_store.supprimer_echec(commune, parcelle.identifiant)
            continue
        parcelle.x, parcelle.y = point
        try:
            _resoudre_une_parcelle(parcelle, cadastre_service, layers_service)
        except Exception as exc:  # noqa: BLE001 - un échec répété ne doit jamais arrêter le retraitement des autres
            _logger.exception("Nouvel échec pour la parcelle %s.", parcelle.identifiant)
            progress_store.enregistrer_echec(commune, parcelle, str(exc))
            continue
        progress_store.set(commune, parcelle.identifiant, parcelle.valeurs)
        progress_store.supprimer_echec(commune, parcelle.identifiant)
        reussies += 1

    if reussies:
        _reconstruire_fichier_sortie(
            commune, progress_store, excel_service, output_path, archiver_copie_datee=archiver_copie_datee,
        )
    restantes = total - reussies
    _logger.info(
        "Commune '%s' : %d/%d parcelle(s) en échec retraitée(s) avec succès (%d encore en échec).",
        commune, reussies, total, restantes,
    )
    return RapportEchecs(tentees=total, reussies=reussies, restantes=restantes)


def supprimer_parcelles_hors_codes_postaux(
    commune: str,
    codes_postaux_autorises: List[str],
    progress_store: ProgressStore,
    excel_service: ExcelService,
    output_path: Optional[Path] = None,
    archiver_copie_datee: bool = True,
) -> int:
    """Supprime DÉFINITIVEMENT les parcelles déjà résolues de cette commune
    dont le code postal n'est PAS dans `codes_postaux_autorises`, ainsi que
    les entrées correspondantes du cache de découverte "sans adresse" (sans
    ça, une future exécution non filtrée les retrouverait telles quelles
    via ce cache et les résoudrait de nouveau).

    Nettoyage pour les communes déjà touchées par un rattrapage lancé sans
    filtre de code postal AVANT que `recalculer_cote_position` respecte
    `codes_postaux` (incident réel : Courcelles, collaboratrice en charge
    du seul 6180, lignes jusqu'à 6183 remontées après coup).

    Action destructive et irréversible (hors historique Git du dépôt de
    données) : voir gui.py::_supprimer_hors_code_postal pour les
    confirmations demandées avant d'appeler ceci — cette fonction elle-même
    ne demande AUCUNE confirmation, elle exécute immédiatement. Renvoie le
    nombre de lignes réellement supprimées de `parcelle_resultats`."""
    identifiants = progress_store.identifiants_hors_codes_postaux(commune, codes_postaux_autorises)
    for identifiant in identifiants:
        progress_store.supprimer_resultat(commune, identifiant)

    for rue in progress_store.rues_verifiees_commune(commune):
        for entree in progress_store.parcelles_sans_adresse(commune, rue):
            if entree.get("code_postal") and entree["code_postal"] not in codes_postaux_autorises:
                progress_store.supprimer_parcelle_sans_adresse(commune, rue, entree["capakey"])

    if identifiants:
        _reconstruire_fichier_sortie(
            commune, progress_store, excel_service, output_path, archiver_copie_datee=archiver_copie_datee,
        )
        _logger.warning(
            "Commune '%s' : %d ligne(s) supprimée(s) définitivement (code postal hors de %s).",
            commune, len(identifiants), codes_postaux_autorises,
        )
    return len(identifiants)


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
                recalculer_cote_position(
                    commune, cadastre_service, progress_store, excel_service,
                    output_path=output_path, codes_postaux=args.codes_postaux,
                    forcer_redecouverte=args.forcer_redecouverte,
                )
                recalcul_reussi = True
                continue
            if args.retraiter_echecs:
                retraiter_echecs(
                    commune, cadastre_service, layers_service, progress_store, excel_service,
                    output_path=output_path,
                )
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
        if resultat.echecs > 0:
            _logger.info(
                "%d échec(s) pendant cette exécution — pas perdus, retentés automatiquement "
                "au prochain lancement.", resultat.echecs,
            )
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
