"""Request contract for Sentry issue events."""

from __future__ import annotations

import httpx

from integrations.sentry import SentryConfig
from integrations.sentry.client import list_sentry_issue_events


def test_issue_events_uses_supported_page_size(monkeypatch) -> None:
    """The requested event count must reach Sentry as per_page."""
    requests: list[httpx.Request] = []

    def fake_request(method, url, **kwargs):
        request = httpx.Request(method, url, params=kwargs["params"])
        requests.append(request)
        return httpx.Response(200, json=[{"eventID": "one"}], request=request)

    monkeypatch.setattr(httpx, "request", fake_request)
    config = SentryConfig(organization_slug="acme", auth_token="secret")

    events = list_sentry_issue_events(config=config, issue_id="123", limit=10)

    assert events == [{"eventID": "one"}]
    assert requests[0].url.path == "/api/0/organizations/acme/issues/123/events/"
    assert requests[0].url.params == {"per_page": "10"}
