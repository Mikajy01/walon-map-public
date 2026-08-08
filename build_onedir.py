"""Construit l'exécutable portable Walonmap en dossier (PyInstaller
--onedir, moins susceptible d'être bloqué par un antivirus). Lancé via
`build-onedir.bat` (double-clic) ou directement `python build_onedir.py
[version]` — voir `build_common.py` pour le contexte (pourquoi un script
Python plutôt qu'un `.bat` complet)."""

import sys

import build_common as bc

print("=== Walonmap - Construction de l'exécutable portable (ONEDIR) ===\n")

bc.preparer_environnement()
version = bc.resoudre_version(sys.argv[1] if len(sys.argv) > 1 else None)
exe_path = bc.construire("onedir", version)

print("\n=== Terminé ===")
print(f"Dossier onedir (moins susceptible d'être bloqué par un antivirus) : {exe_path.parent}")
print(
    "\nPour distribuer l'application, copiez le dossier complet ci-dessus "
    "(avec son dossier data\\) sur la machine cible. Les dossiers cache\\, "
    "logs\\ et output\\ seront créés automatiquement à côté de l'exe au "
    "premier lancement. Aucune installation de Python n'est requise sur la "
    "machine cible."
)
