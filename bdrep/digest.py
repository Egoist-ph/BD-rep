"""Digest step: one markdown file per day with every lead worth a look.

Each lead appears in exactly one digest (the first one after it was scored).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from bdrep.config import Config
from bdrep.store import Store

SOURCE_LABELS = {"sam_gov": "SAM.gov", "news_rss": "News"}


def _cell(text: Any, limit: int = 400) -> str:
    text = str(text or "").replace("|", "/").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _json(row: sqlite3.Row, col: str) -> Any:
    return json.loads(row[col]) if row[col] else {}


def _location(row: sqlite3.Row) -> str:
    return ", ".join(p for p in (row["city"], row["state"]) if p)


def _sources(row: sqlite3.Row) -> str:
    names = []
    for ref in json.loads(row["sources_json"]):
        label = SOURCE_LABELS.get(ref["source"], ref["source"])
        if label not in names:
            names.append(label)
    return " + ".join(names)


def _lead_block(n: int, row: sqlite3.Row, cfg: Config, *, with_draft: bool) -> list[str]:
    score = _json(row, "score_json")
    meta = _json(row, "draft_meta")
    contact = _json(row, "contact_json")
    extra = _json(row, "extra_json")
    loc = _location(row)
    title = f"### {n}. {row['title']}" + (f" — {loc}" if loc else "")
    out = [title, ""]
    out.append(
        f"Score **{row['score']}** (service {score.get('service', 0)} · state {score.get('state', 0)} "
        f"· project {score.get('project', 0)}"
        + (f" · penalty -{score['penalty']}" if score.get("penalty") else "")
        + f") · lead #{row['id']}"
    )
    out.append("")
    if row["why"]:
        out.append(f"- **Why it matters:** {row['why']}")
    terms = score.get("strong_terms", []) + score.get("weak_terms", [])
    if score.get("service_label"):
        out.append(f"- **Service:** {score['service_label']}" + (f" — matched {', '.join(terms)}" if terms else ""))
    source_bits = [_sources(row)]
    for value in (row["notice_type"], row["set_aside"], f"NAICS {row['naics']}" if row["naics"] else ""):
        if value and value != "News":
            source_bits.append(value)
    if row["organization"]:
        source_bits.append(row["organization"])
    out.append(f"- **Source:** {' · '.join(source_bits)}")
    dates = []
    if row["posted_date"]:
        dates.append(f"posted {row['posted_date'][:10]}")
    if row["deadline"]:
        dates.append(f"**responses due {row['deadline'][:16].replace('T', ' ')}**")
    if dates:
        out.append(f"- **Dates:** {' · '.join(dates)}")
    if extra.get("awardee"):
        out.append(f"- **Awardee:** {extra['awardee']}" + (f" ({_money(extra['award_amount'])})" if extra.get("award_amount") else ""))
    recipient = meta.get("recipient")
    if recipient:
        out.append(f"- **Send to:** {recipient}")
    elif contact:
        out.append(f"- **Contact on notice:** {', '.join(contact.values())}")
    if row["url"]:
        out.append(f"- **Link:** {row['url']}")
    if extra.get("state_from"):
        out.append(f"- **State inferred from:** {extra['state_from']} (check place of performance)")
    if score.get("reasons"):
        out.append(f"- **Scoring notes:** {'; '.join(score['reasons'])}")
    line_note = cfg.scoring["service_lines"].get(score.get("service_line") or "", {}).get("note")
    if line_note:
        out.append(f"- **Service caveat:** {line_note}")
    state_note = cfg.states.get(row["state"] or "", {}).get("note")
    if not row["state"]:
        licensed = ", ".join(c for c, s in cfg.states.items() if s.get("license") == "licensed")
        state_note = f"Location not detected. Confirm the project is in a licensed state ({licensed}) before sending."
    if state_note:
        out.append(f"- **State note:** {state_note}")
    for note in meta.get("notes", []):
        out.append(f"- **Pipeline note:** {note}")
    out.append("")
    if with_draft and row["draft"]:
        label = "Draft by Claude" if meta.get("method") == "llm" else "Template draft"
        out += [f"**{label}** — edit before sending:", "", "```text", row["draft"], "```", ""]
    return out


def weekly_stats(store: Store, today: dt.date, days: int = 7) -> dict[str, Any]:
    start = (today - dt.timedelta(days=days - 1)).isoformat()
    # Leads waiting for today's digest count too.
    rows = store.conn.execute(
        """SELECT verdict, review FROM leads
           WHERE (digest_date >= ? AND digest_date <= ?) OR (digest_date IS NULL AND score IS NOT NULL)""",
        (start, today.isoformat()),
    ).fetchall()
    surfaced = [r for r in rows if r["verdict"] in ("qualified", "held")]
    reviewed = [r for r in surfaced if r["review"]]
    good = [r for r in reviewed if r["review"] in ("relevant", "sent", "replied", "meeting", "won")]
    bad = [r for r in reviewed if r["review"] == "not-relevant"]
    return {
        "days": days,
        "surfaced": len(surfaced),
        "reviewed": len(reviewed),
        "relevant": len(good),
        "not_relevant": len(bad),
        "precision": (len(good) / (len(good) + len(bad))) if (good or bad) else None,
        "sent": sum(1 for r in reviewed if r["review"] in ("sent", "replied", "meeting", "won")),
    }


def build_digest(store: Store, cfg: Config, today: dt.date) -> tuple[str, int]:
    """Return (markdown, number of leads shown)."""
    rows = store.for_digest()
    by_verdict: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_verdict.setdefault(r["verdict"], []).append(r)
    qualified = by_verdict.get("qualified", [])
    held = by_verdict.get("held", [])
    near = by_verdict.get("near_miss", [])
    rejected = by_verdict.get("llm_rejected", [])
    runs = store.runs_on(today)

    owner = cfg.get("owner", {})
    states = ", ".join(cfg.states)
    out = [
        f"# BD digest — {today.strftime('%A, %b %d, %Y').replace(' 0', ' ')}",
        "",
        f"{owner.get('firm', '')} · {states} · **nothing in this digest has been sent**",
        "",
        f"**{len(qualified)} to review** · {len(held)} held (license pending) · "
        f"{len(near)} near misses · {len(rejected)} filtered by reviewer",
    ]
    if runs:
        fetched = sum(r["fetched"] for r in runs)
        new = sum(r["new"] for r in runs)
        merged = sum(r["merged"] for r in runs)
        out.append(f"Ingested today: {fetched} items → {new} new, {merged} merged across sources")
    out += [
        "",
        "> Review each draft, edit it, and send it yourself. Then record the outcome so the",
        "> weekly precision number means something: `python -m bdrep mark <lead#> relevant|not-relevant|sent`",
        "",
    ]

    out += ["## Leads to review", ""]
    if not qualified:
        out += ["_No qualified leads today._", ""]
    for i, r in enumerate(qualified, 1):
        out += _lead_block(i, r, cfg, with_draft=True)

    if held:
        out += ["## Held — license pending (no draft)", ""]
        for i, r in enumerate(held, 1):
            out += _lead_block(i, r, cfg, with_draft=False)

    if near:
        out += [
            "## Near misses (no draft)", "",
            "| Lead | Score | State | Title | Missed because |",
            "|---|---|---|---|---|",
        ]
        for r in near:
            reasons = _json(r, "score_json").get("reasons", [])
            title = f"[{_cell(r['title'])}]({r['url']})" if r["url"] else _cell(r["title"])
            out.append(f"| #{r['id']} | {r['score']} | {r['state'] or '?'} | {title} | {_cell('; '.join(reasons))} |")
        out.append("")

    if rejected:
        out += ["## Filtered by the draft reviewer", "",
                "Passed the keyword score but Claude judged them not a real opportunity. Skim for mistakes.", ""]
        for r in rejected:
            title = f"[{r['title']}]({r['url']})" if r["url"] else r["title"]
            out.append(f"- #{r['id']} {title} (score {r['score']}) — {r['why']}")
        out.append("")

    out += ["## Source health", "", "| Source | Status | Fetched | New | Merged | Requests | Notes |",
            "|---|---|---|---|---|---|---|"]
    ran = {r["source"] for r in runs}
    for r in runs:
        status = r["status"].upper() if r["status"] != "ok" else "ok"
        out.append(f"| {SOURCE_LABELS.get(r['source'], r['source'])} | {status} | {r['fetched']} | {r['new']} | "
                   f"{r['merged']} | {r['requests']} | {_cell(r['note'])} |")
    for name, opts in cfg.get("sources", {}).items():
        if name not in ran and opts.get("enabled", True):
            out.append(f"| {SOURCE_LABELS.get(name, name)} | NOT RUN TODAY | | | | | |")
    out.append("")

    stats = weekly_stats(store, today)
    target = int(cfg.get("digest", {}).get("weekly_target", 3))
    precision = f"{stats['precision']:.0%}" if stats["precision"] is not None else "n/a (nothing marked yet)"
    out += [
        "## Last 7 days", "",
        f"- Surfaced (qualified + held): **{stats['surfaced']}** (target: {target}+ genuinely relevant per week)",
        f"- Reviewed: {stats['reviewed']} · relevant: {stats['relevant']} · not relevant: {stats['not_relevant']} "
        f"· precision: {precision} · sent: {stats['sent']}",
        "",
    ]
    return "\n".join(out), len(rows)


def write_digest(store: Store, cfg: Config, today: dt.date, out_dir: str | Path | None = None) -> Path:
    """Write today's digest and close out the leads it covers. Re-running on
    the same day appends any new leads to the same file."""
    markdown, _ = build_digest(store, cfg, today)
    directory = cfg.path(out_dir or cfg.get("digest", {}).get("dir", "digests"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{today.isoformat()}.md"
    if path.exists():
        markdown = path.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n## Later run, same day\n\n" + markdown
    path.write_text(markdown, encoding="utf-8")
    store.mark_digested(today.isoformat())
    store.commit()
    return path
