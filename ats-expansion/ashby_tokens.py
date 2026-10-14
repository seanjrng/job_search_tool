import gzip
import os
import sys
import time
import urllib.error
import urllib.request
import duckdb

CRAWL = sys.argv[1] if len(sys.argv) > 1 else "CC-MAIN-2026-30"
OUT = "ashby_tokens.txt"
CACHE = f".ashby_slug_cache_{CRAWL}"   # per-file results, enables resume
BASE = "https://data.commoncrawl.org/"
UA = "slug-extractor/1.0 (you@example.com)"  # put your real contact info
DELAY = 5.0  # seconds between files; raise it if you still get 503s

# Slug = first path segment on jobs.ashbyhq.com (/{slug}/{job-uuid}/...,
# /{slug}/embed, /{slug}?...), or the third segment on the posting API
# (/posting-api/job-board/{slug}). Case is preserved here because I'm not
# certain Ashby slugs are case-insensitive; dedupe happens after the merge.
QUERY = """
    WITH hits AS (
        SELECT url_host_name AS host, url_path AS path
        FROM read_parquet(?)
        WHERE url_host_name IN ('jobs.ashbyhq.com', 'api.ashbyhq.com')
          AND fetch_status = 200
    )
    SELECT DISTINCT slug FROM (
        SELECT CASE
                 WHEN host = 'api.ashbyhq.com'
                   THEN regexp_extract(path, '^/posting-api/job-board/([^/]+)', 1)
                 ELSE regexp_extract(path, '^/([^/]+)', 1)
               END AS slug
        FROM hits
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
        with urllib.request.urlopen(req, timeout=60) as r:
            paths = gzip.decompress(r.read()).decode().split()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Could not fetch file list for {crawl} (HTTP {e.code})") from e
    return [BASE + p for p in paths if "subset=warc" in p and p.endswith(".parquet")]


if __name__ == "__main__":
    try:
        files = list_index_files(CRAWL)
        if not files:
            raise RuntimeError(f"No index files found for {CRAWL}")
        os.makedirs(CACHE, exist_ok=True)

        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET threads=1;")
        con.execute("SET http_retries=5;")
        con.execute("SET http_retry_wait_ms=2000;")
        con.execute("SET http_retry_backoff=4;")

        for i, url in enumerate(files, 1):
            cache_file = os.path.join(CACHE, os.path.basename(url) + ".txt")
            if os.path.exists(cache_file):
                print(f"[{i}/{len(files)}] cached, skipping", flush=True)
                continue
            slugs = [r[0] for r in con.execute(QUERY, [url]).fetchall()]
            tmp = cache_file + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("".join(s + "\n" for s in slugs))
            os.replace(tmp, cache_file)  # only marks the file done once fully written
            print(f"[{i}/{len(files)}] {len(slugs)} slugs", flush=True)
            time.sleep(DELAY)

        # Merge, deduping case-insensitively (keep the first in sorted order).
        merged = {}
        for name in sorted(os.listdir(CACHE)):
            if name.endswith(".txt"):
                with open(os.path.join(CACHE, name)) as fh:
                    for line in fh:
                        s = line.strip()
                        if s:
                            merged.setdefault(s.lower(), s)
    except Exception as e:
        print(f"Stopping on error: {e}\nRerun to resume; finished files are cached.",
              file=sys.stderr)
        sys.exit(1)

    # Skip slugs already in OUT (case-insensitive) so reruns don't duplicate.
    existing = set()
    needs_newline = False
    if os.path.exists(OUT) and os.path.getsize(OUT) > 0:
        with open(OUT) as fh:
            existing = {line.strip().lower() for line in fh if line.strip()}
        with open(OUT, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_newline = fh.read(1) != b"\n"

    new = sorted((s for k, s in merged.items() if k not in existing), key=str.lower)
    with open(OUT, "a") as fh:
        if needs_newline:
            fh.write("\n")
        fh.writelines(s + "\n" for s in new)

    print(f"Appended {len(new)} new slugs to {OUT} ({len(merged)} found this run)")
