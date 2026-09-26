"""Public search decisions, result parsing, and safe source visits."""

from types import SimpleNamespace

import pytest

from kantoku.capabilities import web_search


def test_search_decision_uses_model_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search, "chat", lambda *_args, **_kwargs: SimpleNamespace(
            content='{"search":true,"query":"北京最新天气"}',
        ),
    )
    assert web_search.plan_web_search("北京天气怎么样？", trace_id="test") == "北京最新天气"


def test_search_html_only_reports_returned_https_domains() -> None:
    payload = b"""<html><body>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa">News</a>
    <a class="result__snippet">Summary</a>
    <a class="result__a" href="http://internal.local/a">Unsafe</a>
    </body></html>"""
    results = web_search.parse_search_results(payload, max_results=4)
    assert len(results) == 1
    assert results[0].domain == "example.org"
    assert results[0].url == "https://example.org/a"


def test_search_visit_rejects_private_addresses() -> None:
    with pytest.raises(ValueError, match="非公开地址"):
        web_search._require_public_https("https://127.0.0.1/private")
    with pytest.raises(ValueError, match="公开 HTTPS"):
        web_search._require_public_https("http://example.org/")
