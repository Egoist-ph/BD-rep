"""SQLite lead store with cross-source deduplication by project name + address.

Dedupe order for an incoming lead:
  1. Same (source, source_id) already stored  -> refresh last_seen.
  2. Same normalized title + address key       -> merge into that lead.
  3. Similar title, compatible address, same or unknown state, no conflicting
     numbers ("Building 100" vs "Building 200"), seen within the window
                                                -> merge.
  4. Otherwise insert.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from bdrep.models import Lead
from bdrep.normalize import norm_address, norm_title, numbers_in

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    norm_title    TEXT NOT NULL,
    norm_address  TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',
    city          TEXT NOT NULL DEFAULT '',
    state         TEXT,
    zip           TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    organization  TEXT NOT NULL DEFAULT '',
    notice_type   TEXT NOT NULL DEFAULT '',
    set_aside     TEXT NOT NULL DEFAULT '',
    naics         TEXT NOT NULL DEFAULT '',
    posted_date   TEXT NOT NULL DEFAULT '',
    deadline      TEXT NOT NULL DEFAULT '',
    contact_json  TEXT NOT NULL DEFAULT '{}',
    extra_json    TEXT NOT NULL DEFAULT '{}',
    sources_json  TEXT NOT NULL DEFAULT '[]',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    score         INTEGER,
    score_json    TEXT,
    verdict       TEXT,
    why           TEXT,
    draft         TEXT,
    draft_meta    TEXT,
    digest_date   TEXT,
    review        TEXT,
    review_note   TEXT,
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS leads_state ON leads(state);
CREATE INDEX IF NOT EXISTS leads_first_seen ON leads(first_seen);
CREATE INDEX IF NOT EXISTS leads_verdict ON leads(verdict);

CREATE TABLE IF NOT EXISTS source_ids (
    source    TEXT NOT NULL,
    source_id TEXT NOT NULL,
    lead_id   INTEGER NOT NULL REFERENCES leads(id),
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date   TEXT NOT NULL,
    started_at TEXT NOT NULL,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL,
    fetched    INTEGER NOT NULL DEFAULT 0,
    new        INTEGER NOT NULL DEFAULT 0,
    merged     INTEGER NOT NULL DEFAULT 0,
    seen       INTEGER NOT NULL DEFAULT 0,
    requests   INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

REVIEW_STATUSES = ("relevant", "not-relevant", "sent", "replied", "meeting", "won", "skip")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def dedupe_key(lead: Lead) -> str:
    return f"{norm_title(lead.title)}|{norm_address(lead.address, lead.city)}|{lead.state or ''}"


def _addresses_compatible(a: str, b: str) -> bool:
    return not a or not b or a == b or a in b or b in a


def titles_match(a: str, b: str, threshold: float) -> bool:
    if numbers_in(a) != numbers_in(b):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


class Store:
    def __init__(self, path: str | Path, dedupe_window_days: int = 120, title_similarity: float = 0.88):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.window_days = dedupe_window_days
        self.similarity = title_similarity

    def close(self) -> None:
        self.conn.close()

    # -- ingest ---------------------------------------------------------------

    def upsert(self, lead: Lead, today: dt.date) -> tuple[str, int]:
        """Store a lead. Returns ('new' | 'merged' | 'seen', lead_id)."""
        day = today.isoformat()
        row = self.conn.execute(
            "SELECT lead_id FROM source_ids WHERE source = ? AND source_id = ?",
            (lead.source, lead.source_id),
        ).fetchone()
        if row:
            self._refresh(row["lead_id"], lead, day)
            return "seen", row["lead_id"]

        key = dedupe_key(lead)
        existing = self.conn.execute("SELECT id FROM leads WHERE dedupe_key = ?", (key,)).fetchone()
        lead_id = existing["id"] if existing else self._find_similar(lead, today)
        if lead_id is not None:
            self._merge(lead_id, lead, day)
            return "merged", lead_id

        cur = self.conn.execute(
            """INSERT INTO leads (dedupe_key, title, norm_title, norm_address, address, city,
                   state, zip, description, url, organization, notice_type, set_aside, naics,
                   posted_date, deadline, contact_json, extra_json, sources_json,
                   first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, lead.title, norm_title(lead.title), norm_address(lead.address, lead.city),
                lead.address, lead.city, lead.state, lead.zip, lead.description, lead.url,
                lead.organization, lead.notice_type, lead.set_aside, lead.naics,
                lead.posted_date, lead.deadline, json.dumps(lead.contact),
                json.dumps(lead.extra), json.dumps([self._source_ref(lead)]), day, day,
            ),
        )
        lead_id = cur.lastrowid
        self._link(lead, lead_id)
        return "new", lead_id

    def has_description(self, source: str, source_id: str) -> bool:
        row = self.conn.execute(
            """SELECT l.description FROM source_ids s JOIN leads l ON l.id = s.lead_id
               WHERE s.source = ? AND s.source_id = ?""",
            (source, source_id),
        ).fetchone()
        return bool(row and row["description"])

    def _find_similar(self, lead: Lead, today: dt.date) -> int | None:
        cutoff = (today - dt.timedelta(days=self.window_days)).isoformat()
        title = norm_title(lead.title)
        address = norm_address(lead.address, lead.city)
        rows = self.conn.execute(
            """SELECT id, norm_title, norm_address FROM leads
               WHERE first_seen >= ? AND (state IS NULL OR ? IS NULL OR state = ?)""",
            (cutoff, lead.state, lead.state),
        ).fetchall()
        for r in rows:
            if _addresses_compatible(address, r["norm_address"]) and titles_match(
                title, r["norm_title"], self.similarity
            ):
                return r["id"]
        return None

    def _merge(self, lead_id: int, lead: Lead, day: str) -> None:
        row = self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        sources = json.loads(row["sources_json"])
        ref = self._source_ref(lead)
        if ref not in sources:
            sources.append(ref)
        updates: dict[str, Any] = {"sources_json": json.dumps(sources), "last_seen": day}
        # Fill gaps from the new source; never overwrite what is already known.
        for col in ("address", "city", "state", "zip", "url", "organization", "deadline", "naics", "set_aside"):
            if not row[col] and getattr(lead, col):
                updates[col] = getattr(lead, col)
        if len(lead.description) > len(row["description"]):
            updates["description"] = lead.description
        if row["digest_date"] is None and ({"state", "description"} & updates.keys()):
            updates["score"] = None  # new information: score again
        self._update(lead_id, updates)
        self._link(lead, lead_id)

    def _refresh(self, lead_id: int, lead: Lead, day: str) -> None:
        row = self.conn.execute("SELECT description, deadline, digest_date FROM leads WHERE id = ?", (lead_id,)).fetchone()
        updates: dict[str, Any] = {"last_seen": day}
        if lead.deadline and lead.deadline != row["deadline"]:
            updates["deadline"] = lead.deadline
        if len(lead.description) > len(row["description"]):
            updates["description"] = lead.description
            if row["digest_date"] is None:
                updates["score"] = None
        self._update(lead_id, updates)

    def _link(self, lead: Lead, lead_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO source_ids (source, source_id, lead_id) VALUES (?, ?, ?)",
            (lead.source, lead.source_id, lead_id),
        )

    @staticmethod
    def _source_ref(lead: Lead) -> dict[str, str]:
        return {"source": lead.source, "source_id": lead.source_id, "url": lead.url}

    def _update(self, lead_id: int, values: dict[str, Any]) -> None:
        if not values:
            return
        cols = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*values.values(), lead_id))

    def commit(self) -> None:
        self.conn.commit()

    # -- pipeline state -------------------------------------------------------

    def unscored(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM leads WHERE score IS NULL ORDER BY id").fetchall()

    def reset_scores(self) -> int:
        """Forget scores and drafts of leads not yet in a digest (after tuning config)."""
        cur = self.conn.execute(
            """UPDATE leads SET score = NULL, score_json = NULL, verdict = NULL, why = NULL,
                   draft = NULL, draft_meta = NULL WHERE digest_date IS NULL"""
        )
        return cur.rowcount

    def set_score(self, lead_id: int, score: int, verdict: str, breakdown: dict[str, Any]) -> None:
        self._update(lead_id, {"score": score, "verdict": verdict, "score_json": json.dumps(breakdown)})

    def needing_draft(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM leads WHERE verdict = 'qualified' AND draft IS NULL
               AND digest_date IS NULL ORDER BY score DESC, id"""
        ).fetchall()

    def set_draft(self, lead_id: int, *, why: str, draft: str | None, meta: dict[str, Any], verdict: str | None = None) -> None:
        values: dict[str, Any] = {"why": why, "draft": draft, "draft_meta": json.dumps(meta)}
        if verdict:
            values["verdict"] = verdict
        self._update(lead_id, values)

    def for_digest(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM leads WHERE digest_date IS NULL
               AND verdict IN ('qualified', 'held', 'near_miss', 'llm_rejected')
               ORDER BY score DESC, id"""
        ).fetchall()

    def mark_digested(self, day: str) -> int:
        """Close out every scored lead not yet covered by a digest."""
        cur = self.conn.execute(
            "UPDATE leads SET digest_date = ? WHERE digest_date IS NULL AND score IS NOT NULL", (day,)
        )
        return cur.rowcount

    def get(self, lead_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()

    def iter_leads(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM leads ORDER BY id")

    # -- review feedback ------------------------------------------------------

    def mark_review(self, lead_id: int, status: str, note: str = "") -> None:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"status must be one of {', '.join(REVIEW_STATUSES)}")
        if self.get(lead_id) is None:
            raise KeyError(f"no lead with id {lead_id}")
        self._update(lead_id, {"review": status, "review_note": note, "reviewed_at": _now()})
        self.commit()

    # -- runs and watermarks --------------------------------------------------

    def record_run(self, run_date: dt.date, source: str, status: str, counts: dict[str, int], note: str = "") -> None:
        self.conn.execute(
            """INSERT INTO runs (run_date, started_at, source, status, fetched, new, merged, seen, requests, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_date.isoformat(), _now(), source, status, counts.get("fetched", 0),
             counts.get("new", 0), counts.get("merged", 0), counts.get("seen", 0),
             counts.get("requests", 0), note),
        )

    def requests_on(self, run_date: dt.date, source: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) AS n FROM runs WHERE run_date = ? AND source = ?",
            (run_date.isoformat(), source),
        ).fetchone()
        return int(row["n"])

    def runs_on(self, run_date: dt.date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs WHERE run_date = ? ORDER BY id", (run_date.isoformat(),)
        ).fetchall()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Row as a dict with the JSON columns decoded under readable names."""
    d = dict(row)
    names = {"contact_json": "contact", "extra_json": "extra", "sources_json": "sources",
             "score_json": "score_breakdown", "draft_meta": "draft_meta"}
    for col, name in names.items():
        raw = d.pop(col, None)
        d[name] = json.loads(raw) if raw else None
    return d


def lead_from_row(row: sqlite3.Row) -> Lead:
    """Rebuild a Lead (first source) from a stored row, for scoring and drafting."""
    sources = json.loads(row["sources_json"])
    first = sources[0] if sources else {"source": "", "source_id": ""}
    return Lead(
        source=first["source"], source_id=first["source_id"], title=row["title"],
        url=row["url"], description=row["description"], address=row["address"],
        city=row["city"], state=row["state"], zip=row["zip"],
        organization=row["organization"], notice_type=row["notice_type"],
        set_aside=row["set_aside"], naics=row["naics"], posted_date=row["posted_date"],
        deadline=row["deadline"], contact=json.loads(row["contact_json"]),
        extra={**json.loads(row["extra_json"]), "sources": [s["source"] for s in sources]},
    )

