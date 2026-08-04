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

    # Repartir d'un fichier vide à chaque exécution plutôt que d'empiler sur
    # les logs des exécutions précédentes. Utile pour déboguer la dernière
    # exécution (l'usage réel qu'on en a fait jusqu'ici — voir l'historique
    # du dépôt), inutile de conserver un historique illimité qui grossirait
    # indéfiniment (observé : ~1,2 Mo pour seulement quelques exécutions de
    # test) une fois commité à chaque exécution GitHub Actions.
    #
    # `RotatingFileHandler` ignore silencieusement `mode="w"` dès que
    # `maxBytes > 0` (il impose 'a', nécessaire à sa propre logique de
    # rotation par taille — vérifié : le fichier n'était PAS vidé avec
    # mode="w" seul) : on tronque donc le fichier explicitement avant de
    # construire le handler, qui repart alors bien de zéro. `backupCount=1`
    # reste comme garde-fou si UNE SEULE exécution dépasse 10 Mo (peu
    # probable : une commune entière ne génère que quelques centaines de Ko).
    log_path = logs_dir / "walonmap.log"
    log_path.write_text("", encoding="utf-8")
    file_handler = RotatingFileHandler(
        log_path, mode="a", maxBytes=10_000_000, backupCount=1, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)  # le fichier garde toujours le détail complet
    root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
