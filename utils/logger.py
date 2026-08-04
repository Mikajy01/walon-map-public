"""Configuration centralisée du logging (console + fichier + mode DEBUG)."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(logs_dir: Path, debug: bool = False) -> None:
    """Configure le logger racine une seule fois par exécution du programme.

    En mode DEBUG, chaque service ArcGIS interrogé, les paramètres envoyés,
    les réponses reçues et la raison de chaque valeur écrite sont journalisés
    (voir services/geoportail_service.py et services/layers_service.py).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    logs_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        logs_dir / "walonmap.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)  # le fichier garde toujours le détail complet
    root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
