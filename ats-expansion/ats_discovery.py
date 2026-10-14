"""Discover Greenhouse and Ashby board slugs from a Common Crawl columnar index.

One crawl id (default CC-MAIN-2026-30) is read from the first argument.
Slugs already stored under the same (slug, ats_board) primary key are left
as they are. Per-file caches, including caches written by the earlier
greenhouse and ashby scripts, are removed after a successful run.
"""

import gzip
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT.parent / "data" / "ats_discovery.sqlite3"
CRAWL = sys.argv[1] if len(sys.argv) > 1 else "CC-MAIN-2026-30"
CACHE = ROOT / f".ats_discovery_cache_{CRAWL}"
# Caches from greenhouse_query.py and ashby_tokens.py. A parquet file is
# skipped when both exist, and they are deleted with the new cache.
LEGACY = {
    "greenhouse": ROOT / f".slug_cache_{CRAWL}",
    "ashby": ROOT / f".ashby_slug_cache_{CRAWL}",
}
BASE = "https://data.commoncrawl.org/"
UA = "slug-extractor/1.0 (you@example.com)"
DELAY = 5.0

QUERY = """
    WITH hits AS (
        SELECT url_host_name AS host, url_path AS path, url_query AS q
        FROM read_parquet(?)
        WHERE fetch_status = 200
          AND url_host_name IN (
              'boards.greenhouse.io',
              'job-boards.greenhouse.io',
              'boards.eu.greenhouse.io',
              'job-boards.eu.greenhouse.io',
              'jobs.ashbyhq.com',
              'api.ashbyhq.com')
    )
    SELECT DISTINCT 'greenhouse' AS board, lower(slug) AS slug FROM (
        SELECT CASE
                 WHEN path LIKE '/embed/%'
                   THEN regexp_extract(q, '(?:^|&)for=([^&]+)', 1)
                 ELSE regexp_extract(path, '^/([^/]+)', 1)
               END AS slug
        FROM hits
        WHERE host IN ('boards.greenhouse.io',
                       'job-boards.greenhouse.io',
                       'boards.eu.greenhouse.io',
                       'job-boards.eu.greenhouse.io')
    )
    WHERE slug <> '' AND slug <> 'embed'
    UNION ALL
    SELECT DISTINCT 'ashby' AS board, slug FROM (
        SELECT CASE
                 WHEN host = 'api.ashbyhq.com'
                   THEN regexp_extract(path, '^/posting-api/job-board/([^/]+)', 1)
                 ELSE regexp_extract(path, '^/([^/]+)', 1)
               END AS slug
        FROM hits
        WHERE host IN ('jobs.ashbyhq.com', 'api.ashbyhq.com')
    )
    WHERE slug <> ''
      AND regexp_matches(slug, '^[A-Za-z0-9][A-Za-z0-9._~-]*$')
      AND lower(slug) NOT IN ('api', 'embed', 'static', 'assets', '_next',
                              'favicon.ico', 'robots.txt', 'sitemap.xml',
                              'login', 'signup', 'health', 'privacy', 'terms',
                              'undefined', 'null', 'posting-api', 'job-board')
      AND NOT regexp_matches(lower(slug), '\\.(js|css|png|jpg|svg|ico|json|txt|xml|map|woff2?)$')
"""


def list_index_files(crawl):
    req = urllib.request.Request(
        f"{BASE}crawl-data/{crawl}/cc-index-table.paths.gz",
        headers={"User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            paths = gzip.decompress(response.read()).decode().split()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Could not fetch file list for {crawl} (HTTP {exc.code})"
        ) from exc
    return [BASE + path for path in paths if "subset=warc" in path and path.endswith(".parquet")]


def cache_name(url):
    return os.path.basename(url) + ".txt"


def already_cached(url, cache=None, legacy=None):
    cache = CACHE if cache is None else cache
    legacy = LEGACY if legacy is None else legacy
    name = cache_name(url)
    if (cache / name).exists():
        return True
    return all((directory / name).exists() for directory in legacy.values())


def write_cache(url, rows):
    CACHE.mkdir(exist_ok=True)
    dest = CACHE / cache_name(url)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text("".join(f"{board}\t{slug}\n" for board, slug in rows), encoding="utf-8")
    os.replace(tmp, dest)


def _lines(path):
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        slug = line.strip()
        if slug:
            yield slug


def collect_slugs(cache=None, legacy=None):
    """Return (slug, ats_board) pairs, one spelling per Ashby slug."""
    cache = CACHE if cache is None else cache
    legacy = LEGACY if legacy is None else legacy
    greenhouse = set()
    ashby = {}

    def add_ashby(slug):
        ashby.setdefault(slug.lower(), slug)

    if cache.is_dir():
        for path in sorted(cache.glob("*.txt")):
            for line in _lines(path):
                board, slug = line.split("\t", 1)
                if board == "greenhouse":
                    greenhouse.add(slug)
                elif board == "ashby":
                    add_ashby(slug)

    greenhouse_dir = legacy["greenhouse"]
    if greenhouse_dir.is_dir():
        for path in greenhouse_dir.glob("*.txt"):
            greenhouse.update(_lines(path))

    ashby_dir = legacy["ashby"]
    if ashby_dir.is_dir():
        for path in sorted(ashby_dir.glob("*.txt")):
            for slug in _lines(path):
                add_ashby(slug)

    return [(slug, "greenhouse") for slug in greenhouse] + [
        (slug, "ashby") for slug in ashby.values()
    ]


def store_slugs(rows, seen_at, db_path=None):
    """Insert new slugs. Existing primary keys are skipped."""
    db = sqlite3.connect(DB_PATH if db_path is None else db_path)
    try:
        db.execute(
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
        before = db.execute("SELECT COUNT(*) FROM ats_slug").fetchone()[0]
        db.executemany(
            """
            INSERT OR IGNORE INTO ats_slug (slug, ats_board, discovery_time)
            VALUES (?, ?, ?)
            """,
            ((slug, board, seen_at) for slug, board in rows),
        )
        db.commit()
        after = db.execute("SELECT COUNT(*) FROM ats_slug").fetchone()[0]
    finally:
        db.close()
    return after - before


def remove_caches():
    for directory in (CACHE, *LEGACY.values()):
        if directory.is_dir():
            shutil.rmtree(directory)


def scan_index(files):
    pending = [url for url in files if not already_cached(url)]
    if not pending:
        for index in range(1, len(files) + 1):
            print(f"[{index}/{len(files)}] cached, skipping", flush=True)
        return

    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET threads=1;")
        con.execute("SET http_retries=5;")
        con.execute("SET http_retry_wait_ms=2000;")
        con.execute("SET http_retry_backoff=4;")
        for index, url in enumerate(files, 1):
            if already_cached(url):
                print(f"[{index}/{len(files)}] cached, skipping", flush=True)
                continue
            rows = con.execute(QUERY, [url]).fetchall()
            write_cache(url, rows)
            print(f"[{index}/{len(files)}] {len(rows)} slugs", flush=True)
            time.sleep(DELAY)
    finally:
        con.close()


def main():
    files = list_index_files(CRAWL)
    if not files:
        raise RuntimeError(f"No index files found for {CRAWL}")

    try:
        scan_index(files)
        found = collect_slugs()
        seen_at = int(time.time())
        inserted = store_slugs(found, seen_at)
    except Exception as exc:
        print(
            f"Stopping on error: {exc}\nRerun to resume; finished files are cached.",
            file=sys.stderr,
        )
        sys.exit(1)

    remove_caches()
    print(
        f"Inserted {inserted} new slugs into {DB_PATH} "
        f"({len(found)} candidates this run, discovery_time {seen_at})"
    )


if __name__ == "__main__":
    main()
