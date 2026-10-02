"""html_to_markdown converts CMS HTML and leaves every other string alone."""
from app.html_to_markdown import html_to_markdown


def test_non_html_is_returned_unchanged():
    plain = "Build APIs in Python.\n\nQ&amp;A with the team. Use vector<int> when a < b."
    assert html_to_markdown(plain) is plain
    assert html_to_markdown("") == ""
    assert html_to_markdown("   ") == "   "


def test_cms_html_becomes_markdown():
    raw = (
        "<div class='editor' contenteditable='true' data-start='4' id='jd' style='color:red'>"
        "<p>&nbsp;</p>"
        "<h2>Role</h2>"
        "<p>We use <b>Python</b> and FastAPI.</p>"
        "<p>One<br><br><br>Two</p>"
        "<ul><li><p>Ship it</p></li><li>Review it</li></ul>"
        "<p><a href='https://example.com/jobs'>Apply</a></p>"
        "</div>"
    )
    text = html_to_markdown(raw)
    assert text.startswith("## Role")
    assert "We use **Python** and FastAPI." in text
    assert "- Ship it" in text
    assert "- Review it" in text
    assert "[Apply](https://example.com/jobs)" in text
    assert "contenteditable" not in text
    assert "data-start" not in text
    assert "<" not in text
    assert text.count("\n\n\n") == 0


def test_escaped_cms_html_is_converted_once():
    raw = (
        "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;&lt;strong&gt;Python&lt;/strong&gt;&lt;/li&gt;&lt;/ul&gt;"
    )
    text = html_to_markdown(raw)
    assert "Hello & welcome" in text
    assert "- **Python**" in text
    assert "&lt;" not in text
    assert "<" not in text


def test_noise_tags_pixels_and_base64_images_are_removed():
    raw = (
        "<p>Visible</p>"
        "<script>SECRET_SCRIPT</script>"
        "<style>SECRET_STYLE</style>"
        "<noscript>SECRET_NOSCRIPT</noscript>"
        "<svg><text>SECRET_SVG</text></svg>"
        "<iframe src='https://evil.example/SECRET_FRAME'></iframe>"
        "<img src='data:image/png;base64,AAAASECRET' alt='embedded'>"
        "<img src='https://t.example/pixel.gif' width='1' height='1' alt='dot'>"
        "<img src='https://cdn.example/logo.png' alt='Logo' srcset='data:image/png;base64,BBBBSECRET'>"
    )
    text = html_to_markdown(raw)
    assert "Visible" in text
    assert "![Logo](https://cdn.example/logo.png)" in text
    for secret in (
        "SECRET_SCRIPT", "SECRET_STYLE", "SECRET_NOSCRIPT", "SECRET_SVG",
        "SECRET_FRAME", "AAAASECRET", "BBBBSECRET", "pixel.gif",
    ):
        assert secret not in text
