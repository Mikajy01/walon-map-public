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
from typing import Any, Optional

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
            conn.commit()

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
