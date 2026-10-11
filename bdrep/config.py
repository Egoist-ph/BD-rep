"""Config loading. The YAML file is the single place to tune the pipeline."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class Config:
    data: dict[str, Any]
    base_dir: Path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def states(self) -> dict[str, dict[str, str]]:
        return self.data["states"]

    @property
    def scoring(self) -> dict[str, Any]:
        return self.data["scoring"]

    def source(self, name: str) -> dict[str, Any]:
        return self.data.get("sources", {}).get(name, {})

    def path(self, value: str | Path) -> Path:
        """Resolve a configured path relative to the config file."""
        p = Path(value)
        return p if p.is_absolute() else self.base_dir / p

    def today(self) -> dt.date:
        tz = ZoneInfo(self.data.get("timezone", "America/Los_Angeles"))
        return dt.datetime.now(tz).date()

    def license_status(self, state: str | None) -> str:
        """'licensed', 'pending', 'unknown' (state not detected) or 'out_of_area'."""
        if not state:
            return "unknown"
        info = self.states.get(state)
        if info is None:
            return "out_of_area"
        return info.get("license", "licensed")


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Config(data=data, base_dir=p.resolve().parent)
