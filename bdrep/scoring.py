"""Rule-based lead scoring: service-line match + state + real-project signals.

Deterministic on purpose: every point is traceable to a matched term in the
config, so a false positive is fixed by editing a keyword list, not a prompt.
Total is 0-100:

    service (0-50)  strongest service line; weak-only matches cap at 25
    state   (0-20)  licensed state 20, license pending 10, not detected 5
    project (0-30)  notice type, procurement/stage/facility words, quantities
    minus penalties (scope Roben does not sell; noise words)

Verdicts:
    excluded   hard-exclude term matched, or project is out of the target states
    qualified  >= threshold and at least one strong service term -> gets a draft
    held       would qualify, but the state's license is pending -> no draft
    near_miss  within near_miss_margin below threshold (or weak-only) -> listed
    low        everything else
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from bdrep.config import Config
from bdrep.models import Lead
from bdrep.normalize import match_terms, norm_text

QUANTITY = re.compile(
    r"\$\s?\d[\d,.]*\s*(?:million|billion|m|bn|k)?\b"
    r"|\b\d[\d,.]*\s*(?:square feet|sq\.?\s?ft|sf|mw|mwh|kv|kva|mva)\b",
    re.IGNORECASE,
)


@dataclass
class ScoreResult:
    total: int
    verdict: str
    service: int = 0
    state: int = 0
    project: int = 0
    penalty: int = 0
    service_line: str | None = None
    service_label: str = ""
    strong_terms: list[str] = field(default_factory=list)
    weak_terms: list[str] = field(default_factory=list)
    project_signals: list[str] = field(default_factory=list)
    license: str = "unknown"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _haystack(lead: Lead) -> str:
    return norm_text(f"{lead.title} {lead.description}")


def _service(text: str, lead: Lead, sc: dict[str, Any]) -> tuple[int, str | None, list[str], list[str]]:
    best: tuple[int, str | None, list[str], list[str]] = (0, None, [], [])
    cap = int(sc.get("service_max", 50))
    for key, line in sc["service_lines"].items():
        strong = match_terms(text, line.get("strong", []))
        weak = match_terms(text, line.get("weak", []))
        if strong:
            points = 35 + 5 * (len(strong) - 1) + 3 * len(weak)
        else:
            points = min(25, 8 * len(weak))
        if points > best[0]:
            best = (min(cap, points), key, strong, weak)
    points, key, strong, weak = best
    if key and lead.naics and lead.naics in {str(n) for n in sc.get("naics_weak", [])}:
        weak = weak + [f"NAICS {lead.naics}"]
        points = min(cap, points + (3 if strong else 8))
        if not strong:
            points = min(points, 25)
    return points, key, strong, weak


def _project(text: str, lead: Lead, sc: dict[str, Any]) -> tuple[int, list[str]]:
    signals = sc.get("project_signals", {})
    points = 0
    found: list[str] = []
    type_points = sc.get("notice_type_points", {})
    if lead.notice_type in type_points:
        points += int(type_points[lead.notice_type])
        found.append(f"notice: {lead.notice_type}")
    for group, value in (("procurement", 12), ("stage", 6), ("facility", 6)):
        hits = match_terms(text, signals.get(group, []))
        if hits:
            points += value
            found.append(f"{group}: {', '.join(hits[:3])}")
    quantities = QUANTITY.findall(f"{lead.title} {lead.description}")
    if quantities:
        points += int(sc.get("quantity_points", 6))
        found.append(f"quantity: {quantities[0].strip()}")
    noise = match_terms(text, signals.get("noise", []))
    if noise:
        points -= 15
        found.append(f"noise: {', '.join(noise)}")
    return max(0, min(int(sc.get("project_max", 30)), points)), found


def score_lead(lead: Lead, cfg: Config) -> ScoreResult:
    sc = cfg.scoring
    text = _haystack(lead)
    license_status = cfg.license_status(lead.state)

    excluded = match_terms(text, sc.get("exclude", []))
    if excluded:
        return ScoreResult(0, "excluded", license=license_status, reasons=[f"excluded: {', '.join(excluded)}"])
    if license_status == "out_of_area":
        return ScoreResult(0, "excluded", license=license_status, reasons=[f"out of area: {lead.state}"])

    service, line, strong, weak = _service(text, lead, sc)
    state = int(sc.get("state_points", {}).get(license_status, 0))
    project, signals = _project(text, lead, sc)

    reasons: list[str] = []
    penalty = 0
    pen = sc.get("penalize", {})
    pen_hits = match_terms(text, pen.get("terms", []))
    if pen_hits:
        penalty += int(pen.get("points", 15))
        reasons.append(f"scope mismatch: {', '.join(pen_hits)}")
    line_cfg = sc["service_lines"].get(line, {}) if line else {}
    line_pen = line_cfg.get("penalize_if", {})
    line_hits = match_terms(text, line_pen.get("terms", []))
    if line_hits:
        penalty += int(line_pen.get("points", 15))
        reasons.append(f"{line_pen.get('reason', 'service caution')}: {', '.join(line_hits)}")

    total = max(0, min(100, service + state + project - penalty))
    threshold = int(sc.get("threshold", 60))
    margin = int(sc.get("near_miss_margin", 15))

    if not line:
        verdict = "low"
        reasons.append("no service-line terms")
    elif total >= threshold and strong:
        verdict = "held" if license_status == "pending" else "qualified"
    elif total >= threshold - margin:
        verdict = "near_miss"
        if not strong:
            reasons.append("no direct service term (weak match only)")
        if total < threshold:
            reasons.append(f"{threshold - total} points below threshold")
    else:
        verdict = "low"
    if license_status == "unknown":
        reasons.append("state not detected")

    return ScoreResult(
        total=total, verdict=verdict, service=service, state=state, project=project,
        penalty=penalty, service_line=line, service_label=line_cfg.get("label", ""),
        strong_terms=strong, weak_terms=weak, project_signals=signals,
        license=license_status, reasons=reasons,
    )


def has_service_signal(lead: Lead, cfg: Config) -> bool:
    """Cheap pre-check used to decide whether to spend a request on details."""
    sc = cfg.scoring
    text = _haystack(lead)
    if match_terms(text, sc.get("exclude", [])) or cfg.license_status(lead.state) == "out_of_area":
        return False
    return any(
        match_terms(text, line.get("strong", []) + line.get("weak", []))
        for line in sc["service_lines"].values()
    )
