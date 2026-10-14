"""Validate discovered ATS slugs and append the live ones to companies.yaml.

Reads ats_slug rows with available = 1 from data/ats_discovery.sqlite3. Each slug is
checked only against its own board:

    https://boards-api.greenhouse.io/v1/boards/{slug}
    https://api.ashbyhq.com/posting-api/job-board/{slug}

A 200 is appended to companies.yaml. last_seen becomes the current epoch
second and available stays 1. A failed check sets available to 0 and leaves
last_seen unchanged. Transient request failures wait 10 seconds before each
retry. (ats, slug) pairs already present in companies.yaml are skipped.
Failures are also written to failed_tokens.txt as ``slug<TAB>ats=reason``.
"""

import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT.parent / "data" / "ats_discovery.sqlite3"
COMPANIES_PATH = ROOT / "companies.yaml"
FAILURES_PATH = ROOT / "failed_tokens.txt"
ATS_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
}
WORKERS = 8
TIMEOUT = 20
RETRIES = 4
RETRY_WAIT = 10

_thread = threading.local()


def yaml_scalar(value: str) -> str:
    if "\n" in value or "\r" in value:
        return json.dumps(value, ensure_ascii=False)
    try:
        loaded = yaml.safe_load("k: " + value)
    except yaml.YAMLError:
        return json.dumps(value, ensure_ascii=False)
    if loaded == {"k": value}:
        return value
    return json.dumps(value, ensure_ascii=False)


def load_existing(text: str) -> set[tuple[str, str]]:
    """Return {(ats, lowercased slug)} for entries already in companies.yaml."""
    data = yaml.safe_load(text) or {}
    existing = set()
    for company in data.get("companies") or []:
        slug = company.get("slug")
        if slug is not None:
            ats = str(company.get("ats") or "").lower()
            existing.add((ats, str(slug).lower()))
    return existing


def load_slugs(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return (ats, slug) rows that are still available to check."""
    return db.execute(
        """
        SELECT ats_board, slug
        FROM ats_slug
        WHERE available = 1
          AND ats_board IN ('greenhouse', 'ashby')
        ORDER BY ats_board, slug
        """
    ).fetchall()


def mark_seen(db: sqlite3.Connection, ats: str, slug: str, seen_at: int) -> None:
    """Record a live board. available stays at 1."""
    db.execute(
        "UPDATE ats_slug SET last_seen = ? WHERE slug = ? AND ats_board = ?",
        (seen_at, slug, ats),
    )


def mark_unavailable(db: sqlite3.Connection, ats: str, slug: str) -> None:
    """Record a failed check. last_seen is left as it is."""
    db.execute(
        "UPDATE ats_slug SET available = 0 WHERE slug = ? AND ats_board = ?",
        (slug, ats),
    )


def session() -> requests.Session:
    client = getattr(_thread, "session", None)
    if client is None:
        client = requests.Session()
        client.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "ats-expansion-token-check/1.0",
            }
        )
        _thread.session = client
    return client


def has_foreign(value: str) -> bool:
    return not value.isascii()


def check(ats: str, token: str) -> tuple[str | None, str | None]:
    """Return (company name, None) on 200, or (None, reason) otherwise."""
    if has_foreign(token):
        return None, "non-ascii"
    url = ATS_URLS[ats].format(token=quote(token, safe=""))
    last_reason = "request failed"
    for attempt in range(RETRIES):
        try:
            response = session().get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_reason = type(exc).__name__
            if attempt + 1 < RETRIES:
                time.sleep(RETRY_WAIT)
            continue
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            name = token
            if ats == "greenhouse":
                if isinstance(payload, dict):
                    found = payload.get("name")
                    if isinstance(found, str) and found.strip():
                        name = found.strip()
            elif ats == "ashby":
                # The posting API has no company name; guard against a
                # "soft 200" that isn't a job board payload.
                if not (isinstance(payload, dict) and isinstance(payload.get("jobs"), list)):
                    return None, "unexpected-body"
            if has_foreign(name):
                return None, "non-ascii"
            return name, None
        if response.status_code in (429, 500, 502, 503, 504):
            last_reason = str(response.status_code)
            if attempt + 1 < RETRIES:
                time.sleep(RETRY_WAIT)
            continue
        return None, str(response.status_code)
    return None, last_reason


def entry(name: str, ats: str, slug: str) -> str:
    return (
        f"  - name: {yaml_scalar(name)}\n"
        f"    ats: {ats}\n"
        f"    slug: {yaml_scalar(slug)}\n"
    )


def main() -> None:
    db = sqlite3.connect(DB_PATH)
    try:
        _expand(db)
    finally:
        db.close()


def _expand(db: sqlite3.Connection) -> None:
    pairs = load_slugs(db)

    companies_text = COMPANIES_PATH.read_text(encoding="utf-8")
    existing = load_existing(companies_text)

    pending = [
        (ats, token) for ats, token in pairs if (ats, token.lower()) not in existing
    ]
    print(
        f"pairs={len(pairs)} already_present={len(pairs) - len(pending)} "
        f"tokens_to_check={len(pending)}",
        flush=True,
    )

    if companies_text and not companies_text.endswith("\n"):
        with COMPANIES_PATH.open("a", encoding="utf-8") as handle:
            handle.write("\n")
    FAILURES_PATH.write_text("", encoding="utf-8")

    results: dict[int, tuple] = {}
    next_index = 0
    added = 0
    failed = 0
    lock = threading.Lock()

    def flush_ready() -> None:
        nonlocal next_index, added, failed
        company_lines = []
        failure_lines = []
        while next_index in results:
            ats, token = pending[next_index]
            name, reason = results.pop(next_index)
            if reason is None:
                company_lines.append(entry(name or token, ats, token))
                mark_seen(db, ats, token, int(time.time()))
                added += 1
            else:
                failure_lines.append(f"{token}\t{ats}={reason}\n")
                mark_unavailable(db, ats, token)
                failed += 1
            next_index += 1
        if company_lines:
            with COMPANIES_PATH.open("a", encoding="utf-8") as handle:
                handle.write("".join(company_lines))
        if failure_lines:
            with FAILURES_PATH.open("a", encoding="utf-8") as handle:
                handle.write("".join(failure_lines))
        db.commit()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(check, ats, token): index
            for index, (ats, token) in enumerate(pending)
        }
        done = 0
        for future in as_completed(futures):
            index = futures[future]
            with lock:
                results[index] = future.result()
                flush_ready()
                done += 1
                if done % 100 == 0 or done == len(pending):
                    print(
                        f"checked {done}/{len(pending)} added={added} failed={failed}",
                        flush=True,
                    )

    yaml.safe_load(COMPANIES_PATH.read_text(encoding="utf-8"))
    print(f"done added={added} failed={failed}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)