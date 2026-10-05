# Job Search Board (Next.js)

A local web UI over `job_search_pipeline`'s SQLite store (`../data/seen_jobs.sqlite3`).
Reads and writes the database directly — no export/import step, no separate JSON file
to keep in sync. This is now the recommended way to browse jobs and track your own
`applied / interview / rejected / skipped / silence` status with notes.

## Setup

```bash
cd web
npm install
npm run dev
```

Then open http://localhost:3000. It reads `../data/seen_jobs.sqlite3` relative to the
`web/` folder — i.e. it expects `job_search_pipeline/data/seen_jobs.sqlite3` to exist,
which means you should run the Python pipeline (`main.py`, then `ai_evaluate.py`) at
least once first. If the file doesn't exist yet, the app still starts and creates the
tables it needs, you'll just see an empty board.

If your `job_search_pipeline` checkout lives somewhere other than the parent of `web/`,
point at the database explicitly:

```bash
DB_PATH=/path/to/seen_jobs.sqlite3 npm run dev
```

## What it shows

Only jobs that passed the deterministic filter (`passed_filters = 1`). Each row has:

- AI status (`apply` / `consider` / `skip` / `not evaluated`) — from `ai_evaluate.py`
- Your own status (`applied` / `interview` / `rejected` / `skipped` / `silence`) —
  set inline in the table or from the detail panel. Deliberately separate from the AI's
  suggestion, since one is "should I apply" and the other is "what actually happened".
  Applied and Skipped jobs, and jobs scored below 55, are left out of
  `data/digest.md` the next time you run `make digest`.
- Notes — free text, editable from the detail panel.

Filter by either status, search by company/title, sort any column, click a row to open
the full JD plus the AI's reasoning (gaps / strengths / risk factors) and edit
status+notes.

## Production build

```bash
npm run build
npm run start
```
