"""Validate Greenhouse board tokens and append the live ones to companies.yaml.

Reads tokens.txt (one board token per line). A GET of
https://boards-api.greenhouse.io/v1/boards/{token} that returns 200 is appended
to companies.yaml. A token or board name with a non-ASCII character is rejected.
Every other result is written to failed_tokens.txt as ``token<TAB>reason`` for
a later pass. Tokens already present as slugs are skipped.
"""

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

ROOT = Path(__file__).resolve().parent
TOKENS_PATH = ROOT / "tokens.txt"
COMPANIES_PATH = ROOT / "companies.yaml"
FAILURES_PATH = ROOT / "failed_tokens.txt"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}"
WORKERS = 8
TIMEOUT = 20
RETRIES = 4

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


def load_slugs(text: str) -> set[str]:
    data = yaml.safe_load(text) or {}
    slugs = set()
    for company in data.get("companies") or []:
        slug = company.get("slug")
        if slug is not None:
            slugs.add(str(slug))
    return slugs


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


def check(token: str) -> tuple[str | None, str | None]:
    """Return (company name, None) on 200, or (None, reason) otherwise."""
    if has_foreign(token):
        return None, "non-ascii"
    url = BOARD_URL.format(token=quote(token, safe=""))
    last_reason = "request failed"
    for attempt in range(RETRIES):
        try:
            response = session().get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_reason = type(exc).__name__
            time.sleep(2**attempt)
            continue
        if response.status_code == 200:
            name = token
            try:
                payload = response.json()
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                found = payload.get("name")
                if isinstance(found, str) and found.strip():
                    name = found.strip()
            if has_foreign(name):
                return None, "non-ascii"
            return name, None
        if response.status_code in (429, 500, 502, 503, 504):
            last_reason = str(response.status_code)
            retry_after = response.headers.get("Retry-After")
            delay = 2**attempt
            if retry_after and retry_after.isdigit():
                delay = min(int(retry_after), 30)
            time.sleep(delay)
            continue
        return None, str(response.status_code)
    return None, last_reason


def entry(name: str, slug: str) -> str:
    return (
        f"  - name: {yaml_scalar(name)}\n"
        f"    ats: greenhouse\n"
        f"    slug: {yaml_scalar(slug)}\n"
    )


def main() -> None:
    tokens = []
    seen = set()
    for line in TOKENS_PATH.read_text(encoding="utf-8").splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if not token or token.startswith("#") or token in seen:
            continue
        seen.add(token)
        tokens.append(token)

    companies_text = COMPANIES_PATH.read_text(encoding="utf-8")
    existing = load_slugs(companies_text)
    pending = [token for token in tokens if token not in existing]
    print(
        f"tokens={len(tokens)} already_present={len(tokens) - len(pending)} to_check={len(pending)}",
        flush=True,
    )

    if companies_text and not companies_text.endswith("\n"):
        with COMPANIES_PATH.open("a", encoding="utf-8") as handle:
            handle.write("\n")
    FAILURES_PATH.write_text("", encoding="utf-8")

    results: dict[int, tuple[str, str | None, str | None]] = {}
    next_index = 0
    added = 0
    failed = 0
    lock = threading.Lock()

    def flush_ready() -> None:
        nonlocal next_index, added, failed
        company_lines = []
        failure_lines = []
        while next_index in results:
            token, name, reason = results.pop(next_index)
            if reason is None:
                company_lines.append(entry(name or token, token))
                added += 1
            else:
                failure_lines.append(f"{token}\t{reason}\n")
                failed += 1
            next_index += 1
        if company_lines:
            with COMPANIES_PATH.open("a", encoding="utf-8") as handle:
                handle.write("".join(company_lines))
        if failure_lines:
            with FAILURES_PATH.open("a", encoding="utf-8") as handle:
                handle.write("".join(failure_lines))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(check, token): (index, token)
            for index, token in enumerate(pending)
        }
        done = 0
        for future in as_completed(futures):
            index, token = futures[future]
            name, reason = future.result()
            with lock:
                results[index] = (token, name, reason)
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
