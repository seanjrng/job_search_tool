"""Write a Markdown digest of the jobs shown on the review board.

One section per job that passed filters and scored 55 or above, highest
match score first. Jobs with no match score, or a match score below 55,
are left out. Jobs you have marked Applied or Skipped are left out too:
Applied means you applied, Skipped means it is not a fit. Each remaining
section carries the information the detail panel
shows: title, company, location, AI status, score, your status, notes,
link, transferable strengths, genuine gaps, risk factors, and the job
description.

Marks stick in user_status, the same row the review board edits. Two ways
to set them, then the digest is rewritten without those jobs:

    # In data/digest.md, set **My status:** to Applied or Skipped, then:
    python -m app.digest

    python -m app.digest --applied URL [URL ...]
    python -m app.digest --skipped URL [URL ...]   # not a fit
    python -m app.digest --restore URL [URL ...]   # put those jobs back
"""
import argparse
import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

from app import dedup

OUTPUT_PATH = Path("data/digest.md")

# Labels match web/app/constants.ts — that is what the detail panel shows.
AI_STATUS_LABEL = {
    "apply": "Apply",
    "consider": "Consider",
    "skip": "Skip",
    "not_evaluated": "Not evaluated",
}
MY_STATUS_LABEL = {
    "consider": "To consider",
    "applied": "Applied",
    "interview": "Interview",
    "rejected": "Rejected",
    "skipped": "Skipped",
    "silence": "Silence",
}

# Applied: you already applied. Skipped: not a fit. Either one drops the
# job from the digest. Interview, rejected, and silence stay, so a role
# still in motion remains in the file. The review board still lists all of them.
EXCLUDED_STATUSES = frozenset({"applied", "skipped"})
# Same bar ai_evaluate uses when it prints a job as rejected.
MIN_MATCH_SCORE = 55
_STATUS_PLACEHOLDERS = frozenset({"", "—", "-", "— not set —", "- not set -", "not set"})
_SKIPPED_MARKS = frozenset({"skipped", "skip", "pass", "not a fit", "not a good fit"})
# Display labels we write for statuses that should stay in the digest.
_KEPT_STATUS_LABELS = frozenset({
    "to consider", "interview", "rejected", "silence",
})
_MARK_STATUS_RE = re.compile(r"(?m)^\*\*My status:\*\* (.+?)\s*$")
_MARK_LINK_RE = re.compile(r"(?m)^\*\*Link:\*\* (\S+)\s*$")

# Same shape check as web/lib/formatDescription.ts: a real tag, not "a < b".
_HTML_TAG_RE = re.compile(r"<[a-z][\s\S]*?>", re.IGNORECASE)
_OL_MARK_RE = re.compile(r"\d\.$")


class _HtmlToText(HTMLParser):
    """Block-aware HTML to plain text. Keeps paragraphs and lists; drops tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.list_kind: list[str] = []
        self.ol_n: list[int] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(alt)
            return
        if tag in ("ul", "ol"):
            self._break(blank=True)
            self.list_kind.append(tag)
            if tag == "ol":
                self.ol_n.append(0)
            return
        if tag == "li":
            self._break(blank=False)
            self.parts.append(self._marker())
            return
        if tag == "br":
            self._break(blank=False)
            return
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre", "section"):
            self._break(blank=True)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style"):
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag == "ol" and self.ol_n:
            self.ol_n.pop()
        if tag in ("ul", "ol") and self.list_kind:
            self.list_kind.pop()

    def handle_data(self, data):
        if self.skip or not data:
            return
        text = data.replace("\xa0", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
        text = re.sub(r" {2,}", " ", text)
        if text.strip() == "":
            if self.parts and not self.parts[-1].endswith((" ", "\n")):
                self.parts.append(" ")
            return
        if text.startswith(" ") and self.parts and self.parts[-1].endswith((" ", "\n")):
            text = text.lstrip(" ")
        self.parts.append(text)

    def _marker(self) -> str:
        depth = len(self.list_kind)
        indent = "  " * (depth - 1) if depth else ""
        if self.list_kind and self.list_kind[-1] == "ol":
            self.ol_n[-1] += 1
            return f"{indent}{self.ol_n[-1]}. "
        return f"{indent}- "

    def _break(self, blank: bool) -> None:
        if not self.parts:
            return
        self.parts[-1] = self.parts[-1].rstrip(" \t")
        if self.parts[-1] == "":
            self.parts.pop()
            if not self.parts:
                return
        tail = self.parts[-1]
        # A list marker is waiting for its item text. Don't push that onto
        # its own line when the item starts with <p> or <div>.
        if tail.endswith("-") or _OL_MARK_RE.search(tail):
            self.parts[-1] = tail + " "
            return
        if blank:
            if tail.endswith("\n\n"):
                return
            self.parts.append("\n" if tail.endswith("\n") else "\n\n")
        elif not tail.endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        raw = "".join(self.parts).replace("\xa0", " ")
        lines = [line.rstrip() for line in raw.split("\n")]
        collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        return collapsed.strip()


def description_to_text(raw: str) -> str:
    """Readable text for a JD stored as HTML, entity-escaped HTML, or plain text.

    Mirrors the three shapes web/lib/formatDescription.ts detects, then
    flattens HTML enough to read in a Markdown file.
    """
    if not raw or not raw.strip():
        return ""
    if _HTML_TAG_RE.search(raw):
        html_src = raw
    else:
        decoded = html.unescape(raw)
        if _HTML_TAG_RE.search(decoded):
            html_src = decoded
        else:
            return decoded.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n").strip()
    parser = _HtmlToText()
    parser.feed(html_src)
    parser.close()
    return parser.text()


def _one_line(value: str) -> str:
    return " ".join((value or "").split())


def render_job(job: dict) -> str:
    score = job.get("match_score")
    title = _one_line(job.get("title") or "") or "Untitled"
    company = _one_line(job.get("company") or "")
    score_prefix = "—" if score is None else str(score)
    if company:
        heading = f"## {score_prefix} · {company} — {title}"
    else:
        heading = f"## {score_prefix} · {title}"

    status = job.get("recommendation") or "not_evaluated"
    my_status = (job.get("my_status") or "").strip()
    lines = [
        heading,
        "",
        f"**Company:** {company}",
        f"**Location:** {_one_line(job.get('location') or '')}",
        f"**AI status:** {AI_STATUS_LABEL.get(status, status)}",
    ]
    if score is not None:
        lines.append(f"**Score:** {score}")
    lines.append(
        "**My status:** " + (MY_STATUS_LABEL.get(my_status, my_status) if my_status else "— not set —")
    )
    lines.append(f"**Link:** {job.get('url') or ''}")
    lines.append("")
    lines.append("### Notes")
    lines.append("")
    lines.append((job.get("notes") or "").strip() or "—")
    lines.append("")

    # The panel hides these three until a score exists.
    if score is not None:
        for heading_text, key in (
            ("Transferable strengths", "transferable_strengths"),
            ("Genuine gaps", "genuine_gaps"),
            ("Risk factors", "risk_factors"),
        ):
            lines.append(f"### {heading_text}")
            lines.append("")
            lines.append((job.get(key) or "").strip() or "—")
            lines.append("")

    lines.append("### Job description")
    lines.append("")
    lines.append(description_to_text(job.get("description") or "") or "No description.")
    lines.append("")
    return "\n".join(lines)


def render_digest(jobs: list[dict], excluded: int = 0) -> str:
    noun = "job" if len(jobs) == 1 else "jobs"
    lines = [
        "# Job digest",
        "",
        f"{len(jobs)} scored {noun}, highest score first.",
    ]
    if excluded:
        excluded_noun = "job" if excluded == 1 else "jobs"
        lines.append(f"{excluded} excluded {excluded_noun} (applied or not a fit).")
    lines.append(
        "Set **My status:** to `Applied` or `Skipped`, then run `make digest` to drop a job."
    )
    lines.append("")
    header = "\n".join(lines) + "\n"
    if not jobs:
        return header
    return header + "\n---\n\n".join(render_job(job) for job in jobs)


def _open_and_excluded(jobs: list[dict]) -> tuple[list[dict], int]:
    open_jobs = []
    excluded = 0
    for job in jobs:
        score = job.get("match_score")
        status = (job.get("my_status") or "").strip().casefold()
        if status in EXCLUDED_STATUSES or (score is not None and score < MIN_MATCH_SCORE):
            excluded += 1
        else:
            open_jobs.append(job)
    return open_jobs, excluded


def write_digest(db_path: str = dedup.DB_PATH, output: Path = OUTPUT_PATH) -> int:
    with dedup.connect(db_path) as conn:
        jobs = list(dedup.iter_board_jobs(conn))
    open_jobs, excluded = _open_and_excluded(jobs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_digest(open_jobs, excluded), encoding="utf-8")
    return len(open_jobs)


def _status_mark(raw: str) -> str | None:
    """Map a digest status line to a stored status, or None to leave it alone.

    'unrecognized' means the line is not a status we write and not an
    exclusion mark, so the caller should warn instead of saving it.
    """
    key = raw.strip().casefold().rstrip(".")
    if key in _STATUS_PLACEHOLDERS or key in _KEPT_STATUS_LABELS:
        return None
    if key == "applied":
        return "applied"
    if key in _SKIPPED_MARKS:
        return "skipped"
    return "unrecognized"


def marks_in_digest(text: str) -> list[tuple[str, str]]:
    """(url, status line) from each job section's metadata block.

    Only the lines before the first ### heading count, so a job description
    that happens to contain the same labels is ignored. Sections split on
    the heading shape render_job writes.
    """
    found = []
    for part in re.split(r"(?m)^## \d+ · ", text)[1:]:
        meta = part.partition("\n### ")[0]
        link = _MARK_LINK_RE.search(meta)
        status = _MARK_STATUS_RE.search(meta)
        if link and status:
            found.append((link.group(1), status.group(1).strip()))
    return found


def apply_digest_marks(db_path: str, text: str) -> dict:
    """Save Applied / Skipped edits from a digest. Other status lines stay as stored.

    A line that already matches the status label we would write is left
    alone, so regenerating does not warn about Interview, Rejected, or Silence.
    """
    applied = 0
    skipped = 0
    warnings: list[str] = []
    with dedup.connect(db_path) as conn:
        for url, raw in marks_in_digest(text):
            existing = dedup.get_user_status(conn, url)
            current = (existing or {}).get("my_status")
            label = MY_STATUS_LABEL.get(current, current) if current else "— not set —"
            if raw.casefold() == str(label).casefold():
                continue
            mark = _status_mark(raw)
            if mark is None:
                key = raw.strip().casefold().rstrip(".")
                if key not in _STATUS_PLACEHOLDERS:
                    warnings.append(
                        f"Left {url} unchanged. The digest only records Applied or Skipped; {raw!r} was not saved."
                    )
                continue
            if mark == "unrecognized":
                warnings.append(
                    f"Unrecognized My status {raw!r} for {url}. Use Applied or Skipped."
                )
                continue
            if dedup.get_details_by_url(conn, url) is None:
                warnings.append(f"No stored job for {url}.")
                continue
            dedup.set_my_status(conn, url, mark)
            if mark == "applied":
                applied += 1
            else:
                skipped += 1
    return {"applied": applied, "skipped": skipped, "warnings": warnings}


def _require_jobs(conn, urls: list[str]) -> list[str]:
    missing = []
    for url in urls:
        if dedup.get_details_by_url(conn, url) is None:
            missing.append(url)
    return missing


def refresh_digest(
    db_path: str = dedup.DB_PATH,
    output: Path = OUTPUT_PATH,
    applied: list[str] | None = None,
    skipped: list[str] | None = None,
    restore: list[str] | None = None,
) -> list[str]:
    """Read exclusion marks, apply CLI overrides, and rewrite the digest.

    File marks are applied first, then --applied / --skipped, then --restore,
    so an explicit flag wins over a status still sitting in the file.
    Returns URLs from the CLI flags that are not in the database. Those
    flags are not applied; file marks already read are kept.
    """
    applied = list(applied or [])
    skipped = list(skipped or [])
    restore = list(restore or [])

    if output.exists():
        result = apply_digest_marks(db_path, output.read_text(encoding="utf-8"))
        for warning in result["warnings"]:
            print(warning)
        if result["applied"] or result["skipped"]:
            print(
                f"Marked {result['applied']} applied and {result['skipped']} not a fit from {output}."
            )

    cli_urls = applied + skipped + restore
    with dedup.connect(db_path) as conn:
        missing = _require_jobs(conn, cli_urls)
        if missing:
            for url in missing:
                print(f"No stored job for {url}")
        else:
            for url in applied:
                dedup.set_my_status(conn, url, "applied")
            for url in skipped:
                dedup.set_my_status(conn, url, "skipped")
            restored = 0
            for url in restore:
                current = (dedup.get_user_status(conn, url) or {}).get("my_status") or ""
                if current in EXCLUDED_STATUSES:
                    dedup.set_my_status(conn, url, None)
                    restored += 1
                else:
                    print(f"Not excluded, left as-is: {url}")
            if restored:
                print(f"Restored {restored} {'job' if restored == 1 else 'jobs'} into the digest.")
            if applied or skipped:
                print(f"Marked {len(applied)} applied and {len(skipped)} not a fit from the command line.")

    count = write_digest(db_path, output)
    print(f"Wrote {count} jobs to {output} (highest score first).")
    return missing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write a Markdown digest of review-board jobs scored 55 or above. "
        "Applied and skipped jobs are left out."
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="Markdown file to write")
    parser.add_argument("--db", default=dedup.DB_PATH, help="SQLite database path")
    parser.add_argument(
        "--applied", nargs="+", metavar="URL",
        help="Mark these jobs as applied and leave them out of the digest",
    )
    parser.add_argument(
        "--skipped", nargs="+", metavar="URL",
        help="Mark these jobs as not a fit and leave them out of the digest",
    )
    parser.add_argument(
        "--restore", nargs="+", metavar="URL",
        help="Clear applied/skipped on these jobs and put them back in the digest",
    )
    args = parser.parse_args()
    missing = refresh_digest(
        args.db,
        args.output,
        applied=args.applied,
        skipped=args.skipped,
        restore=args.restore,
    )
    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
