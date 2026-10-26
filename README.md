# AI-Powered Job Search Automation


An automated pipeline for a targeted job search: fetch open roles directly
from companies' ATS APIs and job aggregators, filter out anything
irrelevant, deduplicate against everything already seen, optionally score
each candidate's fit against your own experience with Claude, and review
the results in a local web board or a Markdown digest.

Nothing here talks to any service other than the job sources you configure
and (for the optional AI step) the Anthropic API. All state — scraped
jobs, scores, your own notes — lives in a local SQLite database; nothing
is sent to a third party beyond fetching the postings themselves.

## How it works

1. **Fetch** (`app/main.py`) — pulls open roles from:
   - `data/ats_discovery.sqlite3` table `ats_slug` — one board per row with
     `available = 1` (Greenhouse, Ashby, Workable, Lever, SmartRecruiters)
     — precise, low noise.
   - `aggregators.yaml` — broad keyword+location search across many
     employers at once (Adzuna, Remotive) — wider reach, more noise.
2. **Filter** (`app/filters.py`) — drops anything that isn't an
   engineering-shaped title, isn't in an allowed location, or whose JD
   requires a stack you've excluded.
3. **Dedup** (`app/dedup.py`) — everything fetched is stored in a local
   SQLite database (`data/seen_jobs.sqlite3`), keyed by URL and by
   normalized company+title, so re-runs only ever surface genuinely new
   postings.
4. **AI evaluation** (`app/ai_evaluate.py`, optional, costs money) — sends
   each filtered candidate plus your `profile.yaml` to Claude, which scores
   fit (0-100) and returns concrete gaps, transferable strengths, risk
   factors, and an apply/consider/skip recommendation.
5. **Review** (`web/`) — a local Next.js app reading/writing the same
   SQLite database directly, for browsing results and tracking your own
   `applied / interview / rejected / skipped / silence` status and notes.
6. **Digest** (`app/digest.py`) — writes `data/digest.md`, one section per
   job scored 45 or above that you have not marked applied or skipped,
   highest match score first, with the same information as the detail panel.

## Setup

```bash
python setup.py
```

That installs `requirements.txt`, copies these templates when the real
file is missing (it never overwrites a file you already have), and creates
`data/ats_discovery.sqlite3` with the `ats_slug` table:

- `.env.example` → `.env`
- `profile.example.yaml` → `profile.yaml`
- `filters.yaml.example` → `filters.yaml`
- `aggregators.yaml.example` → `aggregators.yaml`

Fill in `.env` and `profile.yaml`. `profile.yaml` is gitignored — it holds
your personal experience. `profile.example.yaml` is a blank template.
For a fully worked (fictional) example, see
[`profile.sample.yaml`](profile.sample.yaml). It shows the level of
specificity each evidence bullet should have. `profile.example.yaml` is
only the shape.

`ANTHROPIC_API_KEY` is only needed for the AI evaluation step. Adzuna
(`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`) is only needed if you keep an Adzuna
entry in `aggregators.yaml` — register a free key at
[developer.adzuna.com](https://developer.adzuna.com). Remotive needs no
auth but only covers remote roles.

### Customize for your own search

This repo ships pre-configured for the original author. **Before your first run, new file and customize to your profile** or you'll get zero candidates, or candidates that don't match your
actual stack:

1. **`filters.yaml` → `location_allow_patterns`** — regex patterns for
   locations to keep. Ships as Canada/Alberta-only; replace with your own
   country/region/cities, or delete entries to broaden it. This is the
   #1 reason a first run returns nothing — if nothing you fetch ever
   matches these patterns, `location_is_allowed()` rejects every job.
2. **`filters.yaml` → `stack_dealbreakers` / `stack_core`** — a JD is
   rejected if it mentions a `stack_dealbreakers` language and none of
   `stack_core`. Ships assuming you want Python/JS and don't want
   Java/C#/.NET/etc. If your own stack includes one of the "dealbreaker"
   languages, move it into `stack_core` (or the filter will reject roles
   in your own stack).
3. **`ats_slug`** — the company registry, in `data/ats_discovery.sqlite3`.
   Each row is one board (`slug`, `ats_board`, `name`). `available = 1`
   means the next run will check it. `companies.yaml.example` shows the
   old entry shape; the pipeline does not read that file.
   `aggregators.yaml`'s `where` params are also location-specific —
   update those too if you're not targeting Alberta/Canada.

## Usage

### 1. Fetch + filter (free)

```bash
python -m app.main                    # all companies + aggregators
python -m app.main --company affirm   # just one company, for debugging a fetcher
python -m app.main --skip-aggregators # ats_slug boards only
python -m app.main --skip-companies   # aggregators.yaml only

make run                              # same thing, interactive prompts instead of flags
```

Output: `data/candidates.csv`, appended to on every run.

### 2. AI evaluation (optional, costs money)

```bash
python -m app.ai_evaluate --dry-run   # see what's queued, no API calls, no cost
python -m app.ai_evaluate --limit 20  # score just 20, to sanity-check quality/cost first
python -m app.ai_evaluate             # score everything unscored

make evaluate                         # interactive prompts instead of flags
```

Results are stored in SQLite (so re-running never re-pays for a job
already scored) and written to `data/scored_candidates.csv`, sorted
best-match-first.

### Check the scores

The check below compares those scores with your own five-point apply
labels. While you label, it shows the title, location, and job description.
It does not show the company name or the model score. Run it from the repo root:

```bash
python -m app.fit_validate          # draw a sample, then label each JD
make validate                       # same thing

python -m app.fit_validate report   # tuning-set agreement, noise, and sanity checks
python -m app.fit_validate report --held-out
python -m app.fit_validate tag      # one cause per disagreement
python -m app.fit_validate noise    # rescore 12 JDs, 5 times each
python -m app.fit_validate gates    # record hard-constraint pass/fail checks
```

Labels are `1` strong no, `2` or `n` no, `3` or `m` maybe, `4` or `y` yes,
and `5` or `sy` strong yes. A note can follow the label. Yes and strong yes
count as would apply. No and strong no count as would not. Maybe is the swing label.
The default sample is 80 scored JDs plus 20 random controls,
split in half. The report uses the tuning half unless you pass
`--held-out`. Copy `gates.yaml.example` to `gates.yaml` to add location,
seniority, or other pass/fail gates. `python -m app.ai_evaluate --gates`
applies that file before spending a model call.

### 3. Review results

```bash
cd web && npm install    # first time only
cd ..
make web                              # starts the board at http://localhost:3000
```

Reads/writes `data/seen_jobs.sqlite3` directly — no export/import step.
See [`web/README.md`](web/README.md) for details (custom `DB_PATH`,
production build, etc).

### 4. Markdown digest

```bash
python -m app.digest          # writes data/digest.md
make digest                   # same thing
```

One section per job that passed filters and scored 45 or above, highest
match score first. Jobs with no match score, or a score below 45, are
left out. So are jobs you have marked **Applied** (you applied) or
**Skipped** (not a fit). Each remaining
section has the same information as the review board's detail panel:
title, company, location, AI status, score, your own status, notes, the
job link, transferable strengths, genuine gaps, risk factors, and the
job description.

To drop a job, set its **My status:** line in `data/digest.md` to
`Applied` or `Skipped` and run `make digest` again. The decision is saved
on that job and it is left out of the new file. `Not a good fit` is
accepted as Skipped. Interview, rejected, and silence stay in the digest.
The review board still lists every job, including ones the digest omits.

```bash
python -m app.digest --applied URL [URL ...]
python -m app.digest --skipped URL [URL ...]   # not a fit
python -m app.digest --restore URL [URL ...]   # put those jobs back
```

Setting Applied or Skipped on the review board has the same effect the
next time the digest is generated. `--restore` clears that status and
keeps any notes.

## Makefile commands

Thin wrappers over the commands above — run from the repo root:

| Command        | Equivalent to                    | What it does |
|-----------------|----------------------------------|--------------|
| `make setup`    | `python setup.py`                | Install Python dependencies, copy missing example configs, and create `data/ats_discovery.sqlite3` if needed. |
| `make run`      | `python -m app.main`             | Fetch + filter (step 1). Interactively asks whether to skip companies/aggregators/discovery and whether to limit to one company slug, instead of you remembering the flags. |
| `make evaluate` | `python -m app.ai_evaluate`       | AI evaluation (step 2). Interactively asks for `--dry-run` and an optional `--limit`. |
| `make web`      | `npm --prefix web run dev`       | Starts the Next.js review board, with `DB_PATH` already pointed at `data/seen_jobs.sqlite3`. |
| `make digest`   | `python -m app.digest`           | Writes `data/digest.md` for jobs scored 45 or above that you have not marked applied or skipped, highest score first. Reads Applied/Skipped edits from an existing digest first. |
| `make validate` | `python -m app.fit_validate`     | Labels sampled job descriptions (title and JD only) and can report how well the fit scores match those labels. |
| `make test`     | `pytest app/` + the standalone sanity-check scripts | Runs the full test suite (see the Tests section below). |

`make` with no target runs `make run` (the default goal).

## Configuration

- **`data/ats_discovery.sqlite3`** — the company registry (`ats_slug`:
  name, ATS board, slug, `available`, `last_seen`, `discovery_time`).
  Rows with `available = 1` are the boards `main.py` checks.
- **`aggregators.yaml`** — aggregator search config (keywords, location,
  pagination limits).
- **`filters.yaml`** — title allowlist/exclusion keywords, location
  allowlist patterns, and JD stack-dealbreaker/core-stack keywords. Edit
  this directly as you refine what counts as in-scope for you — no code
  changes needed (matching logic lives in `app/filters.py`).
- **`profile.yaml`** — your experience profile fed to the AI evaluation
  step (see `profile.example.yaml` for the blank template and
  `profile.sample.yaml` for a fully worked example).
- **`gates.yaml`** — optional pass/fail constraints (location, seniority,
  work authorization) checked before the fit score. See
  `gates.yaml.example`. Absent means gating is off.

## Project layout

```
app/
  main.py                 orchestrates fetch -> filter -> dedup -> candidates.csv
  ats_clients.py           one fetch function per ATS
  aggregator_clients.py    one fetch function per aggregator
  filters.py               loads and applies filters.yaml's rules
  dedup.py                 SQLite store (seen_jobs, job_details)
  discover_companies.py    records newly-resolved companies in ats_slug
  ai_evaluate.py           stage 2: Claude-based fit scoring
  fit_validate.py          label JDs and measure score noise, agreement, and gates
  digest.py                Markdown digest of open jobs scored 45 or above, highest score first
  inspect_job.py           CLI to look up a stored job or list recent rejections
  refilter.py              re-runs current filters.py against already-fetched jobs
  scripts/                 one-off diagnostic/maintenance scripts, not part of the pipeline
  tests/                   pytest + standalone sanity-check scripts
web/                       Next.js review board (see web/README.md)
companies.yaml.example     example company entries (not read by the pipeline)
data/ats_discovery.sqlite3 company -> ATS registry (table ats_slug)
aggregators.yaml           aggregator search config
filters.yaml               title/location/stack filter rules
profile.example.yaml       blank template for profile.yaml (your real profile, gitignored)
profile.sample.yaml        fully worked (fictional) example of a filled-in profile
```

## Tests

```bash
make test
```

Runs the pytest suite plus the standalone sanity-check scripts
(`test_pipeline.py`, `test_discover_companies.py`, `test_ai_evaluate.py`).
None of them call live external APIs.

## Notes on scope

- ATS fetchers for Greenhouse, Ashby, Workable, and Lever were each
  validated against real live responses. SmartRecruiters support is built
  from documentation and third-party corroboration only — verify a new
  SmartRecruiters company with `python -m app.main --company <slug>`
  before trusting it in a real run.
- If a particular company's fetch fails, `main.py` logs a warning and
  continues with the rest rather than crashing the whole run.

## Possible next steps

- Workday support (`clio.wd3.myworkdayjobs.com`-style boards) — its
  job-search API is POST-based with a different pagination shape than the
  ATSes currently supported.
- Company-name normalization for aggregator-sourced dedup (the same
  posting sometimes comes back under slightly different company name
  strings, e.g. "Acme Corp" vs. "Acme").
- Scheduling: once you're happy with a full local run, a daily cron entry
  like `0 7 * * * cd /path/to/job_search_pipeline && python -m app.main && python -m app.ai_evaluate`.

## License

[MIT](LICENSE)
