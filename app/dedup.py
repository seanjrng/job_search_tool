"""SQLite-backed local datastore: dedup tracking + full job details.

Two dedup keys, matching the approach from the original post:
  1. exact job URL
  2. normalized (company, title) pair — catches reposts under a new URL

A job is only "new" if neither key has been seen before. Seen jobs are
recorded regardless of whether they passed the content filters, so we
never re-fetch/re-consider the same posting on a future run.

`job_details` holds the full record (including the JD description text)
for every job we've ever seen, keyed by URL — this is where descriptions
live instead of in candidates.csv. Rationale (2026-08-11): a full JD is
one to several KB of HTML/text; dumping that into a CSV column makes the
file unreadable in Excel/Sheets and defeats the point of the CSV being a
quick human-scannable list. Query this table directly (see
`get_details_by_url` / `iter_all_details`) when you want to see why a job
was included or excluded, or once ai_evaluate.py exists, to feed the JD
into the Haiku prompt without re-fetching it.
"""
import os
import re
import sqlite3
from contextlib import contextmanager

DB_PATH = "data/seen_jobs.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    company_title_key TEXT,
    company TEXT,
    title TEXT,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_company_title_key ON seen_jobs(company_title_key);

CREATE TABLE IF NOT EXISTS job_details (
    url TEXT PRIMARY KEY,
    company TEXT,
    title TEXT,
    location TEXT,
    posted_at TEXT,
    description TEXT,
    passed_filters INTEGER,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    source TEXT
);

CREATE TABLE IF NOT EXISTS ai_evaluations (
    url TEXT PRIMARY KEY,
    match_score INTEGER,
    recommendation TEXT,
    genuine_gaps TEXT,
    transferable_strengths TEXT,
    risk_factors TEXT,
    model TEXT,
    evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);

-- Your own tracking, separate from the AI's recommendation. The AI's
-- apply/consider/skip is a suggestion made before applying; my_status is
-- what actually happened (applied/interview/rejected/skipped/silence) —
-- deliberately a different vocabulary so the two never collide in the UI.
CREATE TABLE IF NOT EXISTS user_status (
    url TEXT PRIMARY KEY,
    my_status TEXT,
    notes TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);
"""

MY_STATUS_VALUES = ["applied", "interview", "rejected", "skipped", "silence"]


def _normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_company_title_key(company: str, title: str, location: str = "") -> str:
    # location is part of the key so two genuinely distinct postings with
    # the same title at the same company (e.g. "Backend Engineer" open in
    # both Toronto and Vancouver) aren't treated as a repost of each other.
    return f"{_normalize(company)}::{_normalize(title)}::{_normalize(location)}"


def _migrate(conn) -> None:
    """SCHEMA's CREATE TABLE IF NOT EXISTS only handles brand-new DBs —
    columns added later need an explicit ALTER TABLE for a DB file that
    already exists (e.g. `source`, added 2026-08-12)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(job_details)")}
    if "source" not in existing:
        conn.execute("ALTER TABLE job_details ADD COLUMN source TEXT")


@contextmanager
def connect(db_path: str = DB_PATH):
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def is_new(conn, job: dict) -> bool:
    key = make_company_title_key(job["company"], job["title"], job.get("location", ""))
    cur = conn.execute(
        "SELECT 1 FROM seen_jobs WHERE url = ? OR company_title_key = ? LIMIT 1",
        (job["url"], key),
    )
    return cur.fetchone() is None


def mark_seen(conn, job: dict) -> None:
    key = make_company_title_key(job["company"], job["title"], job.get("location", ""))
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs (url, company_title_key, company, title) "
        "VALUES (?, ?, ?, ?)",
        (job["url"], key, job["company"], job["title"]),
    )


def save_details(conn, job: dict, passed_filters: bool) -> None:
    """Store the full job record (incl. description) keyed by URL. Called
    for every job we ever see — pass or fail — so you can audit filter
    decisions later without re-fetching anything."""
    conn.execute(
        "INSERT OR REPLACE INTO job_details "
        "(url, company, title, location, posted_at, description, passed_filters, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job.get("url", ""),
            job.get("company", ""),
            job.get("title", ""),
            job.get("location", ""),
            job.get("posted_at"),
            job.get("description", ""),
            1 if passed_filters else 0,
            job.get("source", ""),
        ),
    )


def get_details_by_url(conn, url: str) -> dict | None:
    cur = conn.execute(
        "SELECT url, company, title, location, posted_at, description, passed_filters, fetched_at, source "
        "FROM job_details WHERE url = ?",
        (url,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ["url", "company", "title", "location", "posted_at", "description", "passed_filters", "fetched_at", "source"]
    return dict(zip(keys, row))


def distinct_companies(conn) -> list[str]:
    """Every distinct company name we've ever seen in job_details — the
    input for try_companies_across_ats.py, which checks which of these
    aren't in companies.yaml yet and tries to guess their ATS slug
    directly, since the Adzuna redirect_url path (discover_companies.py)
    turned out to almost never leave adzuna.* for real queries (see
    diagnose_adzuna_redirects.py's 2026-08-12 finding: 25/25 samples
    stayed on Adzuna)."""
    cur = conn.execute("SELECT DISTINCT company FROM job_details WHERE company != '' ORDER BY company")
    return [row[0] for row in cur.fetchall()]


def iter_filtered_out(conn):
    """Every job we've ever stored that didn't pass the filters at fetch
    time — the input for refilter.py, which re-runs the CURRENT filters.py
    against them without re-hitting the ATS/aggregator APIs."""
    cur = conn.execute(
        "SELECT url, company, title, location, posted_at, description "
        "FROM job_details WHERE passed_filters = 0"
    )
    keys = ["url", "company", "title", "location", "posted_at", "description"]
    for row in cur.fetchall():
        yield dict(zip(keys, row))


def iter_passed(conn):
    """Every job we've ever stored that DID pass the filters at fetch time
    — the other input for refilter.py, so a TIGHTENED filter can demote
    jobs that no longer pass, not just rescue ones that now do."""
    cur = conn.execute(
        "SELECT url, company, title, location, posted_at, description "
        "FROM job_details WHERE passed_filters = 1"
    )
    keys = ["url", "company", "title", "location", "posted_at", "description"]
    for row in cur.fetchall():
        yield dict(zip(keys, row))


def set_passed_filters(conn, url: str, passed: bool) -> None:
    """Flip a stored job's passed_filters flag in place — used by
    refilter.py when a filter change rescues (or newly drops) a job that
    was already fetched, without touching seen_jobs (still deduped) or
    forcing a re-fetch."""
    conn.execute(
        "UPDATE job_details SET passed_filters = ? WHERE url = ?",
        (1 if passed else 0, url),
    )


def get_unevaluated_candidates(conn) -> list[dict]:
    """Jobs that passed the deterministic filters but haven't been scored
    by the AI evaluation step yet — the input queue for ai_evaluate.py."""
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.location, jd.posted_at, jd.description "
        "FROM job_details jd "
        "LEFT JOIN ai_evaluations ae ON jd.url = ae.url "
        "WHERE jd.passed_filters = 1 AND ae.url IS NULL"
    )
    keys = ["url", "company", "title", "location", "posted_at", "description"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def save_evaluation(conn, url: str, evaluation: dict, model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ai_evaluations "
        "(url, match_score, recommendation, genuine_gaps, transferable_strengths, risk_factors, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            url,
            evaluation.get("match_score"),
            evaluation.get("recommendation"),
            evaluation.get("genuine_gaps"),
            evaluation.get("transferable_strengths"),
            evaluation.get("risk_factors"),
            model,
        ),
    )


def save_user_status(conn, url: str, my_status: str | None, notes: str | None) -> None:
    """my_status should be one of MY_STATUS_VALUES or None/'' to clear it.
    Not validated strictly here (the web UI constrains it via a <select>)
    so a hand-edited import file with a typo doesn't hard-fail the import."""
    conn.execute(
        "INSERT INTO user_status (url, my_status, notes, updated_at) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(url) DO UPDATE SET my_status=excluded.my_status, "
        "notes=excluded.notes, updated_at=CURRENT_TIMESTAMP",
        (url, my_status or None, notes or None),
    )


def get_user_status(conn, url: str) -> dict | None:
    cur = conn.execute("SELECT url, my_status, notes, updated_at FROM user_status WHERE url = ?", (url,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(["url", "my_status", "notes", "updated_at"], row))


def set_my_status(conn, url: str, my_status: str | None) -> None:
    """Set my_status and keep notes already stored for this URL.

    Passing None clears the status. An empty string is stored as NULL,
    same as save_user_status.
    """
    existing = get_user_status(conn, url)
    notes = existing["notes"] if existing else None
    save_user_status(conn, url, my_status, notes)


def iter_board_jobs(conn):
    """Scored jobs the review board lists, highest match score first.

    Same join as web/lib/db.ts getJobs() (passed filters, AI evaluation,
    your own status), limited to rows that have a match score. Company,
    title, and URL break ties so a digest is stable from run to run.
    """
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.location, jd.description, "
        "       ae.match_score, ae.recommendation, ae.genuine_gaps, "
        "       ae.transferable_strengths, ae.risk_factors, "
        "       us.my_status, us.notes "
        "FROM job_details jd "
        "LEFT JOIN ai_evaluations ae ON jd.url = ae.url "
        "LEFT JOIN user_status us ON jd.url = us.url "
        "WHERE jd.passed_filters = 1 AND ae.match_score IS NOT NULL "
        "ORDER BY ae.match_score DESC, jd.company, jd.title, jd.url"
    )
    keys = [
        "url", "company", "title", "location", "description",
        "match_score", "recommendation", "genuine_gaps",
        "transferable_strengths", "risk_factors", "my_status", "notes",
    ]
    for row in cur.fetchall():
        yield dict(zip(keys, row))


def iter_scored_candidates(conn):
    """All evaluated candidates, best match first, joined with job_details
    for display — used to write the scored CSV."""
    cur = conn.execute(
        "SELECT jd.company, jd.title, jd.location, jd.url, jd.posted_at, "
        "       ae.match_score, ae.recommendation, ae.genuine_gaps, "
        "       ae.transferable_strengths, ae.risk_factors "
        "FROM ai_evaluations ae "
        "JOIN job_details jd ON jd.url = ae.url "
        "ORDER BY ae.match_score DESC"
    )
    keys = ["company", "title", "location", "url", "posted_at", "match_score",
            "recommendation", "genuine_gaps", "transferable_strengths", "risk_factors"]
    for row in cur.fetchall():
        yield dict(zip(keys, row))