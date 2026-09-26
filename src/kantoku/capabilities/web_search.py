"""Optional public web-search context for the shared Conversation capability."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen

from kantoku.config.settings import SearchSettings
from kantoku.core.llm import chat


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    domain: str


def plan_web_search(content: str, *, trace_id: str) -> str | None:
    """Let the configured chat model decide whether current public facts are needed."""
    response = chat(
        [
            {"role": "system", "content": (
                "Decide whether the user's question requires live public web information. "
                "Search for current events, changing facts, or requested sources; do not search "
                "for greetings, private conversation, or creative generation. "
                'Return only JSON: {"search":true|false,"query":"short search terms"}. '
                "Never claim to have searched."
            )},
            {"role": "user", "content": content[:1200]},
        ],
        temperature=0,
        timeout_s=15,
    )
    raw = str(response.content or "").strip()
    try:
        decision = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]+\}", raw)
        if match is None:
            return None
        try:
            decision = json.loads(match.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(decision, dict) or decision.get("search") is not True:
        return None
    query = str(decision.get("query") or "").strip()[:120]
    return query or None


class _SearchHtmlParser(HTMLParser):
    """Read only result titles, snippets, and real destination URLs."""

    def __init__(self, max_results: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_results = max_results
        self.results: list[SearchResult] = []
        self.current_url = ""
        self.title_parts: list[str] = []
        self.snippet_parts: list[str] = []
        self.capture: str | None = None
        self.capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._flush()
            href = attributes.get("href") or ""
            redirect = urlparse("https:" + href if href.startswith("//") else href)
            if redirect.hostname == "duckduckgo.com":
                href = parse_qs(redirect.query).get("uddg", [""])[0]
            parsed = urlparse(href)
            self.current_url = href if parsed.scheme == "https" and parsed.hostname else ""
            self.capture = "title"
            self.capture_depth = 1
        elif tag == "a" and "result__snippet" in classes and self.current_url:
            self.capture = "snippet"
            self.capture_depth = 1
        elif self.capture and tag not in {"br", "img", "input"}:
            self.capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.capture and tag not in {"br", "img", "input"}:
            self.capture_depth -= 1
            if self.capture_depth <= 0:
                self.capture = None

    def handle_data(self, data: str) -> None:
        if self.capture == "title":
            self.title_parts.append(data)
        elif self.capture == "snippet":
            self.snippet_parts.append(data)

    def _flush(self) -> None:
        if self.current_url and len(self.results) < self.max_results:
            parsed = urlparse(self.current_url)
            title = " ".join(" ".join(self.title_parts).split())[:200]
            snippet = " ".join(" ".join(self.snippet_parts).split())[:400]
            if title and parsed.hostname:
                self.results.append(SearchResult(
                    title, self.current_url, snippet, parsed.hostname.lower(),
                ))
        self.current_url = ""
        self.title_parts = []
        self.snippet_parts = []


def parse_search_results(payload: bytes, *, max_results: int) -> list[SearchResult]:
    """Return only HTTPS destinations actually observed in public search results."""
    parser = _SearchHtmlParser(max_results)
    parser.feed(payload.decode("utf-8", errors="replace"))
    parser._flush()
    return parser.results


def search_web(query: str, settings: SearchSettings) -> list[SearchResult]:
    """Use the configured public search adapter; no user-supplied URL is fetched."""
    if not settings.enabled:
        return []
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query[:120])}"
    request = Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
        ),
    })
    with urlopen(request, timeout=settings.timeout_s) as response:  # noqa: S310
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError("搜索响应超过大小限制")
    return parse_search_results(payload, max_results=settings.max_results)
