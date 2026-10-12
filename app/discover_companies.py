"""Auto-discovery: when aggregator_clients.fetch_full_description resolves
an Adzuna redirect to a Greenhouse or Lever company not already tracked in
companies.yaml, main.py queues it here after the run. Getting a company
into companies.yaml means every future run fetches its FULL board directly
(every open role, not just whatever Adzuna happened to surface this time)
for free, without going through the aggregator/JD-fetch dance again.

Trust note: a discovered (ats, slug) pair only ever gets here after a REAL
successful single-job API call in fetch_full_description — strictly more
trustworthy than a slug guessed from a search-result URL. companies.yaml
already has scar tissue on that front (Coveo's "coveodeven" and Treewalk's
display-page-ID slug both burned real API calls on 404s before being
caught by hand) — this sidesteps that failure mode entirely.

Deliberately does NOT rewrite companies.yaml with yaml.safe_dump: that
would silently drop every hand-written comment in the file (the "ats:
one of..." header, the Coveo/Treewalk provenance notes, the "Added from
Edmonton/Calgary scan" section marker). Instead this only ever APPENDS a
new commented block to the end of the file as plain text, leaving
everything already there untouched.
"""
import yaml

COMPANIES_YAML = "companies.yaml"


def load_known_keys(path: str = COMPANIES_YAML) -> set[tuple[str, str]]:
    """(ats, slug) pairs already tracked, so we never suggest a duplicate."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {(c["ats"], c["slug"]) for c in data.get("companies", [])}


def _yaml_str(value: str) -> str:
    """Quote a value the way PyYAML would for a plain scalar, so a company
    name with a colon or a leading special char (rare, but real — e.g.
    "Warner Music Group: Canada") can't corrupt the file structure."""
    return yaml.safe_dump(value, default_style='"').strip()


def append_new_companies(discovered: list[dict], path: str = COMPANIES_YAML, source_note: str = "Adzuna aggregator + full-JD fetch") -> list[dict]:
    """discovered: [{"name", "ats", "slug"}, ...] — may contain duplicates
    (the same company posts multiple roles in one run) and may overlap
    with what's already in companies.yaml. Dedups against both. Appends a
    new commented block for whatever's left; returns exactly what got
    appended (empty list if nothing new — the common case most runs).
    """
    known = load_known_keys(path)
    seen_this_call = set()
    to_add = []
    for c in discovered:
        key = (c["ats"], c["slug"])
        if key in known or key in seen_this_call:
            continue
        seen_this_call.add(key)
        to_add.append(c)

    if not to_add:
        return []

    lines = [f"\n  # Auto-discovered via {source_note}"]
    for c in to_add:
        lines.append(f"  - name: {_yaml_str(c['name'])}")
        lines.append(f"    ats: {c['ats']}")
        lines.append(f"    slug: {c['slug']}")
    block = "\n".join(lines) + "\n"

    with open(path, "a", encoding="utf-8") as f:
        f.write(block)

    return to_add


if __name__ == "__main__":
    # Manual smoke check against the real file, no changes: just show what's
    # currently tracked, since running this standalone with fake data would
    # actually mutate companies.yaml — use test_discover_companies.py for that.
    known = load_known_keys()
    print(f"{len(known)} (ats, slug) pairs currently tracked in {COMPANIES_YAML}:")
    for ats, slug in sorted(known):
        print(f"  {ats:12s} {slug}")