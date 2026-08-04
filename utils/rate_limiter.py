"""Limitation de débit simple pour les appels HTTP vers les services du Géoportail."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Impose un intervalle minimal entre deux requêtes sortantes.

    Implémentation volontairement simple (pas de file d'attente ni de
    burst) : un verrou + un timestamp partagé suffisent puisque le projet
    n'utilise que peu de threads en parallèle (voir services/geoportail_service.py).
    """

    def __init__(self, max_requests_per_second: float) -> None:
        if max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second doit être > 0")
        self._min_interval = 1.0 / max_requests_per_second
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_call = time.monotonic()
