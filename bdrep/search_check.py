"""Live check of the Google News search: run queries, score every result, report.

Nothing is stored and nothing is drafted. Use it to see what each query
actually returns and what the scoring keeps or throws away, before trusting
the daily digest:

    python -m bdrep search                       # every configured query
    python -m bdrep search --query '"arc flash" Chicago' --days 30
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import requests

from bdrep.config import Config
from bdrep.models import Lead
from bdrep.scoring import ScoreResult, score_lead
from bdrep.sources.base import SourceError, get, http_session
from bdrep.sources.news_rss import google_news_url, parse_feed

VERDICTS = ("qualified", "held", "near_miss", "low", "excluded")


@dataclass
class QueryReport:
    query: str
    url: str
    results: list[tuple[Lead, ScoreResult]] = field(default_factory=list)
    error: str = ""

    def counts(self) -> Counter:
        return Counter(score.verdict for _, score in self.results)


def run_search(
    cfg: Config,
    queries: list[str] | None = None,
    days: int = 7,
    session: requests.Session | None = None,
) -> list[QueryReport]:
    opts = cfg.source("news_rss")
    queries = queries or list(opts.get("google_news_queries") or [])
    session = session or http_session()
    timeout = float(opts.get("timeout_seconds", 30))
    targets = list(cfg.states)
    reports: list[QueryReport] = []
    for query in queries:
        report = QueryReport(query, google_news_url(query, days))
        try:
            resp = get(session, report.url, timeout=timeout)
            leads = parse_feed(resp.text, targets)
        except SourceError as exc:
            report.error = str(exc)
        else:
            scored = [(lead, score_lead(lead, cfg)) for lead in leads]
            report.results = sorted(scored, key=lambda pair: (-pair[1].total, pair[0].title))
        reports.append(report)
    return reports


def _why(score: ScoreResult) -> str:
    if score.verdict == "excluded":
        return "; ".join(score.reasons)
    bits = []
    if score.service_label:
        terms = ", ".join(score.strong_terms + score.weak_terms)
        bits.append(f"{score.service_label}: {terms}")
    bits += score.reasons
    return "; ".join(bits)


def format_report(reports: list[QueryReport], *, limit: int = 15, kept_only: bool = False) -> str:
    out: list[str] = []
    for i, report in enumerate(reports, 1):
        out.append(f"Query {i}: {report.query}")
        out.append(f"  {report.url}")
        if report.error:
            out.append(f"  ERROR: {report.error}")
            out.append("")
            continue
        counts = report.counts()
        tally = ", ".join(f"{counts[v]} {v.replace('_', ' ')}" for v in VERDICTS if counts[v])
        out.append(f"  {len(report.results)} results" + (f": {tally}" if tally else ""))
        shown = [r for r in report.results if not kept_only or r[1].verdict in ("qualified", "held", "near_miss")]
        for lead, score in shown[:limit]:
            out.append(
                f"  {score.total:>3}  {score.verdict:<9}  {lead.state or '?':<2}  {lead.posted_date or '----------':<10}  "
                f"{lead.title}" + (f"  [{lead.organization}]" if lead.organization else "")
            )
            why = _why(score)
            if why:
                out.append(f"                 {why}")
        if len(shown) > limit:
            out.append(f"  ... {len(shown) - limit} more (raise --limit to see them)")
        out.append("")

    out.append("Summary")
    header = f"  {'#':>2}  {'results':>7}  " + "  ".join(f"{v.replace('_', ' '):>9}" for v in VERDICTS)
    out.append(header)
    unique: set[str] = set()
    total = 0
    for i, report in enumerate(reports, 1):
        if report.error:
            out.append(f"  {i:>2}  {'ERROR':>7}")
            continue
        counts = report.counts()
        total += len(report.results)
        unique.update(lead.source_id for lead, _ in report.results)
        out.append(f"  {i:>2}  {len(report.results):>7}  " + "  ".join(f"{counts[v]:>9}" for v in VERDICTS))
    out.append(f"  {len(unique)} unique results across queries ({total - len(unique)} returned by more than one query)")
    return "\n".join(out)
