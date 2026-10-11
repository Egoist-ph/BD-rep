"""The normalized lead record every source adapter produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Lead:
    source: str                  # adapter name, e.g. "sam_gov"
    source_id: str               # stable id within that source
    title: str                   # project name as published
    url: str = ""
    description: str = ""
    address: str = ""            # street address when the source gives one
    city: str = ""
    state: str | None = None     # two-letter code, None when unknown
    zip: str = ""
    organization: str = ""       # agency, owner or publisher
    notice_type: str = ""        # e.g. "Solicitation", "News"
    set_aside: str = ""
    naics: str = ""
    posted_date: str = ""        # ISO date
    deadline: str = ""           # ISO date or datetime
    contact: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def location(self) -> str:
        parts = [p for p in (self.address, self.city, self.state) if p]
        return ", ".join(parts)
