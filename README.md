# BD-rep

Daily lead digest for Mata Engineering. It finds projects in California, Illinois and Florida that match four service lines (arc flash / short circuit studies, permitting, forensics and expert witness, Section 179D certification), scores them, drafts outreach for the good ones, and writes one markdown digest per day.

**Nothing is ever sent.** Drafts sit in the digest until you review, edit and send them yourself.

```
ingest ──► score ──► draft ──► digest
SAM.gov     rules     Claude    digests/YYYY-MM-DD.md
News RSS    0-100     or        (you review, edit, send,
  │                   template   then `bdrep mark`)
  └─ deduped by project name + address into data/leads.db (SQLite)
```

Out of scope for this MVP: LinkedIn scraping, contractor association directories, automated sending.

## Quick start

```bash
pip install -e ".[llm,dev]"          # Python 3.11+
export ANTHROPIC_API_KEY=...          # optional: without it, drafts come from templates
python -m bdrep search                # live-test the Google News queries first (stores nothing)
python -m bdrep run                   # ingest + score + draft + digest
open digests/$(date +%F).md
```

SAM.gov is **off by default**. To turn it on, set `sources.sam_gov.enabled: true` in `config.yaml` and export `SAM_API_KEY` (SAM.gov > Profile > Account Details > Public API Key). Until then, the digest's Source health table lists it as "off".

### Test the Google search

`search` runs the queries live, scores every result, and prints what each query returned and why each result was kept or dropped. It writes nothing to the database and drafts nothing.

```bash
python -m bdrep search                                  # all configured queries, last 7 days
python -m bdrep search --kept-only                      # only qualified / held / near-miss results
python -m bdrep search --query '"arc flash" Chicago' --days 30 --limit 50   # try a query before adding it
```

Each query prints its Google News URL, so you can open it in a browser and compare against what the tool saw. The summary at the end gives, per query, how many results landed in each verdict. A query that returns mostly `excluded` and `low` is costing you noise. Rewrite it or drop it in `config.yaml` under `sources.news_rss.google_news_queries`.

After you act on a lead, record it. The weekly precision number is computed from these marks, and without them it has nothing to measure:

```bash
python -m bdrep mark 12 sent          # relevant | not-relevant | sent | replied | meeting | won | skip
python -m bdrep stats                 # last 7 days against the success metric
python -m bdrep show 12               # one lead with its score breakdown and draft
```

Other commands: `search`, `ingest`, `score [--rescore]`, `draft [--no-llm]`, `digest`. Global flags: `--config`, `--db`, `--date YYYY-MM-DD`.

### Run it daily

On a personal machine (not an employer laptop), for example with cron at 6:10 am:

```
10 6 * * * cd ~/BD-rep && ANTHROPIC_API_KEY=... .venv/bin/python -m bdrep run >> data/run.log 2>&1
```

Running it twice in a day is safe. The second run adds only the new leads to the same digest file and counts SAM.gov requests already spent that day.

**This repository is public.** `data/` and `digests/` are git-ignored because they hold prospect details and unsent drafts. Don't commit them, and don't run the pipeline in GitHub Actions while the repo is public: Actions logs and artifacts are public too.

## Sources

| Source | What it pulls | Cost per run | Watch out for |
|---|---|---|---|
| `sam_gov` (off by default) | Active federal notices with place of performance in CA, IL, FL (SAM.gov Get Opportunities v2) | 1 request per state + pagination, plus up to `description_budget` description fetches | A personal key with no SAM.gov role is limited to **10 requests/day**, enforced by `daily_request_budget`. **Keys expire every 90 days.** An expired key shows up as an error in the digest's Source health table. |
| `news_rss` | Google News RSS search results for the queries in `config.yaml`, plus any RSS/Atom feeds you add | 1 request per query | Noisy by nature. Scoring and the Claude review exist mainly to filter this source. |

Adding a source takes one adapter in `bdrep/sources/` that returns `Lead` objects, plus one registry entry.

A failed source never stops the run. It appears as `ERROR` or `PARTIAL` in the digest's **Source health** table, so a dead key can't hide behind "no leads today".

## Scoring

The scoring is rule-based, so every point traces to a keyword in `config.yaml`. To fix a false positive, edit a list there. No code change is needed.

| Part | Points | Basis |
|---|---|---|
| Service match | 0–50 | Strongest service line. A *strong* term (`arc flash`, `coordination study`, `expert witness`, `179d`, …) earns 35+. *Weak* terms only (`switchgear`, `substation`, …) cap at 25. Engineering NAICS codes count as a weak hit. |
| State | 0–20 | Licensed state 20, license pending 10, state not detected 5. Any other state is **excluded**. |
| Real project | 0–30 | SAM.gov notice type, procurement words (RFP, bid, sources sought), stage, facility, dollar or MW quantities. Minus noise words (stock, earnings, opinion). |
| Penalties | −15 / −25 | Scopes you don't sell (design-build, design services, residential). 179D items that read as not yet under construction. |

Hard excludes score 0: PPE and arc-flash-suit purchases, NFPA 70E training, market-size press releases, job ads, single-family residential.

| Verdict | Rule | In the digest |
|---|---|---|
| `qualified` | Score ≥ threshold (60) **and** at least one strong service term | Full entry with a draft |
| `held` | Would qualify, but the state's license is pending (FL) | Listed with a HOLD note and no draft |
| `near_miss` | Within 15 points of threshold, or weak terms only | One row in a table |
| `low` / `excluded` | Everything else | Not shown |

After editing the config, run `python -m bdrep score --rescore` to re-score leads not yet in a digest. `python examples/make_sample_digest.py` re-runs the synthetic fixtures, so you can see what a change moves. A sample output is at [`examples/sample-digest.md`](examples/sample-digest.md).

## Segments

A segment is a group of prospects with its own pitch, configured under `scoring.segments` in `config.yaml`. It has one entry so far.

**Illinois manufacturers (`il_manufacturing`).** These are plants that may run their arc flash study in-house. A lead joins the segment when it is in Illinois and mentions a manufacturing facility (manufacturer, plant, factory, foundry, machining, …). Inside the segment:

- **Extra evidence:** "electrical hazards" and similar OSHA wording count as direct arc flash evidence. Expansions, new production lines and OSHA mentions count as weak evidence, so they show up as near misses rather than getting drafts.
- **Bonus:** the lead gets +10 points.
- **Pitch:** an independent review of the existing study: system data, IEEE 1584 method, protective device settings and labels, with a written fix list. Drafts never say or imply that the plant's staff are unqualified or that an in-house study is improper.
- **Size:** a headline doesn't tell you whether a company is small, so the digest asks you to check before sending.

Three Google News queries feed it: OSHA electrical citations at Illinois plants, "arc flash" at Illinois plants, and Illinois plant expansions.

**Incident and enforcement wording** (injured, burned, killed, cited, fined, …) gets a "Handle with care" note in the digest. The draft then never quotes the headline or mentions the incident, citation or people involved; it opens with the offer instead.

**Exclusions** are split. PPE, gloves and training are excluded on procurement notices, where someone is buying them, but not in news, where "cited for failing to provide PPE" is a lead.

## Drafting

**Off by default** (`drafting.enabled: false`): the digest is a lead list with who, where, why it matters, and a link. Set it to `true` to get an outreach draft under each qualified lead, as described below.

When `ANTHROPIC_API_KEY` is set, each qualified lead goes to Claude (`claude-opus-5-5`, effort `medium`, structured JSON output). Claude returns:

- a judgment on whether this is a real, current opportunity. Rejections are listed in the digest under "Filtered by the draft reviewer" so you can catch mistakes.
- a one-line reason the lead matters
- who to send it to, taken from the lead data and never invented
- a plain-text email of 120 words or fewer

The prompt rules out offering design, construction or training. It also rules out claiming clients, results or credentials beyond your stated licenses, and inventing project facts. Server-side refusal fallback is on (`fallbacks: "default"`). If the call fails for any reason, the template draft is used and the digest says so. Expected cost is a few cents per qualified lead (estimate, not measured).

Drafts point at the person who actually buys the work:

- On a SAM.gov construction notice or award, the email goes to the electrical contractor bidding or awarded, who usually carries the study scope.
- On a sources-sought notice, it asks how to submit a capability statement.

## Compliance guardrails (built in, but still yours to verify)

Each of these comes from web research done while building this tool. That research worked from search-engine excerpts of the official sources, not a full reading of them. Confirm with the board, IDFPR, or counsel before relying on any of it.

- **Florida (license pending):** F.S. 471.031(1)(a) bars practicing engineering without a license, and the definition in 471.005 counts holding yourself out as an engineer as offering to practice. FL leads are therefore **held, with no draft**. Set `states.FL.license: licensed` the day the license issues. Firms offering engineering in Florida also need to be qualified by a Florida-licensed engineer (471.023).
- **Illinois:** a business offering engineering, including one operating under an assumed name, must register as a Professional Design Firm (225 ILCS 325/23). Every IL lead carries that reminder.
- **California:** BPELSG does not license firms, but B&P 6738 requires an Organization Record filing. If Mata Engineering is an LLC, have counsel check Corp. Code 17701.04(e).
- **179D:** P.L. 119-21 §70507 ends the deduction for property whose construction begins after **June 30, 2026**. For new construction, the certifier must be licensed in the building's state (IRS Notice 2006-52). The scoring penalizes "proposed/planned/groundbreaking" 179D items, and every 179D lead carries the caveat.
- **Employer:** moonlighting, conflict-of-interest and IP-assignment terms in your W-2 agreement apply to this work. Run it on your own hardware and accounts.

## Layout

```
config.yaml              every keyword, weight, threshold, query and state rule
bdrep/sources/           sam_gov.py, news_rss.py, base.py (HTTP, key redaction)
bdrep/store.py           SQLite store + dedupe (exact key, then fuzzy title within state)
bdrep/scoring.py         rule-based score and verdict
bdrep/drafting.py        Claude review + draft, template fallback
bdrep/digest.py          markdown digest, source health, weekly scorecard
bdrep/search_check.py    `search` command: live query test, nothing stored
bdrep/cli.py             python -m bdrep ...
tests/                   pytest, no network; fixtures are synthetic
```

Run the tests with `python -m pytest`.
