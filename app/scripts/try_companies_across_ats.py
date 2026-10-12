"""For companies seen in your job data (data/seen_jobs.sqlite3) that
aren't already tracked in companies.yaml, guess an ATS slug from the
company name and try it directly against Greenhouse/Ashby/Workable/Lever/
SmartRecruiters's public APIs — the same manual process already used for
AltaML, LawDepot, MongoDB etc. during the Edmonton/Calgary scan, now
automated.

Why this exists (2026-08-12): discover_companies.py auto-adds companies
whose Adzuna redirect_url resolves to a real Greenhouse/Lever API call —
but diagnose_adzuna_redirects.py found that for real Canada/Alberta
queries, 25/25 sampled redirect_urls stayed on adzuna.* and never reached
an external ATS. That path isn't dead code, but it's not going to be the
main way new companies get discovered either. Guessing slugs from company
names you've already SEEN posting real roles (via any source — Adzuna,
Remotive, or companies.yaml itself) is a more direct way to grow the list.

IMPORTANT — this does NOT touch companies.yaml. A slug that returns a
successful API response with jobs isn't automatically the RIGHT board:
Coveo's "coveodeven" was a real, live dev/test board with exactly 1 fake
job, and Treewalk's first guessed slug 404'd even though a *different*
slug for the same company worked fine. Both were only caught by a human
looking at the result. This script prints a report — company, ats, slug,
job count, a few real sample titles — for you to eyeball. Once you've
picked out the real ones, add them with discover_companies.py (see the
snippet this script prints at the end) or by hand in companies.yaml.

Usage (run from the repo root):
    python -m app.scripts.try_companies_across_ats                # first 30 untracked companies seen
    python -m app.scripts.try_companies_across_ats --limit 100     # more (this is slow: up to 10 requests/company)
    python -m app.scripts.try_companies_across_ats --company "Warner Music Group"   # just one, for debugging
"""
import argparse
import re
import sys

import yaml

from app import ats_clients
from app import dedup

ATS_TYPES = ["greenhouse", "ashby", "workable", "lever", "smartrecruiters"]

_SUFFIX_WORDS = {
    "inc", "llc", "ltd", "corp", "corporation", "co", "company", "group",
    "canada", "technologies", "technology", "tech", "the", "holdings",
}
_PUNCT_RE = re.compile(r"[^\w\s()-]")


def candidate_slugs(name: str) -> list[str]:
    """Heuristic, not authoritative — every result still needs a human
    look before it goes in companies.yaml. Splits on parentheses too:
    "Fleetworthy (Bestpass)" needs "bestpass" as a candidate, not just
    "fleetworthy" or "fleetworthybestpass" — that's the REAL slug for
    that company (already in companies.yaml), so this pattern is proven
    to matter, not speculative."""
    parts = [p.strip() for p in re.split(r"[()]", name) if p.strip()]
    candidates = []
    for part in parts:
        base = _PUNCT_RE.sub("", part.lower())
        words = [w for w in base.split() if w not in _SUFFIX_WORDS]
        if not words:
            words = base.split()  # name was ALL suffix-words -> don't end up with nothing
        for c in ("".join(words), "-".join(words)):
            if c and c not in candidates:
                candidates.append(c)
    return candidates


def load_known_names(path: str = "companies.yaml") -> set[str]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {c["name"].strip().lower() for c in data.get("companies", [])}


def try_one(company_display_name: str, ats: str, slug: str):
    """Returns the job list on success (possibly empty), None on any
    failure (404, timeout, malformed response, unsupported ats)."""
    fetcher = ats_clients.FETCHERS.get(ats)
    if fetcher is None:
        return None
    try:
        return fetcher(company_display_name, slug)
    except Exception:
        return None


def scan(companies: list[str]) -> tuple[list[dict], list[dict]]:
    """Returns (confirmed, empty_hits). confirmed = slug found a board
    with >0 open jobs. empty_hits = slug found a board that responded
    successfully but has 0 open jobs right now (worth a second look later,
    lower priority than `confirmed`)."""
    confirmed, empty_hits = [], []
    for i, name in enumerate(companies, 1):
        print(f"[{i}/{len(companies)}] {name}...", file=sys.stderr)
        slugs = candidate_slugs(name)
        for slug in slugs:
            for ats in ATS_TYPES:
                jobs = try_one(name, ats, slug)
                if jobs is None:
                    continue
                entry = {"name": name, "ats": ats, "slug": slug, "count": len(jobs),
                         "sample_titles": [j["title"] for j in jobs[:3]]}
                (confirmed if jobs else empty_hits).append(entry)
    return confirmed, empty_hits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30,
                         help="Max companies to try this run (default 30 — this is slow, up to 10 requests each)")
    parser.add_argument("--company", help="Just try one company name, ignoring companies.yaml/seen-companies filtering")
    args = parser.parse_args()

    if args.company:
        todo = [args.company]
    else:
        with dedup.connect() as conn:
            seen = dedup.distinct_companies(conn)
        known = load_known_names()
        todo = [c for c in seen if c.strip().lower() not in known]
        if not todo:
            print("Nothing to try — every company in job_details is already in companies.yaml, "
                  "or job_details is empty (run main.py first).")
            return
        todo = todo[: args.limit]

    print(f"Trying {len(todo)} compan{'y' if len(todo) == 1 else 'ies'} against "
          f"{', '.join(ATS_TYPES)}...\n", file=sys.stderr)

    confirmed, empty_hits = scan(todo)

    print("\n=== CONFIRMED — board exists, has open roles right now (review before adding!) ===")
    if not confirmed:
        print("(none)")
    for c in confirmed:
        titles = "; ".join(c["sample_titles"])
        print(f"{c['name']:30.30s} | {c['ats']:10s} | slug={c['slug']:22s} | {c['count']:3d} jobs | e.g. {titles}")

    print("\n=== Board exists but 0 open jobs right now (lower priority, could still be the right slug) ===")
    if not empty_hits:
        print("(none)")
    for c in empty_hits:
        print(f"{c['name']:30.30s} | {c['ats']:10s} | slug={c['slug']}")

    tried_names = {c["name"] for c in confirmed} | {c["name"] for c in empty_hits}
    not_found = [n for n in todo if n not in tried_names]
    print(f"\n=== No working slug guessed for {len(not_found)} compan{'y' if len(not_found) == 1 else 'ies'} "
          f"(needs a manual look, like Jobber/Clio/Kinaxis in the README) ===")
    for n in not_found:
        print(f"  {n}")

    if confirmed:
        print("\n=== Once you've checked the CONFIRMED list above, add the real ones like this: ===")
        print("python3 -c \"")
        print("from app import discover_companies")
        print("discover_companies.append_new_companies([")
        for c in confirmed:
            print(f'    {{"name": {c["name"]!r}, "ats": {c["ats"]!r}, "slug": {c["slug"]!r}}},')
        print("])\"")
        print("# (edit the list above first — delete any that aren't actually the right company/board)")


if __name__ == "__main__":
    main()