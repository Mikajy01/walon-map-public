"""Vérification d'accès en ligne, par commune, avant de lancer un
traitement dans le GUI — empêche qu'une copie vendue pour une commune soit
revendue/réutilisée pour une autre.

Le mot de passe est saisi une seule fois par installation (sauvegardé
localement à côté de l'exécutable) puis revalidé automatiquement à chaque
lancement auprès d'un fichier JSON public (jamais le mot de passe en
clair, seulement un hash salé par entrée, voir `hacher_mot_de_passe`).
Révoquer l'accès d'une commune se fait en éditant ce fichier public (voir
`outils/gerer_licences.py`, réservé au propriétaire) — la copie déjà
distribuée cesse de fonctionner dès son prochain lancement, qu'elle ait
été revendue ou non, sans rien changer côté client. Nécessite une
connexion internet à chaque lancement (vérification volontairement en
échec fermé : pas de connexion = pas d'accès).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Tuple

import requests

import config
from utils.logger import get_logger

_logger = get_logger("utils.licence")

# Fichier JSON public (lisible sans authentification, ex: Gist GitHub
# public) listant, par commune (clé en minuscules), le sel et le hash du
# mot de passe attendu — jamais le mot de passe en clair. Voir
# outils/gerer_licences.py pour le générer/mettre à jour.
URL_AUTORISATIONS = "https://gist.githubusercontent.com/Mikajy01/3de5b42ba9bce38ca043d41afedc6318/raw/autorisations.json"

_NOM_FICHIER_LICENCE_LOCALE = ".licence"


def _chemin_licence_locale() -> Path:
    return config.BASE_DIR / _NOM_FICHIER_LICENCE_LOCALE


def charger_mot_de_passe_local() -> Optional[str]:
    chemin = _chemin_licence_locale()
    if not chemin.exists():
        return None
    contenu = chemin.read_text(encoding="utf-8").strip()
    return contenu or None


def sauvegarder_mot_de_passe_local(mot_de_passe: str) -> None:
    _chemin_licence_locale().write_text(mot_de_passe, encoding="utf-8")


def hacher_mot_de_passe(mot_de_passe: str, sel: str) -> str:
    return hashlib.sha256((sel + mot_de_passe).encode("utf-8")).hexdigest()


def verifier_acces_en_ligne(commune: str, mot_de_passe: str) -> Tuple[bool, str]:
    """Vérifie `mot_de_passe` pour `commune` auprès du fichier
    d'autorisations en ligne. Renvoie (True, "") si l'accès est valide,
    sinon (False, message d'erreur explicite pour l'utilisateur)."""
    try:
        reponse = requests.get(URL_AUTORISATIONS, timeout=15)
        reponse.raise_for_status()
        autorisations = reponse.json()
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Échec de la vérification d'accès en ligne pour '%s'", commune)
        return False, (
            "Impossible de vérifier l'accès (connexion internet requise pour lancer "
            f"l'application). Détail technique : {exc}"
        )

    entree = autorisations.get(commune.strip().lower())
    if not entree:
        return False, f"Aucun accès autorisé pour la commune '{commune}'."

    if hacher_mot_de_passe(mot_de_passe, entree.get("sel", "")) != entree.get("hash"):
        return False, "Mot de passe incorrect pour cette commune."

    return True, ""


def verifier_et_enregistrer(commune: str, mot_de_passe: str) -> Tuple[bool, str]:
    """Comme `verifier_acces_en_ligne`, mais sauvegarde localement le mot
    de passe si (et seulement si) la vérification réussit — pour ne plus
    jamais le redemander tant qu'il reste valide."""
    ok, message = verifier_acces_en_ligne(commune, mot_de_passe)
    if ok:
        sauvegarder_mot_de_passe_local(mot_de_passe)
    return ok, message
