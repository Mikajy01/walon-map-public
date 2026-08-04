"""Exceptions partagées entre `geoportail_service.py` (qui les lève) et
`utils/retry.py` (qui inclut les transitoires dans sa politique de
réessai) — isolées dans ce module pour éviter tout import circulaire
entre les deux (retry.py ne doit pas dépendre de geoportail_service.py,
qui dépend déjà de retry.py pour le décorateur `http_retry`)."""

from __future__ import annotations


class ArcGISServiceError(RuntimeError):
    """Erreur applicative renvoyée par un service ArcGIS (payload JSON
    `error`), considérée comme définitive (ex: requête/couche invalide) —
    jamais réessayée automatiquement."""


class ArcGISTransientError(ArcGISServiceError):
    """Variante de `ArcGISServiceError` pour un code d'erreur ArcGIS >= 500.

    ArcGIS répond parfois avec un HTTP 200 contenant un `error.code` de
    type serveur (500) dans le corps JSON plutôt qu'un vrai statut HTTP
    5xx — `response.raise_for_status()` ne le détecte donc jamais. Observé
    en production sur des messages clairement transitoires ("Service not
    started.", "Error performing query operation") qui se résolvent en général
    en réessayant. Incluse dans `utils.retry.RETRYABLE_EXCEPTIONS`."""
