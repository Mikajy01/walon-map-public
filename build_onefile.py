"""Construit l'exécutable portable Walonmap en un seul fichier (PyInstaller
--onefile). Lancé via `build-onefile.bat` (double-clic) ou directement
`python build_onefile.py [version]` — voir `build_common.py` pour le
contexte (pourquoi un script Python plutôt qu'un `.bat` complet)."""

import sys

import build_common as bc

print("=== Walonmap - Construction de l'exécutable portable (ONEFILE) ===\n")

bc.preparer_environnement()
version = bc.resoudre_version(sys.argv[1] if len(sys.argv) > 1 else None)
exe_path = bc.construire("onefile", version)

print("\n=== Terminé ===")
print(f"Exécutable onefile (un seul .exe) : {exe_path}")
print(
    "\nPour distribuer l'application, copiez dist\\onefile\\ avec son dossier "
    "data\\ sur la machine cible. Les dossiers cache\\, logs\\ et output\\ "
    "seront créés automatiquement à côté de l'exe au premier lancement. "
    "Aucune installation de Python n'est requise sur la machine cible."
)
print(
    "\nNOTE : un .exe onefile déclenche parfois un faux positif antivirus "
    "(comportement heuristique proche d'un dropper) - utilisez "
    "build-onedir.bat si c'est le cas sur la machine cible."
)
