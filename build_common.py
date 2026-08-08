"""Logique partagée entre `build_onefile.py` et `build_onedir.py` — appelée
depuis un script Python plutôt qu'un `.bat` : un script `.bat` combinant
`pip install` + un appel PyInstaller (subprocess long) déclenchait de façon
fiable une erreur cmd.exe ("'.' était inattendu") juste après la fin
propre de PyInstaller, dès que cmd.exe reprenait la lecture du script pour
continuer — reproduit même dans un script `.bat` réduit à un seul appel
PyInstaller (donc pas une histoire de longueur/complexité du script), même
en écartant le fichier `.spec` du dossier du projet (`--specpath`), même
selon différentes méthodes d'invocation (cmd.exe natif, PowerShell direct).
Cause exacte jamais identifiée. Python n'a pas ce mécanisme de lecture
paresseuse d'un fichier `.bat` pendant qu'un sous-processus tourne, donc
structurellement pas exposé à cette classe de bug.

Les `.bat` (`build-onefile.bat`/`build-onedir.bat`) ne servent plus qu'à
lancer ce module d'un double-clic, sans aucune autre logique."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

RACINE = pathlib.Path(__file__).resolve().parent
VENV_DIR = RACINE / ".venv_build"
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"


def _executer(commande: list, **kwargs) -> None:
    print("+ " + " ".join(str(c) for c in commande))
    resultat = subprocess.run(commande, cwd=RACINE, **kwargs)
    if resultat.returncode != 0:
        print(f"[ERREUR] Commande échouée (code {resultat.returncode}).")
        sys.exit(1)


def preparer_environnement() -> None:
    """Crée `.venv_build` si absent, installe les dépendances execution +
    build dedans — jamais dans l'environnement Python courant, qui pourrait
    contenir des bibliothèques sans rapport (autres projets) que PyInstaller
    embarquerait par erreur."""
    if not VENV_PYTHON.exists():
        print(f"Création d'un environnement virtuel dédié au build — dossier {VENV_DIR.name}...")
        _executer([sys.executable, "-m", "venv", str(VENV_DIR)])

    print("Installation des dépendances (exécution + build) dans l'environnement dédié...")
    _executer([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"], stdout=subprocess.DEVNULL)
    _executer([
        str(VENV_PYTHON), "-m", "pip", "install",
        "-r", "requirements.txt", "-r", "requirements-build.txt",
    ])


def _version_actuelle() -> str:
    config_path = RACINE / "config.py"
    texte = config_path.read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION = "(.*?)"', texte)
    if not m:
        print("[ERREUR] Impossible de lire config.APP_VERSION dans config.py.")
        sys.exit(1)
    return m.group(1)


def resoudre_version(argument: str | None) -> str:
    """Détermine la version à construire — reprise de config.APP_VERSION,
    en argument (build non interactif) ou saisie interactive (Entrée pour
    garder l'actuelle) — puis met à jour config.py si elle change."""
    version_actuelle = _version_actuelle()

    if argument:
        nouvelle_version = argument
        print(f"Version indiquée en argument : {nouvelle_version}")
    else:
        saisie = input(
            f"Version actuelle : {version_actuelle} — nouvelle version à construire "
            f"(Entrée pour garder) : "
        ).strip()
        nouvelle_version = saisie or version_actuelle

    if nouvelle_version != version_actuelle:
        print(f'Mise à jour de config.py : APP_VERSION = "{nouvelle_version}"...')
        config_path = RACINE / "config.py"
        texte = config_path.read_text(encoding="utf-8")
        texte2, n = re.subn(
            r'APP_VERSION = ".*?"', f'APP_VERSION = "{nouvelle_version}"', texte, count=1,
        )
        assert n == 1
        config_path.write_text(texte2, encoding="utf-8")

    print(f"Version : {nouvelle_version}")
    return nouvelle_version


def construire(mode: str, version: str) -> pathlib.Path:
    """Lance PyInstaller (`mode` = "onefile" ou "onedir") pour cette
    version, nettoie le build précédent du même mode, copie
    `data/liens_communaux.csv`. Renvoie le chemin de l'exécutable produit."""
    assert mode in ("onefile", "onedir")

    dist_dir = RACINE / "dist" / mode
    build_dir = RACINE / "build" / mode
    nom = f"Walonmap-{version}"

    print(f"\nNettoyage du build {mode} précédent...")
    for dossier in (dist_dir, build_dir):
        if dossier.exists():
            import shutil
            shutil.rmtree(dossier)

    print(f"\nConstruction {mode.upper()} (PyInstaller --{mode} --windowed)...")
    _executer([
        str(VENV_DIR / "Scripts" / "pyinstaller.exe"),
        f"--{mode}", "--windowed",
        "--name", nom,
        "--collect-all", "customtkinter",
        "--distpath", str(dist_dir),
        "--workpath", str(build_dir),
        "--specpath", str(build_dir),
        "gui.py",
    ])

    if mode == "onefile":
        cible_data = dist_dir / "data"
        exe_path = dist_dir / f"{nom}.exe"
    else:
        cible_data = dist_dir / nom / "data"
        exe_path = dist_dir / nom / f"{nom}.exe"

    cible_data.mkdir(parents=True, exist_ok=True)
    source_csv = RACINE / "data" / "liens_communaux.csv"
    cible_csv = cible_data / "liens_communaux.csv"
    if source_csv.exists() and not cible_csv.exists():
        import shutil
        shutil.copy(source_csv, cible_csv)

    return exe_path
