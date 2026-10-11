"""Command line: python -m bdrep <command>.

  run       ingest + score + draft + digest (the daily job)
  ingest    pull from sources into the store
  score     score unscored leads (--rescore after editing config.yaml)
  draft     write drafts for qualified leads
  digest    write today's digest file
  mark      record what you did with a lead (feeds the weekly precision number)
  show      print one lead with its score breakdown and draft
  stats     weekly scorecard against the MVP success metric
  search    live-test the Google News queries (stores nothing)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from bdrep.config import load_config
from bdrep.digest import weekly_stats, write_digest
from bdrep.drafting import run_drafts
from bdrep.pipeline import ingest, open_store, score_all
from bdrep.search_check import format_report, run_search
from bdrep.store import REVIEW_STATUSES, row_to_dict


def _print_ingest(reports) -> None:
    for r in reports:
        c = r.counts
        line = f"{r.name}: {r.status}"
        if c:
            line += f" (fetched {c['fetched']}, new {c['new']}, merged {c['merged']}, seen {c['seen']}, requests {c['requests']})"
        print(line)
        for m in r.messages:
            print(f"  - {m}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bdrep", description="Mata Engineering lead pipeline. Never sends anything.")
    parser.add_argument("--config", help="path to config.yaml (default: repo config.yaml)")
    parser.add_argument("--db", help="override store.path")
    parser.add_argument("--date", help="treat this ISO date as today (backfills, testing)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="ingest, score, draft and write the digest")
    p_run.add_argument("--source", action="append", help="only this source (repeatable)")
    p_run.add_argument("--no-llm", action="store_true", help="template drafts only")
    p_run.add_argument("--digest-dir", help="override digest.dir")

    p_ing = sub.add_parser("ingest", help="pull new items from sources")
    p_ing.add_argument("--source", action="append", help="only this source (repeatable)")

    p_score = sub.add_parser("score", help="score unscored leads")
    p_score.add_argument("--rescore", action="store_true", help="re-score every lead not yet in a digest")

    p_draft = sub.add_parser("draft", help="draft outreach for qualified leads")
    p_draft.add_argument("--no-llm", action="store_true", help="template drafts only")

    p_dig = sub.add_parser("digest", help="write the digest for today")
    p_dig.add_argument("--digest-dir", help="override digest.dir")

    p_mark = sub.add_parser("mark", help="record a review outcome for a lead")
    p_mark.add_argument("lead_id", type=int)
    p_mark.add_argument("status", choices=REVIEW_STATUSES)
    p_mark.add_argument("--note", default="")

    p_show = sub.add_parser("show", help="print one lead")
    p_show.add_argument("lead_id", type=int)

    p_stats = sub.add_parser("stats", help="weekly scorecard")
    p_stats.add_argument("--days", type=int, default=7)

    p_search = sub.add_parser("search", help="live-test the Google News queries; stores nothing")
    p_search.add_argument("--query", action="append", help="test this query instead of the configured ones (repeatable)")
    p_search.add_argument("--days", type=int, default=7, help="how far back Google News should look (default 7)")
    p_search.add_argument("--limit", type=int, default=15, help="results shown per query (default 15)")
    p_search.add_argument("--kept-only", action="store_true", help="hide low-scoring and excluded results")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    if args.command == "search":
        reports = run_search(cfg, queries=args.query, days=args.days)
        print(format_report(reports, limit=args.limit, kept_only=args.kept_only))
        return 1 if all(r.error for r in reports) else 0

    today = dt.date.fromisoformat(args.date) if args.date else cfg.today()
    store = open_store(cfg, args.db)
    try:
        if args.command in ("run", "ingest"):
            _print_ingest(ingest(store, cfg, today, only=args.source))
        if args.command == "score" and args.rescore:
            print(f"reset {store.reset_scores()} undigested leads")
            store.commit()
        if args.command in ("run", "score"):
            print(f"scored: {score_all(store, cfg) or 'nothing new'}")
        if args.command in ("run", "draft"):
            print(f"drafts: {run_drafts(store, cfg, use_llm=not args.no_llm)}")
        if args.command in ("run", "digest"):
            path = write_digest(store, cfg, today, args.digest_dir)
            print(f"digest: {path}")
        if args.command == "mark":
            store.mark_review(args.lead_id, args.status, args.note)
            print(f"lead #{args.lead_id}: {args.status}")
        if args.command == "show":
            row = store.get(args.lead_id)
            if row is None:
                print(f"no lead #{args.lead_id}", file=sys.stderr)
                return 1
            d = row_to_dict(row)
            draft = d.pop("draft", None)
            print(json.dumps(d, indent=2, default=str))
            if draft:
                print("\n" + draft)
        if args.command == "stats":
            print(json.dumps(weekly_stats(store, today, args.days), indent=2))
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0
