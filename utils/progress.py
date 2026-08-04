"""Barre de progression standardisée pour le traitement des parcelles."""

from __future__ import annotations

from typing import Iterable, Iterator, TypeVar

from tqdm import tqdm

T = TypeVar("T")


def progress(iterable: Iterable[T], total: int, description: str) -> Iterator[T]:
    yield from tqdm(iterable, total=total, desc=description, unit="parcelle")
