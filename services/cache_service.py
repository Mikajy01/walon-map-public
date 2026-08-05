"""Cache local des requêtes HTTP (SQLite) pour éviter les appels redondants.

Le cache est la brique qui permet à la fois la performance (aucune requête
n'est envoyée deux fois pour les mêmes paramètres) et la reprise après
interruption : si le programme est relancé, toutes les réponses déjà
obtenues sont immédiatement disponibles sans re-solliciter les services du
Géoportail.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

_logger = get_logger("services.cache_service")


class CacheService:
    def __init__(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = cache_dir / "http_cache.sqlite3"
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=30)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS http_cache (
                    cache_key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    params TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    @staticmethod
    def make_key(url: str, params: dict) -> str:
        payload = json.dumps({"url": url, "params": params}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, url: str, params: dict) -> Optional[Any]:
        key = self.make_key(url, params)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT response FROM http_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        _logger.debug("Cache HIT (%s) url=%s params=%s", key[:12], url, params)
        return json.loads(row[0])

    def set(self, url: str, params: dict, response: Any) -> None:
        key = self.make_key(url, params)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO http_cache (cache_key, url, params, response) "
                "VALUES (?, ?, ?, ?)",
                (key, url, json.dumps(params, default=str), json.dumps(response)),
            )
            conn.commit()
        _logger.debug("Cache SET (%s) url=%s params=%s", key[:12], url, params)


class ProgressStore:
    """Persistance des lignes déjà calculées, pour la reprise après interruption.

    Distincte du cache HTTP (bien que partageant le même fichier SQLite) :
    ici on stocke le résultat métier final par parcelle (toutes les
    colonnes déjà résolues), afin de pouvoir reconstruire le fichier Excel
    de sortie sans recalculer une parcelle déjà entièrement traitée lors
    d'une exécution précédente interrompue.
    """

    def __init__(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = cache_dir / "http_cache.sqlite3"
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=30)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS parcelle_resultats (
                    commune TEXT NOT NULL,
                    identifiant TEXT NOT NULL,
                    valeurs TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (commune, identifiant)
                )
                """
            )
            # Mémorise, par rue, la liste des parcelles cadastrales SANS
            # adresse déjà découvertes (voir CadastreService::
            # _parcelles_cadastrales_sans_adresse) — pas le cache HTTP brut
            # (volontairement vidé avant de committer, voir le workflow
            # GitHub Actions), mais le RÉSULTAT de cette découverte,
            # beaucoup plus compact. Sans ça, cette découverte (PICC + une
            # dizaine de requêtes CADMAP par rue, ~20-30s/rue) devrait être
            # intégralement refaite à CHAQUE exécution — observé en
            # conditions réelles : ~36 minutes rien que pour les 194 rues de
            # Dour sur un cache neuf. `rues_verifiees` distingue "jamais
            # vérifiée" de "vérifiée, aucune parcelle sans adresse trouvée"
            # (liste vide légitime, à ne pas reverifier non plus).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rues_verifiees (
                    commune TEXT NOT NULL,
                    rue TEXT NOT NULL,
                    PRIMARY KEY (commune, rue)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS parcelles_sans_adresse (
                    commune TEXT NOT NULL,
                    rue TEXT NOT NULL,
                    capakey TEXT NOT NULL,
                    numero_cadastral TEXT,
                    code_postal TEXT,
                    geometry TEXT,
                    PRIMARY KEY (commune, rue, capakey)
                )
                """
            )
            # Ajoutées après coup (voir utils/geometrie.py) : `ALTER TABLE
            # ADD COLUMN` plutôt que `CREATE TABLE IF NOT EXISTS`, qui ne
            # touche jamais une table déjà existante — nécessaire pour que
            # les bases déjà commitées (Dour/Chimay/Colfontaine) restent
            # utilisables sans réinitialisation.
            colonnes = {row[1] for row in conn.execute("PRAGMA table_info(parcelles_sans_adresse)")}
            if "cote" not in colonnes:
                conn.execute("ALTER TABLE parcelles_sans_adresse ADD COLUMN cote TEXT")
            if "position_rue" not in colonnes:
                conn.execute("ALTER TABLE parcelles_sans_adresse ADD COLUMN position_rue REAL")
            # Une parcelle qui échoue en cours de traitement (voir
            # main.py::_traiter_jusqu_a_objectif) n'est délibérément PAS
            # enregistrée dans `parcelle_resultats` — c'est ce qui permet
            # à la redécouverte normale de la reprendre automatiquement au
            # prochain traitement, mélangée avec les parcelles jamais
            # encore rencontrées. Cette table sert un besoin différent :
            # pouvoir cibler et retraiter UNIQUEMENT les échecs (voir
            # main.py::retraiter_echecs), sans devoir retraiter toute la
            # commune pour les rattraper. Les champs stockés sont
            # exactement ceux nécessaires pour reconstruire une `Parcelle`
            # et refaire un rattachement cadastral complet (voir
            # CadastreService.point_pour_identifiant, qui retrouve x/y à
            # partir du seul `identifiant`).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS parcelles_echouees (
                    commune TEXT NOT NULL,
                    identifiant TEXT NOT NULL,
                    pays TEXT NOT NULL,
                    code_postal TEXT,
                    rue TEXT NOT NULL,
                    numero TEXT,
                    adr_id TEXT,
                    capakey TEXT,
                    derniere_erreur TEXT,
                    tentatives INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (commune, identifiant)
                )
                """
            )
            # Marque les rues déjà redécouvertes avec le rayon de recherche
            # élargi (voir CadastreService._RAYON_RECHERCHE_PARCELLES_M et
            # main.py::recalculer_cote_position, phase 1) — sans cette
            # table, cette phase (coûteuse : ~2 min/rue observé en
            # conditions réelles) referait le même travail à chaque
            # relance du rattrapage sur une commune déjà redécouverte,
            # pour ne jamais rien trouver de nouveau. `rues_verifiees`
            # existant ne peut pas servir à ça : il marque "a déjà été
            # découverte" (avec n'importe quel rayon, y compris l'ancien),
            # pas spécifiquement "sous le rayon élargi".
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rues_redecouvertes_rayon_elargi (
                    commune TEXT NOT NULL,
                    rue TEXT NOT NULL,
                    PRIMARY KEY (commune, rue)
                )
                """
            )
            conn.commit()

    def rue_deja_verifiee(self, commune: str, rue: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM rues_verifiees WHERE commune = ? AND rue = ?", (commune, rue),
            ).fetchone()
        return row is not None

    def rues_verifiees_commune(self, commune: str) -> List[str]:
        """Toutes les rues déjà vérifiées pour une commune — sert au
        rattrapage qui reparcourt les parcelles sans adresse déjà mises en
        cache (voir main.py::recalculer_cote_position)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT rue FROM rues_verifiees WHERE commune = ?", (commune,),
            ).fetchall()
        return [rue for (rue,) in rows]

    def rue_redecouverte_rayon_elargi(self, commune: str, rue: str) -> bool:
        """True si cette rue a déjà été redécouverte avec le rayon de
        recherche élargi (voir main.py::recalculer_cote_position, phase 1)
        — évite de refaire ce travail coûteux (~2 min/rue observé en
        conditions réelles) à chaque relance du rattrapage sur une commune
        déjà redécouverte."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM rues_redecouvertes_rayon_elargi WHERE commune = ? AND rue = ?",
                (commune, rue),
            ).fetchone()
        return row is not None

    def marquer_rue_redecouverte_rayon_elargi(self, commune: str, rue: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rues_redecouvertes_rayon_elargi (commune, rue) VALUES (?, ?)",
                (commune, rue),
            )
            conn.commit()

    def supprimer_parcelle_sans_adresse(self, commune: str, rue: str, capakey: str) -> None:
        """Retire une parcelle du cache de découverte 'sans adresse' — sert
        au rattrapage (voir `rues_verifiees_commune`) quand elle s'avère en
        réalité avoir une adresse ailleurs dans la commune."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM parcelles_sans_adresse WHERE commune = ? AND rue = ? AND capakey = ?",
                (commune, rue, capakey),
            )
            conn.commit()

    def supprimer_resultat(self, commune: str, identifiant: str) -> None:
        """Retire une parcelle déjà résolue de `parcelle_resultats` — sans
        effet si elle n'a jamais été résolue (donc jamais présente). Utilisé
        par `nettoyer_doublons_sans_adresse` : une parcelle sans adresse
        découverte à tort (elle a en fait une adresse sur une autre rue) ne
        doit plus apparaître du tout dans le fichier de sortie, pas
        seulement être corrigée."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM parcelle_resultats WHERE commune = ? AND identifiant = ?",
                (commune, identifiant),
            )
            conn.commit()

    def parcelles_sans_adresse(self, commune: str, rue: str) -> List[Dict[str, Any]]:
        """Parcelles sans adresse déjà découvertes pour cette rue (voir
        `rue_deja_verifiee` — à n'appeler que si True, sinon liste
        trompeusement vide pour une rue jamais vérifiée). `cote`/
        `position_rue` valent `None` pour une entrée enregistrée avant
        l'ajout de ce calcul (voir utils/geometrie.py) — pas encore
        recalculée, voir le passage de rattrapage dédié."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT capakey, numero_cadastral, code_postal, geometry, cote, position_rue "
                "FROM parcelles_sans_adresse WHERE commune = ? AND rue = ?",
                (commune, rue),
            ).fetchall()
        return [
            {
                "capakey": capakey,
                "numero_cadastral": numero_cadastral,
                "code_postal": code_postal,
                "geometry": json.loads(geometry) if geometry else None,
                "cote": cote,
                "position_rue": position_rue,
            }
            for capakey, numero_cadastral, code_postal, geometry, cote, position_rue in rows
        ]

    def enregistrer_parcelles_sans_adresse(
        self, commune: str, rue: str, parcelles: List[Dict[str, Any]],
    ) -> None:
        """Marque la rue comme vérifiée et enregistre les parcelles sans
        adresse trouvées (liste vide acceptée : signifie qu'il n'y en a
        réellement aucune sur cette rue, résultat à ne pas reperdre)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rues_verifiees (commune, rue) VALUES (?, ?)",
                (commune, rue),
            )
            for p in parcelles:
                conn.execute(
                    "INSERT OR REPLACE INTO parcelles_sans_adresse "
                    "(commune, rue, capakey, numero_cadastral, code_postal, geometry, cote, position_rue) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        commune, rue, p["capakey"], p.get("numero_cadastral"),
                        p.get("code_postal"), json.dumps(p.get("geometry")),
                        p.get("cote"), p.get("position_rue"),
                    ),
                )
            conn.commit()

    def maj_cote_position_sans_adresse(self, commune: str, rue: str, capakey: str, cote: str, position_rue: float) -> None:
        """Met à jour côté/position d'une parcelle sans adresse déjà
        enregistrée, sans toucher au reste (utilisé par le rattrapage —
        voir main.py::recalculer_cote_position)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE parcelles_sans_adresse SET cote = ?, position_rue = ? "
                "WHERE commune = ? AND rue = ? AND capakey = ?",
                (cote, position_rue, commune, rue, capakey),
            )
            conn.commit()

    def enregistrer_echec(self, commune: str, parcelle: Any, erreur: str) -> None:
        """Enregistre (ou met à jour, en incrémentant `tentatives`) l'échec
        d'une parcelle — voir main.py::_traiter_jusqu_a_objectif et
        ::retraiter_echecs. `parcelle` : `models.parcelle.Parcelle` (pas
        typé strictement ici pour éviter une dépendance circulaire)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO parcelles_echouees "
                "(commune, identifiant, pays, code_postal, rue, numero, adr_id, capakey, "
                "derniere_erreur, tentatives, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP) "
                "ON CONFLICT(commune, identifiant) DO UPDATE SET "
                "derniere_erreur = excluded.derniere_erreur, "
                "tentatives = tentatives + 1, "
                "updated_at = CURRENT_TIMESTAMP",
                (
                    commune, parcelle.identifiant, parcelle.pays, parcelle.code_postal,
                    parcelle.rue, parcelle.numero, parcelle.adr_id, parcelle.capakey, erreur,
                ),
            )
            conn.commit()

    def supprimer_echec(self, commune: str, identifiant: str) -> None:
        """Retire une parcelle du suivi des échecs — dès qu'elle réussit,
        que ce soit via le retraitement ciblé ou via le traitement normal
        (une parcelle en échec reste redécouvrable normalement aussi, voir
        `enregistrer_echec`)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM parcelles_echouees WHERE commune = ? AND identifiant = ?",
                (commune, identifiant),
            )
            conn.commit()

    def echecs_commune(self, commune: str) -> List[Dict[str, Any]]:
        """Toutes les parcelles actuellement en échec pour cette commune —
        voir main.py::retraiter_echecs."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT identifiant, pays, code_postal, rue, numero, adr_id, capakey, "
                "derniere_erreur, tentatives FROM parcelles_echouees WHERE commune = ?",
                (commune,),
            ).fetchall()
        return [
            {
                "identifiant": identifiant, "pays": pays, "code_postal": code_postal,
                "rue": rue, "numero": numero, "adr_id": adr_id, "capakey": capakey,
                "derniere_erreur": derniere_erreur, "tentatives": tentatives,
            }
            for identifiant, pays, code_postal, rue, numero, adr_id, capakey, derniere_erreur, tentatives in rows
        ]

    def get(self, commune: str, identifiant: str) -> Optional[dict]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT valeurs FROM parcelle_resultats WHERE commune = ? AND identifiant = ?",
                (commune, identifiant),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, commune: str, identifiant: str, valeurs: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO parcelle_resultats (commune, identifiant, valeurs) "
                "VALUES (?, ?, ?)",
                (commune, identifiant, json.dumps(valeurs)),
            )
            conn.commit()

    def all_for_commune(self, commune: str) -> dict:
        """Renvoie {identifiant: valeurs} pour toutes les parcelles déjà traitées."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT identifiant, valeurs FROM parcelle_resultats WHERE commune = ?",
                (commune,),
            ).fetchall()
        return {identifiant: json.loads(valeurs) for identifiant, valeurs in rows}
