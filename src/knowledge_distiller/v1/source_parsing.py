from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class SourceReadError(RuntimeError):
    def __init__(self, code: str, *, retryable=False):
        super().__init__(code)
        self.retryable = retryable


@dataclass(frozen=True)
class ParsedMedia:
    member_id: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class ParsedSource:
    snapshot: str
    metadata: Mapping[str, object]
    lineage: Mapping[str, object]
    media: tuple[ParsedMedia, ...] = ()
    uncertainties: tuple[Mapping[str, object], ...] = ()
