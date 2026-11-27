"""Turn an Odoo HTML field into the small, safe HTML Jobiba job ads use."""
import re

from lxml import html as lxml_html

from odoo.tools import html_sanitize, html2plaintext

#: Tags kept in the text sent to Jobiba. Everything else is unwrapped.
ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "h3", "h4"}
_HEADING = {"h1": "h3", "h2": "h3", "h5": "h4", "h6": "h4"}
_BLOCK = {"div", "section", "article", "blockquote", "tr", "table"}
_DROP = {"script", "style", "img", "iframe", "svg", "form", "input", "button", "video", "audio", "object", "embed"}


def _is_empty(markup):
    return not (html2plaintext(markup or "") or "").strip()


def clean_job_html(markup):
    """Sanitised HTML with a short tag allowlist and no attributes.

    Links become their text, images and embeds are dropped, so nothing in the
    job ad can point a candidate away from the listing or load remote content.
    """
    if _is_empty(markup):
        return ""
    safe = html_sanitize(markup, strip_style=True, strip_classes=True, sanitize_tags=True)
    try:
        root = lxml_html.fragment_fromstring(str(safe), create_parent="div")
    except Exception:  # unparsable: fall back to plain text
        return plain_job_text(markup)
    for node in list(root.iter()):
        if node is root or not isinstance(node.tag, str):
            continue
        tag = node.tag.lower()
        if tag in _DROP:
            node.drop_tree()
            continue
        if tag in _HEADING:
            node.tag = tag = _HEADING[tag]
        elif tag in _BLOCK:
            node.tag = tag = "p"
        if tag not in ALLOWED_TAGS:
            node.drop_tag()
            continue
        for name in list(node.attrib):
            del node.attrib[name]
    out = "".join(
        lxml_html.tostring(child, encoding="unicode") for child in root
    )
    if root.text and root.text.strip():
        out = "<p>%s</p>%s" % (root.text.strip(), out)
    out = re.sub(r"<p>\s*(?:<br\s*/?>\s*)*</p>", "", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def plain_job_text(markup):
    text = html2plaintext(markup or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
