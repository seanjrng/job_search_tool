"""Thin clients for each ATS's public job-board API.

Every function returns a list of plain dicts with a common shape:
    {"company": str, "title": str, "location": str, "url": str,
     "posted_at": str|None, "description": str}

`description` is HTML or plain text, whatever the ATS gives us, and is
consumed by filters.jd_stack_mismatch() for the JD-based stack dealbreaker
check. It's fetched from the SAME request as the listing wherever the ATS
supports that (Greenhouse ?content=true, Ashby/Lever include it by
default) — no extra per-job request needed, so this doesn't multiply your
call volume. Workable's widget API may or may not include it depending on
account; if absent, `description` is just "" and the stack check is
skipped for that job (title/location filters still apply).

NOTE ON THIS SANDBOX: outbound HTTP to arbitrary domains (e.g.
boards-api.greenhouse.io) is blocked by this cloud environment's network
allowlist — that's a property of THIS dev sandbox, not of the ATS APIs
themselves (verified working via WebFetch during development). Run this
module on a machine/server with normal internet access — your laptop, a
cron box, a small VM — and it will work as-is.

CONFIDENCE NOTE on fetch_smartrecruiters specifically (2026-08-12): unlike
Greenhouse/Ashby/Workable/Lever, which were each confirmed against a real
live response during development, SmartRecruiters' shape here is built
from their docs (developers.smartrecruiters.com) plus two independent
third-party integrations that describe the same shape (jobspipe.dev,
an Apify scraper) — this sandbox's WebFetch got blocked by
api.smartrecruiters.com's robots.txt, so it's NOT been hit live the way
the others were. Verify it the same way Coveo/Treewalk's slugs got
verified: `python -m app.main --company <slug>` against a real
SmartRecruiters company before trusting it in a real run.
"""
import re
from urllib.parse import quote

import httpx

from app import filters

USER_AGENT = "job-search-pipeline/0.1 (personal use)"
TIMEOUT = 20.0


def fetch_greenhouse(company_display_name: str, slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = httpx.get(
        url, params={"content": "true"}, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "company": company_display_name,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted_at": j.get("first_published") or j.get("updated_at"),
            "description": j.get("content", ""),  # HTML
        })
    return jobs


def fetch_ashby(company_display_name: str, slug: str) -> list[dict]:
    # Ashby's public posting-API endpoint (no auth needed for public boards).
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or j.get("locationName") or ""
        jobs.append({
            "company": company_display_name,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "posted_at": j.get("publishedAt"),
            "description": j.get("descriptionPlain") or j.get("descriptionHtml") or "",
        })
    return jobs


def fetch_workable(company_display_name: str, slug: str) -> list[dict]:
    # Workable's public widget API.
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or {}
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
        if loc.get("workplace") == "remote":
            loc_str = f"Remote ({loc_str})" if loc_str else "Remote"
        jobs.append({
            "company": company_display_name,
            "title": j.get("title", ""),
            "location": loc_str,
            "url": j.get("url") or j.get("shortlink", ""),
            "posted_at": j.get("published_on") or j.get("created_at"),
            # Workable's widget API doesn't reliably include a description
            # field across all accounts — treat missing as unknown, not
            # as "no dealbreaker language", filters.py handles empty safely.
            "description": j.get("description", ""),
        })
    return jobs


def fetch_lever(company_display_name: str, slug: str) -> list[dict]:
    # Lever's public postings API. EU-hosted sites 404 on the global host.
    headers = {"User-Agent": USER_AGENT}
    urls = (
        f"https://api.lever.co/v0/postings/{slug}?mode=json",
        f"https://api.eu.lever.co/v0/postings/{slug}?mode=json",
    )
    resp = None
    for url in urls:
        resp = httpx.get(url, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 404:
            continue
        break
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data:
        cats = j.get("categories", {}) or {}
        loc = cats.get("location", "")
        all_locs = cats.get("allLocations") or []
        if all_locs:
            loc = ", ".join(all_locs)
        jobs.append({
            "company": company_display_name,
            "title": j.get("text", ""),
            "location": loc,
            "url": j.get("hostedUrl", ""),
            "posted_at": j.get("createdAt"),  # epoch millis
            "description": j.get("descriptionPlain") or j.get("description", ""),
        })
    return jobs


def _fetch_smartrecruiters_description(slug: str, posting_id: str) -> str:
    """SmartRecruiters' list endpoint (fetch_smartrecruiters below) doesn't
    include the JD text — only the per-posting detail endpoint does, one
    extra request per job. Best-effort: any failure (private board, 404,
    timeout) just means no description, not a crashed run; filters.py
    treats an empty description as "unknown, don't reject on stack alone"."""
    try:
        resp = httpx.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}",
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        sections = (resp.json().get("jobAd") or {}).get("sections") or {}
        parts = [
            (sections.get(key) or {}).get("text", "")
            for key in ("jobDescription", "qualifications", "additionalInformation")
        ]
        return "\n\n".join(p for p in parts if p)
    except Exception:
        return ""


def fetch_smartrecruiters(company_display_name: str, slug: str) -> list[dict]:
    # SmartRecruiters' public Posting API — only enabled per-account (not
    # every customer turns it on), same "might just 404" caveat as the
    # other ATSes here. Paginated via limit/offset, 100 postings per page.
    jobs = []
    offset = 0
    limit = 100
    while True:
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        resp = httpx.get(
            url, params={"limit": limit, "offset": offset},
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        if not content:
            break

        for p in content:
            loc = p.get("location") or {}
            loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
            if loc.get("remote"):
                loc_str = f"Remote ({loc_str})" if loc_str else "Remote"

            title = p.get("name", "")
            posting_id = p.get("id", "")
            job_url = p.get("applyUrl") or p.get("ref") or (
                f"https://jobs.smartrecruiters.com/{slug}/{posting_id}" if posting_id else ""
            )
            if not job_url:
                # No applyUrl/ref/id to build any identifier from — skip
                # rather than store url="", which would make dedup.is_new()
                # treat every subsequent url-less posting as a duplicate of
                # the first one (an exact-match dedup key collision).
                continue

            description = ""
            # Only worth the extra per-job request (see
            # _fetch_smartrecruiters_description) for postings that
            # already look like real candidates — same gating idea as
            # aggregator_clients.fetch_adzuna uses for its full-JD fetch,
            # so a company with hundreds of postings doesn't turn into
            # hundreds of extra requests for roles that'd get filtered
            # out on title/location alone anyway.
            if posting_id and filters.title_is_relevant(title) and filters.location_is_allowed(loc_str):
                description = _fetch_smartrecruiters_description(slug, posting_id)

            jobs.append({
                "company": company_display_name,
                "title": title,
                "location": loc_str,
                "url": job_url,
                "posted_at": p.get("releasedDate"),
                "description": description,
            })

        if len(content) < limit:
            break
        offset += limit
    return jobs


# "{tenant}.{wdN}/{site}" — the slug ats_discovery stores for a career site.
_WORKDAY_SLUG = re.compile(
    r"^(?P<tenant>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"\.(?P<wd>wd[0-9]+)/(?P<site>[A-Za-z0-9][A-Za-z0-9._-]*)$"
)
WORKDAY_PAGE = 20
# The public search stops returning new jobs around this offset.
WORKDAY_MAX = 2000


def _workday_list(origin: str, tenant: str, site: str, offset: int):
    url = f"{origin}/wday/cxs/{quote(tenant, safe='')}/{quote(site, safe='')}/jobs"
    return httpx.post(
        url,
        json={
            "appliedFacets": {},
            "limit": WORKDAY_PAGE,
            "offset": offset,
            "searchText": "",
        },
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=TIMEOUT,
    )


def _workday_detail(origin: str, tenant: str, site: str, external_path: str) -> dict:
    """Best-effort job detail. A failure leaves description empty."""
    job_slug = external_path.rstrip("/").rsplit("/", 1)[-1]
    if not job_slug:
        return {}
    url = (
        f"{origin}/wday/cxs/{quote(tenant, safe='')}/{quote(site, safe='')}"
        f"/job/{quote(job_slug, safe='')}"
    )
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        info = resp.json().get("jobPostingInfo") or {}
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def _workday_location(posting: dict, info: dict) -> str:
    parts = []
    loc = info.get("location") if info else None
    if isinstance(loc, str) and loc.strip():
        parts.append(loc.strip())
    extra = info.get("additionalLocations") if info else None
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, list):
        for item in extra:
            text = item if isinstance(item, str) else ""
            if isinstance(item, dict):
                found = item.get("location") or item.get("descriptor") or ""
                text = found if isinstance(found, str) else ""
            if text.strip():
                parts.append(text.strip())
    if parts:
        unique = []
        for part in parts:
            if part not in unique:
                unique.append(part)
        return "; ".join(unique)
    text = posting.get("locationsText") or ""
    return text if isinstance(text, str) else ""


def fetch_workday(company_display_name: str, slug: str) -> list[dict]:
    """Public Workday CXS board.

    slug is "{tenant}.{wdN}/{site}". The list endpoint is POST, paged by
    offset. Descriptions and the real start date come from a second GET
    per job, and only for postings that already pass the title and
    location filters. A 404 on myworkdayjobs.com is tried once on
    myworkdaysite.com.
    """
    match = _WORKDAY_SLUG.fullmatch(slug)
    if match is None:
        raise ValueError(f"Not a Workday slug: {slug}")
    tenant, wd, site = match.group("tenant"), match.group("wd"), match.group("site")
    origins = (
        f"https://{tenant}.{wd}.myworkdayjobs.com",
        f"https://{wd}.myworkdaysite.com",
    )
    response = None
    origin = None
    data = None
    for candidate in origins:
        response = _workday_list(candidate, tenant, site, 0)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        origin = candidate
        data = response.json()
        break
    else:
        response.raise_for_status()

    jobs = []
    offset = 0
    while isinstance(data, dict):
        postings = data.get("jobPostings") or []
        if not isinstance(postings, list) or not postings:
            break
        total = data.get("total")
        for posting in postings:
            if not isinstance(posting, dict):
                continue
            external_path = posting.get("externalPath") or ""
            if not isinstance(external_path, str) or not external_path.startswith("/"):
                continue
            title = posting.get("title") or ""
            if not isinstance(title, str):
                title = ""
            location = posting.get("locationsText") or ""
            if not isinstance(location, str):
                location = ""
            info = {}
            if filters.title_is_relevant(title) and filters.location_is_allowed(location):
                info = _workday_detail(origin, tenant, site, external_path)
            posted = posting.get("postedOn")
            if info:
                posted = info.get("startDate") or info.get("postedOn") or posted
                location = _workday_location(posting, info)
            jobs.append({
                "company": company_display_name,
                "title": title,
                "location": location,
                "url": f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{external_path}",
                "posted_at": posted,
                "description": (info.get("jobDescription") or "") if info else "",
            })
        offset += WORKDAY_PAGE
        if len(postings) < WORKDAY_PAGE:
            break
        if isinstance(total, int) and offset >= total:
            break
        if offset >= WORKDAY_MAX:
            break
        response = _workday_list(origin, tenant, site, offset)
        response.raise_for_status()
        data = response.json()
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def fetch_company(company: dict) -> list[dict]:
    fetcher = FETCHERS.get(company["ats"])
    if fetcher is None:
        raise ValueError(f"No fetcher for ATS type: {company['ats']}")
    jobs = fetcher(company["name"], company["slug"])
    for j in jobs:
        j["source"] = company["ats"]
    return jobs