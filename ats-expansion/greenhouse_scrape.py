import requests
import json
from urllib.parse import parse_qs, urlparse

_crawls = ["CC-MAIN-2026-30", "CC-MAIN-2026-25", "CC-MAIN-2026-21"]
_tag = "CC-MAIN-2026-30"
_tokens = set()
_split_objects = []

def loads_jsonl(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print("bad line:", repr(line[:200]))
            raise
    return rows

def extract_token(url_object: dict) -> str | None:
    url = url_object["url"]
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]

    if host.startswith("boards-api.") and parts[:2] == ["v1", "boards"] and len(parts) >= 3:
        return parts[2]

    # boards.greenhouse.io/embed/job_board?for=stripe
    if parts[:2] == ["embed", "job_board"]:
        return parse_qs(parsed.query).get("for", [None])[0]

    if parts:
        return parts[0]
    return None

def get_page(path: str, page_number: int) -> str:
    response = requests.get(path, params={"url": "boards.greenhouse.io/*", "fl": "url", "output": "json", "page": page_number, "pageSize": 5}, timeout=360 )
    response.raise_for_status()
    print("got response from shop")
    text = response.text
    last = next((line for line in reversed(text.splitlines()) if line.strip()), "")
    json.loads(last)  # raises if the page was cut off
    return text

def read_number(path="read_page.txt"):
    """Read a number from the first line of a text file and return it."""
    with open(path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()

    try:
        return int(first_line)
    except ValueError:
        return float(first_line) 

def increment_and_save(number, path="read_page.txt"):
    """Add one to a number and overwrite the file with the result."""
    result = number + 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(result))
    return result

page_number = read_number()
path = f"https://index.commoncrawl.org/{_tag}-index"
#meta = requests.get(path, params={"url": "boards.greenhouse.io/*", "showNumPages": "true", "pageSize": "1000"})
#if (meta.status_code != 200):
#    print(f"failed to get crawl {_tag}")
#    exit
#else :
#    print("got the crawl")
#    page_counts = meta.json()
print(f"getting page {page_number}")
page = loads_jsonl(get_page(path, page_number))
_split_objects += page

for url_object in _split_objects:
    token = extract_token(url_object)
    if (token is None):
        print(f"token not found {url_object}")
        continue
    _tokens.add(token)
print("tokens retrieved from response")
with open("tokens.txt", "w", encoding="utf-8") as f:
    for item in sorted(_tokens):
        f.write(f"{item}\n")
print("wrote the tokens to the file. incrementing read page number")
increment_and_save(page_number)
