"""Write a Markdown digest of the jobs shown on the review board.

One section per scored job that passed filters, highest match score first.
Jobs with no match score are left out. Each section carries the information
the detail panel shows: title, company, location, AI status, score, your
status, notes, link, transferable strengths, genuine gaps, risk factors,
and the job description.

Usage (from the repo root):
    python -m app.digest
    python -m app.digest --output path/to/digest.md
"""
import argparse
import html
import re
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


def render_digest(jobs: list[dict]) -> str:
    noun = "job" if len(jobs) == 1 else "jobs"
    header = f"# Job digest\n\n{len(jobs)} scored {noun}, highest score first.\n\n"
    if not jobs:
        return header
    return header + "\n---\n\n".join(render_job(job) for job in jobs)


def write_digest(db_path: str = dedup.DB_PATH, output: Path = OUTPUT_PATH) -> int:
    with dedup.connect(db_path) as conn:
        jobs = list(dedup.iter_board_jobs(conn))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_digest(jobs), encoding="utf-8")
    return len(jobs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write a Markdown digest of review-board jobs.")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="Markdown file to write")
    parser.add_argument("--db", default=dedup.DB_PATH, help="SQLite database path")
    args = parser.parse_args()
    count = write_digest(args.db, args.output)
    print(f"Wrote {count} jobs to {args.output} (highest score first).")


if __name__ == "__main__":
    main()
