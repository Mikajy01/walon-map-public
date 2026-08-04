"""Politique de réessai commune pour tous les appels réseau du projet."""

from __future__ import annotations

import logging

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from services.exceptions import ArcGISTransientError
from utils.logger import get_logger

_logger = get_logger("utils.retry")

# Erreurs considérées comme temporaires : timeouts, coupures de connexion,
# erreurs serveur 5xx (via requests.exceptions.HTTPError levée par
# response.raise_for_status()), et erreurs ArcGIS >= 500 renvoyées en HTTP
# 200 avec le code dans le corps JSON (voir services/geoportail_service.py
# et services/exceptions.py — ex: "Service not started.", observé en
# production sur le service LOT/GCU, résolu par un simple réessai).
#
# Un HTTPError 4xx (ex: 414 Request-URI Too Large, observé en production
# sur le service PASH avec une géométrie cadastrale très complexe) est en
# revanche définitif : la même requête échoue de façon identique à chaque
# tentative, réessayer ne fait que perdre du temps (jusqu'à ~30s de
# backoff) sans jamais pouvoir réussir. Seuls les HTTPError 5xx (erreur
# serveur, potentiellement transitoire) sont donc réessayés.
def _est_reessayable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ArcGISTransientError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status is not None and status >= 500
    return False


def http_retry(max_attempts: int = 5):
    """Décorateur de réessai avec backoff exponentiel pour les appels HTTP.

    Ne réessaie que sur des erreurs transitoires ; une erreur applicative
    définitive (ex: JSON invalide, couche introuvable, requête ArcGIS
    invalide, HTTPError 4xx) n'est jamais masquée par un réessai silencieux.
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception(_est_reessayable),
        before_sleep=before_sleep_log(_logger, logging.WARNING),
    )
