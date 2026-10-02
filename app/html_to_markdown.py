"""Convert an HTML job description to Markdown.

Job text from a CMS is often HTML, sometimes with the tags escaped
(`&lt;p&gt;...`). Plain text is returned exactly as it was passed in.
"""
import re
from html import unescape

from bs4 import BeautifulSoup
from markdownify import ATX, markdownify

# Known tag names only, so C++ snippets like "vector<int>" and comparisons
# like "a < b" stay plain text.
_HTML_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "blockquote", "br",
    "caption", "center", "cite", "code", "col", "colgroup", "dd", "del",
    "details", "div", "dl", "dt", "em", "figcaption", "figure", "font",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i",
    "img", "li", "main", "mark", "nav", "ol", "p", "pre", "s", "section",
    "small", "span", "strike", "strong", "sub", "summary", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9:-]*)\b([^<>]*)>")
_PIXEL_SRC_RE = re.compile(
    r"pixel|spacer|beacon|tracking|cleardot|1x1|blank\.gif|track\.gif",
    re.IGNORECASE,
)
_STYLE_PX_RE = re.compile(
    r"(width|height)\s*:\s*(\d+(?:\.\d+)?)\s*px",
    re.IGNORECASE,
)
_NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe")
# class/id/style, data-*, and editor chrome. href/src/alt stay.
_EDITOR_ATTRS = {
    "style", "class", "id", "contenteditable", "spellcheck",
    "autocorrect", "autocapitalize",
}
_INLINE_TAGS = {
    "a", "abbr", "b", "cite", "code", "em", "font", "i", "label", "mark",
    "s", "small", "span", "strong", "sub", "sup", "u",
}


def html_to_markdown(text: str) -> str:
    """Return Markdown when `text` contains HTML, otherwise `text` unchanged."""
    html = _html_source(text)
    if html is None:
        return text

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    for img in list(soup.find_all("img")):
        if _is_noise_image(img):
            img.decompose()
    for source in list(soup.find_all("source")):
        blob = f"{_attr(source, 'src')} {_attr(source, 'srcset')}"
        if _is_embedded_image(blob) or _PIXEL_SRC_RE.search(blob):
            source.decompose()
    for tag in soup.find_all(True):
        _strip_editor_attrs(tag)
    # Drop empty wrappers first so <hr><h2>&nbsp;</h2><hr> becomes one rule.
    _drop_empty_tags(soup)
    _collapse_repeated(soup, "br")
    _collapse_repeated(soup, "hr")
    return markdownify(str(soup), heading_style=ATX, bullets="-").strip()


def _html_source(text: str):
    if not isinstance(text, str) or not text:
        return None
    if _has_html(text):
        return text
    # Greenhouse stores CMS HTML with the tags escaped. Decode once, and
    # only accept it when that reveals real tags. Other text keeps its
    # original entities, so "Q&amp;A" is not rewritten.
    decoded = unescape(text)
    if decoded != text and _has_html(decoded):
        return decoded
    return None


def _has_html(text: str) -> bool:
    for match in _TAG_RE.finditer(text):
        name = match.group(1).lower()
        if ":" in name:
            name = name.rsplit(":", 1)[-1]
        if name in _HTML_TAGS:
            return True
    return False


def _attr(tag, name: str) -> str:
    value = tag.attrs.get(name)
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return str(value)


def _is_embedded_image(value: str) -> bool:
    low = value.lower().strip()
    return low.startswith("data:") or "base64," in low or " data:" in low


def _px(value):
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else ""
    match = re.match(r"\s*(\d+)", str(value))
    return int(match.group(1)) if match else None


def _is_tracking_pixel(img) -> bool:
    if _PIXEL_SRC_RE.search(_attr(img, "src")):
        return True
    width = _px(img.get("width"))
    height = _px(img.get("height"))
    if width is not None and height is not None and width <= 1 and height <= 1:
        return True
    style = _attr(img, "style")
    found = {prop.lower(): float(size) for prop, size in _STYLE_PX_RE.findall(style)}
    return (
        "width" in found
        and "height" in found
        and found["width"] <= 1
        and found["height"] <= 1
    )


def _is_noise_image(img) -> bool:
    src = _attr(img, "src").strip()
    srcset = _attr(img, "srcset")
    if _is_embedded_image(src) or _is_tracking_pixel(img):
        return True
    # A real src can sit beside a base64 srcset. Drop the payload, keep the image.
    if _is_embedded_image(srcset) and "srcset" in img.attrs:
        del img.attrs["srcset"]
    return False


def _strip_editor_attrs(tag) -> None:
    for name in list(tag.attrs):
        lower = name.lower()
        if lower in _EDITOR_ATTRS or lower.startswith("data-"):
            del tag.attrs[name]


def _collapse_repeated(soup, name: str) -> None:
    """Turn <br><br><br> into one <br>, and the same for <hr>."""
    for tag in list(soup.find_all(name)):
        if tag.parent is None:
            continue
        nxt = tag.next_sibling
        while nxt is not None:
            if isinstance(nxt, str) and not nxt.replace("\xa0", " ").strip():
                drop, nxt = nxt, nxt.next_sibling
                drop.extract()
                continue
            if getattr(nxt, "name", None) == name:
                drop, nxt = nxt, nxt.next_sibling
                drop.decompose()
                continue
            break


def _drop_empty_tags(soup) -> None:
    """Remove tags with no visible text. Repeat so parents empty out too.

    A whitespace-only inline tag becomes a space, so words on either side
    of a CMS <span>&nbsp;</span> do not get glued together.
    """
    while True:
        victims = []
        for tag in soup.find_all(True):
            if tag.name in ("br", "hr", "img"):
                continue
            if tag.find("img") is not None:
                continue
            if tag.get_text().replace("\xa0", " ").strip():
                continue
            victims.append(tag)
        if not victims:
            return
        for tag in victims:
            if tag.parent is None:
                continue
            if tag.name in _INLINE_TAGS:
                tag.replace_with(" ")
            else:
                tag.decompose()
