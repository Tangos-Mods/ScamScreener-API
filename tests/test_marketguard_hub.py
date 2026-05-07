from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

from app.marketguard_hub.config import MarketGuardHubSettings
from app.marketguard_hub.main import create_marketguard_hub_app


def test_marketguard_hub_root_renders_lowestbin_page() -> None:
    app = create_marketguard_hub_app(_settings())

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'data-view="lowestbin"' in response.text
    assert 'data-api-url="/api/v2/lowestbin"' in response.text
    assert 'href="/market/"' in response.text
    assert 'href="/market/bazaar"' in response.text
    assert '/market/assets/css/bootstrap.min.css' in response.text
    assert '/market/assets/css/marketguard-hub.css' in response.text
    assert '/market/assets/js/marketguard-hub.js' in response.text
    assert 'id="market-last-updated"' in response.text
    assert 'id="market-product-count"' in response.text
    assert 'id="market-data-state"' in response.text
    assert 'id="market-refresh-button"' in response.text
    assert 'id="market-refresh-countdown"' in response.text
    assert 'id="market-search-input"' in response.text
    assert 'id="market-search-field"' in response.text
    assert 'id="market-sort-field"' in response.text
    assert 'id="market-sort-direction"' in response.text
    assert 'class="market-hero-status"' not in response.text
    assert 'class="market-toolbar"' not in response.text
    assert "Content-Security-Policy" in response.headers
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store, max-age=0"


def test_marketguard_hub_bazaar_renders_bazaar_page() -> None:
    app = create_marketguard_hub_app(_settings())

    with TestClient(app) as client:
        response = client.get("/bazaar")

    assert response.status_code == 200
    assert 'data-view="bazaar"' in response.text
    assert 'data-api-url="/api/v1/bazaar"' in response.text
    assert 'aria-current="page"' in response.text


def test_marketguard_hub_prefixed_routes_render_without_caddy_strip_prefix() -> None:
    app = create_marketguard_hub_app(_settings())

    with TestClient(app) as client:
        root_response = client.get("/market/")
        bazaar_response = client.get("/market/bazaar")
        asset_response = client.get("/market/assets/css/marketguard-hub.css")

    assert root_response.status_code == 200
    assert 'data-view="lowestbin"' in root_response.text
    assert bazaar_response.status_code == 200
    assert 'data-view="bazaar"' in bazaar_response.text
    assert asset_response.status_code == 200
    assert "--market-radius: 4px;" in asset_response.text


def test_marketguard_hub_serves_assets_and_internal_health() -> None:
    app = create_marketguard_hub_app(_settings())

    with TestClient(app) as client:
        css_response = client.get("/assets/css/marketguard-hub.css")
        js_response = client.get("/assets/js/marketguard-hub.js")
        health_response = client.get("/internal/health")

    assert css_response.status_code == 200
    assert "--market-radius: 4px;" in css_response.text
    assert "text-overflow: ellipsis;" in css_response.text
    assert "market-refresh-button" in css_response.text
    assert "market-search-row" in css_response.text
    assert "market-sort-panel" in css_response.text
    assert js_response.status_code == 200
    assert "IntersectionObserver" in js_response.text
    assert "formatCompactNumber" in js_response.text
    assert "market-refresh-countdown" in js_response.text
    assert "applyFiltersAndSort" in js_response.text
    assert "spreadPercentage" in js_response.text
    assert "/api/player-names" in js_response.text
    assert health_response.status_code == 200
    assert health_response.json()["service"] == "marketguard-hub"


def test_marketguard_hub_player_name_lookup_endpoint_batches_uuid_resolution() -> None:
    resolver = _FakeResolver({"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": "Notch"})
    app = create_marketguard_hub_app(_settings(), profile_resolver=resolver)

    with TestClient(app) as client:
        response = client.post(
            "/api/player-names",
            json={
                "uuids": [
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "invalid",
                    "cccccccccccccccccccccccccccccccc",
                ]
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "playerNames": {
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": "Notch",
        }
    }
    assert resolver.calls == [["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "cccccccccccccccccccccccccccccccc"]]


def _settings() -> MarketGuardHubSettings:
    return MarketGuardHubSettings(
        host="0.0.0.0",
        port=8082,
        public_base_url="https://scamscreener.example.com",
        allowed_hosts=("testserver", "scamscreener.example.com"),
        enforce_https=True,
        base_path="/market",
        refresh_interval_seconds=60,
    )


class _FakeResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls: list[list[str]] = []

    async def resolve_many(self, uuids: Sequence[str]) -> dict[str, str]:
        normalized = [
            str(value).strip().lower()
            for value in uuids
            if len(str(value).strip().replace("-", "")) == 32
            and all(character in "0123456789abcdef" for character in str(value).strip().lower().replace("-", ""))
        ]
        self.calls.append(normalized)
        return {player_uuid: self._mapping[player_uuid] for player_uuid in normalized if player_uuid in self._mapping}

    async def aclose(self) -> None:
        return None
