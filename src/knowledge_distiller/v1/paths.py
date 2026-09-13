"""Shared application storage paths; importing this never starts an engine."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    data_root: Path

    @property
    def database(self) -> Path:
        return self.data_root / "knowledge.sqlite3"

    @property
    def runtime(self) -> Path:
        return self.data_root / "runtime"

    @classmethod
    def mac_default(cls) -> AppPaths:
        return cls(
            Path.home()
            / "Library"
            / "Application Support"
            / "Knowledge Distiller"
        )

    @classmethod
    def system_default(cls) -> AppPaths:
        import os
        if os.name == 'nt':
            return cls(Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'Knowledge Distiller')
        return cls.mac_default()

