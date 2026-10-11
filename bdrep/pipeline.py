"""Orchestration: ingest -> score. Drafting and the digest live in their own modules."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Mapping

from bdrep.config import Config
from bdrep.scoring import has_service_signal, score_lead
from bdrep.sources import REGISTRY, Source, SourceError
from bdrep.store import Store, lead_from_row

log = logging.getLogger(__name__)


@dataclass
class SourceReport:
    name: str
    status: str                      # ok | partial | error | skipped
    counts: dict[str, int] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


def open_store(cfg: Config, path: str | None = None) -> Store:
    scfg = cfg.get("store", {})
    return Store(
        cfg.path(path or scfg.get("path", "data/leads.db")),
        dedupe_window_days=int(scfg.get("dedupe_window_days", 120)),
        title_similarity=float(scfg.get("title_similarity", 0.88)),
    )


def fetch_window(store: Store, name: str, opts: Mapping, today: dt.date) -> dt.date:
    """From the day before the last successful fetch (overlap is deduped),
    or `lookback_days` on the first run; never wider than `max_window_days`."""
    last = store.get_meta(f"{name}:last_success")
    if last:
        since = dt.date.fromisoformat(last) - dt.timedelta(days=1)
    else:
        since = today - dt.timedelta(days=int(opts.get("lookback_days", 2)))
    earliest = today - dt.timedelta(days=int(opts.get("max_window_days", 30)))
    return max(since, earliest)


def ingest(
    store: Store,
    cfg: Config,
    today: dt.date,
    only: list[str] | None = None,
    sources: Mapping[str, Source] | None = None,
) -> list[SourceReport]:
    reports: list[SourceReport] = []
    for name, cls in REGISTRY.items():
        opts = cfg.source(name)
        if only and name not in only:
            continue
        if not opts.get("enabled", True):
            reports.append(SourceReport(name, "skipped", messages=["disabled in config"]))
            continue
        since = fetch_window(store, name, opts, today)

        def worth_details(lead, name=name) -> bool:
            # Spend extra requests only on new, on-topic items.
            return has_service_signal(lead, cfg) and not store.has_description(name, lead.source_id)

        try:
            src = sources[name] if sources and name in sources else cls(cfg, used_today=store.requests_on(today, name))
            result = src.fetch(since, today, prefilter=worth_details)
        except SourceError as exc:
            log.warning("%s failed: %s", name, exc)
            store.record_run(today, name, "error", {}, str(exc))
            store.commit()
            reports.append(SourceReport(name, "error", messages=[str(exc)]))
            continue

        counts = {"fetched": len(result.leads), "new": 0, "merged": 0, "seen": 0, "requests": result.requests}
        for lead in result.leads:
            outcome, _ = store.upsert(lead, today)
            counts[outcome] += 1
        status = "partial" if result.errors else "ok"
        messages = result.errors + result.notes
        store.record_run(today, name, status, counts, "; ".join(messages))
        store.set_meta(f"{name}:last_success", today.isoformat())
        store.commit()
        reports.append(SourceReport(name, status, counts, messages))
    return reports


def score_all(store: Store, cfg: Config) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in store.unscored():
        result = score_lead(lead_from_row(row), cfg)
        store.set_score(row["id"], result.total, result.verdict, result.to_dict())
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    store.commit()
    return counts
