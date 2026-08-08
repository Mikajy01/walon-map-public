"""Outil réservé au propriétaire : génère, liste ou révoque l'accès d'une
commune. Ne fait JAMAIS partie de l'exécutable distribué (voir
build-onefile.bat/build-onedir.bat — seul gui.py y est empaqueté ; ce
dossier `outils/` n'est jamais inclus).

Ce script ne modifie que la copie LOCALE `outils/autorisations.json`. Pour
qu'un changement (nouvel accès, révocation) prenne effet côté clients, il
faut ensuite publier son contenu manuellement vers l'emplacement public
configuré dans `utils/licence.py::URL_AUTORISATIONS` (ex: coller le
contenu dans le Gist GitHub public correspondant, et l'enregistrer) — la
publication n'est jamais automatique, pour garder un contrôle explicite
sur ce qui est rendu public.

Usage :
    python outils/gerer_licences.py generer --commune Chimay
    python outils/gerer_licences.py revoquer --commune Chimay
    python outils/gerer_licences.py lister
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.licence import hacher_mot_de_passe  # noqa: E402

FICHIER_AUTORISATIONS = Path(__file__).resolve().parent / "autorisations.json"


def _charger() -> dict:
    if FICHIER_AUTORISATIONS.exists():
        return json.loads(FICHIER_AUTORISATIONS.read_text(encoding="utf-8"))
    return {}


def _sauvegarder(autorisations: dict) -> None:
    FICHIER_AUTORISATIONS.write_text(
        json.dumps(autorisations, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def generer(commune: str) -> None:
    autorisations = _charger()
    sel = secrets.token_hex(8)
    mot_de_passe = secrets.token_hex(8)
    autorisations[commune.strip().lower()] = {
        "sel": sel,
        "hash": hacher_mot_de_passe(mot_de_passe, sel),
    }
    _sauvegarder(autorisations)
    print(f"Commune : {commune}")
    print(f"Mot de passe à transmettre à l'acheteur : {mot_de_passe}")
    print("(ce mot de passe n'est jamais stocké en clair — le noter maintenant, il ne sera plus jamais affiché)")
    print(f"\n{FICHIER_AUTORISATIONS} mis à jour — reste à publier manuellement (voir en-tête du script).")


def revoquer(commune: str) -> None:
    autorisations = _charger()
    cle = commune.strip().lower()
    if cle in autorisations:
        del autorisations[cle]
        _sauvegarder(autorisations)
        print(f"Accès révoqué pour '{commune}' — republiez le fichier pour que ça s'applique.")
    else:
        print(f"Aucun accès trouvé pour '{commune}'.")


def lister() -> None:
    autorisations = _charger()
    if not autorisations:
        print("Aucune commune autorisée.")
        return
    for commune in sorted(autorisations):
        print("-", commune)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gestion des accès par commune (outil propriétaire uniquement).")
    sous = parser.add_subparsers(dest="action", required=True)

    p_generer = sous.add_parser("generer", help="Génère un nouveau mot de passe pour une commune.")
    p_generer.add_argument("--commune", required=True)

    p_revoquer = sous.add_parser("revoquer", help="Révoque l'accès d'une commune.")
    p_revoquer.add_argument("--commune", required=True)

    sous.add_parser("lister", help="Liste les communes actuellement autorisées.")

    args = parser.parse_args()
    if args.action == "generer":
        generer(args.commune)
    elif args.action == "revoquer":
        revoquer(args.commune)
    elif args.action == "lister":
        lister()


if __name__ == "__main__":
    main()
