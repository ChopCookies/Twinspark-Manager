"""MkDocs hook for the documentation site (mkdocs.yml): a link from docs/ to a file outside it — the README,
catalog/, community/, a source file — points at that file on GitHub instead of breaking."""

from __future__ import annotations

import posixpath
import re

REPO = "https://github.com/ChopCookies/Twinspark-Manager/blob/main/"
LINK = re.compile(r"\]\(((?:\.\./)+[^)\s#]*)(#[^)\s]*)?\)")


def on_page_markdown(markdown, page, config, files):
    here = posixpath.dirname(page.file.src_uri)

    def rewrite(m):
        target = posixpath.normpath(posixpath.join("docs", here, m.group(1)))
        if target.startswith("docs/"):
            return m.group(0)                       # still inside docs/: MkDocs resolves it
        return f"]({REPO}{target}{m.group(2) or ''})"

    return LINK.sub(rewrite, markdown)
