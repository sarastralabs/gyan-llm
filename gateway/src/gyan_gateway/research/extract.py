"""Turn HTML into clean text and pick the passages that answer the question."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

MIN_PAGE_CHARS = 150

_STOPWORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has have how i in is it "
    "its of on or that the their them then there these they this to was were what when where "
    "which who why will with would you your about into than also not no yes".split()
)


@dataclass(frozen=True)
class Page:
    title: str
    text: str


def extract_page(html: str, url: str = "") -> Page | None:
    """Main readable text and title of a page, or None if there is not enough real content.

    Pages that need JavaScript to show their content usually come back empty here; the
    pipeline then falls back to the search snippet.
    """
    import trafilatura

    try:
        text = trafilatura.extract(
            html,
            url=url or None,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
            # Not `deduplicate=True`: it keeps a process-wide memory of text it has seen and
            # drops repeats, so a page read a few times (cache expiry, retries) would come
            # back empty.
        )
        meta = trafilatura.extract_metadata(html, default_url=url or None)
    except Exception:  # extraction must never fail a request
        return None
    if not text:
        return None
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_PAGE_CHARS:
        return None
    title = (getattr(meta, "title", "") or "").strip()
    return Page(title=title, text=text)


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w{2,}", text.lower()) if t not in _STOPWORDS]


def is_relevant(query: str, text: str) -> bool:
    """Does `text` mention enough of the question's words to be worth showing the model?

    Used for search snippets, which search engines sometimes return for unrelated pages. A
    short question needs one matching word; a longer one needs two.
    """
    terms = set(_tokens(query))
    if not terms:
        return True
    needed = 1 if len(terms) <= 3 else 2
    return len(terms & set(_tokens(text))) >= needed


def _cut(paragraph: str, limit: int) -> str:
    """Shorten to `limit` characters, preferring to end at a sentence."""
    if len(paragraph) <= limit:
        return paragraph
    clipped = paragraph[:limit]
    end = max(clipped.rfind(". "), clipped.rfind("? "), clipped.rfind("! "))
    return clipped[: end + 1] if end > limit * 0.5 else clipped.rstrip() + "..."


def best_passages(text: str, query: str, max_chars: int) -> str:
    """The paragraphs most relevant to `query`, in original order, within `max_chars`.

    A small keyword-overlap score; good enough to keep the useful part of a long page when the
    model can only read a few thousand characters.
    """
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    if not paragraphs:
        return ""
    if len(text) <= max_chars:
        return "\n".join(paragraphs)

    query_terms = set(_tokens(query))
    scored: list[tuple[float, int]] = []
    for index, paragraph in enumerate(paragraphs):
        terms = set(_tokens(paragraph))
        overlap = len(query_terms & terms)
        # Favour dense matches over long rambling paragraphs; keep the lead paragraph as context.
        score = overlap / math.sqrt(1 + len(paragraph) / 200)
        if index == 0:
            score += 0.5
        scored.append((score, index))

    chosen: dict[int, str] = {}
    used = 0
    for score, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        if score <= 0 and chosen:
            break
        room = max_chars - used
        if room < 80:
            break
        piece = _cut(paragraphs[index], room)
        chosen[index] = piece
        used += len(piece) + 1
    return "\n".join(chosen[i] for i in sorted(chosen))
