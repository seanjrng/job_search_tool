"""Record ATS boards found during a run in ats_slug.

When aggregator_clients.fetch_full_description resolves an Adzuna redirect
to a Greenhouse or Lever company that is not already in ats_slug, main.py
queues it here after the run. A row in ats_slug is what later runs fetch
directly.

A discovered (ats, slug) pair only gets here after a real successful
single-job API call. New rows are insert-only: an existing primary key is
left unchanged.
"""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ats_discovery.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ats_slug(
            slug TEXT,
            ats_board TEXT,
            available INTEGER DEFAULT 1,
            last_seen INTEGER,
            discovery_time INTEGER,
            name TEXT,
            PRIMARY KEY(slug, ats_board)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ats_slug)")}
    if "name" not in columns:
        conn.execute("ALTER TABLE ats_slug ADD COLUMN name TEXT")
    return conn


def load_known_keys(path: Path = DB_PATH) -> set[tuple[str, str]]:
    """(ats, slug) pairs already stored, so a board is not recorded twice."""
    conn = _connect(path)
    try:
        return {(ats, slug) for ats, slug in conn.execute("SELECT ats_board, slug FROM ats_slug")}
    finally:
        conn.close()


def append_new_companies(discovered: list[dict], path: Path = DB_PATH) -> list[dict]:
    """discovered: [{"name", "ats", "slug"}, ...].

    Duplicates in this call and pairs already stored are skipped. Each new
    board is inserted with available = 1 and both timestamps set to now.
    Returns the dicts that were inserted.
    """
    conn = _connect(path)
    try:
        known = {(ats, slug) for ats, slug in conn.execute("SELECT ats_board, slug FROM ats_slug")}
        seen_this_call = set()
        added = []
        seen_at = int(time.time())
        for company in discovered:
            key = (company["ats"], company["slug"])
            if key in known or key in seen_this_call:
                continue
            seen_this_call.add(key)
            conn.execute(
                """
                INSERT OR IGNORE INTO ats_slug
                    (slug, ats_board, available, last_seen, discovery_time, name)
                VALUES (?, ?, 1, ?, ?, ?)
                """,
                (company["slug"], company["ats"], seen_at, seen_at, company["name"]),
            )
            added.append(company)
        conn.commit()
    finally:
        conn.close()
    return added


if __name__ == "__main__":
    known = load_known_keys()
    print(f"{len(known)} (ats, slug) pairs currently tracked in {DB_PATH}:")
    for ats, slug in sorted(known):
        print(f"  {ats:12s} {slug}")
