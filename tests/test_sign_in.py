"""Sign in with Plex (core's plex_pin flow) and server discovery through plex.tv."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from plugins.plex import PLEX_RESOURCES_URL, PlexPlugin
from src.oauth.provider import parse_provider_block, validate_provider_block

MANIFEST = json.loads((Path(__file__).resolve().parent.parent / "manifest.json").read_text())

LAN = "https://192-168-1-5.abc.plex.direct:32400"
REMOTE = "https://203-0-113-9.abc.plex.direct:32400"
RELAY = "https://10-0-0-1.abc.plex.direct:8443"


def response(status_code=200, body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body if body is not None else {"MediaContainer": {"size": 0}}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code} Error")
    else:
        resp.raise_for_status.return_value = None
    return resp


def server(name="Home", owned=True, access_token="test_server_token", connections=None):
    return {
        "name": name,
        "provides": "server",
        "owned": owned,
        "presence": True,
        "accessToken": access_token,
        "connections": connections
        if connections is not None
        else [
            {"uri": RELAY, "local": False, "relay": True},
            {"uri": REMOTE, "local": False, "relay": False},
            {"uri": LAN, "local": True, "relay": False},
        ],
    }


class Router:
    """Answers requests.get by URL; records every call."""

    def __init__(self, resources=None, resources_status=200, reachable=(LAN, REMOTE, RELAY), sessions_status=200):
        self.resources = resources if resources is not None else [server()]
        self.resources_status = resources_status
        self.reachable = set(reachable)
        self.sessions_status = sessions_status
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == PLEX_RESOURCES_URL:
            return response(self.resources_status, self.resources)
        base = url.rsplit("/", 2)[0] if url.endswith("/status/sessions") else url.rsplit("/", 1)[0]
        if base not in self.reachable:
            raise requests.ConnectionError("unreachable")
        if url.endswith("/status/sessions"):
            return response(self.sessions_status)
        return response(200, {})

    def urls(self):
        return [url for url, _ in self.calls]


def make_plugin(signed_in_token="test_signed_in_token", **config):
    plugin = PlexPlugin(manifest=MANIFEST)
    plugin.config = config
    plugin.get_oauth_token = MagicMock(return_value=signed_in_token)
    plugin.report_oauth_rejected = MagicMock(return_value=None)
    return plugin


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("PLEX_URL", raising=False)
    monkeypatch.delenv("PLEX_TOKEN", raising=False)


def run(plugin, router):
    with patch("plugins.plex.requests.get", side_effect=router):
        return plugin.fetch_data()


class TestManifest:
    def test_oauth_block_is_plex_pin(self):
        assert validate_provider_block(MANIFEST["oauth"]) == []
        provider = parse_provider_block(MANIFEST["oauth"], MANIFEST["name"])
        assert provider.flows == ("plex_pin",)
        assert provider.plex_product == "FiestaBoard"

    def test_requires_core_with_plex_pin(self):
        assert MANIFEST["fiestaboard_version"] == ">=9.11.0"

    def test_existing_settings_keys_kept_and_optional(self):
        props = MANIFEST["settings_schema"]["properties"]
        for key in ("server_url", "token", "plex_user", "show_accents", "refresh_seconds"):
            assert key in props
        assert "token" not in MANIFEST["settings_schema"].get("required", [])
        assert "server_url" not in MANIFEST["settings_schema"].get("required", [])

    def test_sign_in_is_not_a_breaking_release(self):
        """Sign in with Plex shipped in 1.1.0 without breaking 1.0.x settings."""
        major, minor, _ = (int(part) for part in MANIFEST["version"].split("."))
        assert (major, minor) >= (1, 1) and major == 1


class TestTokenChoice:
    def test_pasted_token_wins_over_sign_in(self):
        plugin = make_plugin(server_url="http://plex.local:32400", token="test_pasted")
        router = Router(reachable=("http://plex.local:32400",))
        result = run(plugin, router)
        assert result.available is True
        assert router.calls[-1][1]["headers"]["X-Plex-Token"] == "test_pasted"
        plugin.get_oauth_token.assert_not_called()

    def test_env_token_wins_over_sign_in(self, monkeypatch):
        monkeypatch.setenv("PLEX_TOKEN", "test_env")
        plugin = make_plugin(server_url="http://plex.local:32400")
        router = Router(reachable=("http://plex.local:32400",))
        run(plugin, router)
        assert router.calls[-1][1]["headers"]["X-Plex-Token"] == "test_env"

    def test_signed_in_token_used_with_server_url(self):
        plugin = make_plugin(server_url="http://plex.local:32400")
        router = Router(reachable=("http://plex.local:32400",))
        result = run(plugin, router)
        assert result.available is True
        assert router.urls() == ["http://plex.local:32400/status/sessions"]
        assert router.calls[0][1]["headers"]["X-Plex-Token"] == "test_signed_in_token"

    def test_neither_token_asks_to_sign_in(self):
        plugin = make_plugin(signed_in_token=None, server_url="http://plex.local:32400")
        result = run(plugin, Router())
        assert result.available is False
        assert "Sign in with Plex" in result.error

    def test_old_core_without_sign_in(self):
        """A core without get_oauth_token still works with a pasted token, and explains otherwise."""
        plugin = PlexPlugin(manifest=MANIFEST)
        plugin.config = {}
        with patch.object(PlexPlugin, "get_oauth_token", None, create=True):
            result = plugin.fetch_data()
        assert result.available is False
        assert "Plex token" in result.error

    def test_sign_in_lookup_failure_is_not_fatal(self):
        plugin = make_plugin(server_url="http://plex.local:32400")
        plugin.get_oauth_token.side_effect = RuntimeError("oauth store unreadable")
        result = run(plugin, Router())
        assert result.available is False
        assert "Sign in with Plex" in result.error


class TestDiscovery:
    def test_finds_server_and_prefers_local_connection(self):
        plugin = make_plugin()
        router = Router()
        result = run(plugin, router)
        assert result.available is True
        url, kwargs = router.calls[0]
        assert url == PLEX_RESOURCES_URL
        assert kwargs["headers"]["X-Plex-Token"] == "test_signed_in_token"
        assert kwargs["headers"]["X-Plex-Client-Identifier"]
        assert kwargs["headers"]["Accept"] == "application/json"
        assert router.urls()[1:] == [f"{LAN}/identity", f"{LAN}/status/sessions"]
        assert router.calls[-1][1]["headers"]["X-Plex-Token"] == "test_server_token"

    def test_falls_back_to_remote_then_relay(self):
        router = Router(reachable=(RELAY,))
        result = run(make_plugin(), router)
        assert result.available is True
        assert router.urls()[1:] == [f"{LAN}/identity", f"{REMOTE}/identity", f"{RELAY}/identity", f"{RELAY}/status/sessions"]

    def test_discovery_is_cached(self):
        plugin = make_plugin()
        router = Router()
        run(plugin, router)
        run(plugin, router)
        assert router.urls().count(PLEX_RESOURCES_URL) == 1

    def test_cache_dropped_when_server_unreachable(self):
        plugin = make_plugin()
        router = Router()
        run(plugin, router)
        router.reachable = {REMOTE}
        result = run(plugin, router)
        assert result.available is False
        assert result.error.startswith("Could not reach Plex server")
        run(plugin, router)
        assert router.urls().count(PLEX_RESOURCES_URL) == 2
        assert router.urls()[-1] == f"{REMOTE}/status/sessions"

    def test_works_with_pasted_token_too(self):
        plugin = make_plugin(signed_in_token=None, token="test_pasted")
        router = Router()
        assert run(plugin, router).available is True
        assert router.calls[0][1]["headers"]["X-Plex-Token"] == "test_pasted"

    def test_owned_server_preferred(self):
        friend = server(name="Friend", owned=False, access_token="test_friend",
                        connections=[{"uri": REMOTE, "local": False, "relay": False}])
        router = Router(resources=[friend, server()])
        run(make_plugin(), router)
        assert router.urls()[1] == f"{LAN}/identity"

    def test_server_name_picks_server(self):
        friend = server(name="Friend", owned=False, access_token="test_friend",
                        connections=[{"uri": REMOTE, "local": False, "relay": False}])
        router = Router(resources=[server(), friend])
        result = run(make_plugin(server_name="friend"), router)
        assert result.available is True
        assert router.calls[-1][0] == f"{REMOTE}/status/sessions"
        assert router.calls[-1][1]["headers"]["X-Plex-Token"] == "test_friend"

    def test_unknown_server_name(self):
        result = run(make_plugin(server_name="Cabin"), Router())
        assert result.available is False
        assert "Cabin" in result.error

    def test_ignores_non_servers_and_missing_access_token(self):
        player = {"name": "Phone", "provides": "client,player", "connections": [{"uri": REMOTE}]}
        router = Router(resources=[player, server(access_token=None)])
        run(make_plugin(), router)
        assert router.calls[-1][1]["headers"]["X-Plex-Token"] == "test_signed_in_token"

    def test_no_server_on_account(self):
        result = run(make_plugin(), Router(resources=[]))
        assert result.available is False
        assert "Plex Server URL" in result.error

    def test_server_not_reachable_from_board(self):
        result = run(make_plugin(), Router(reachable=()))
        assert result.available is False
        assert "Plex Server URL" in result.error

    def test_plex_tv_unreachable(self):
        with patch("plugins.plex.requests.get", side_effect=requests.ConnectionError("dns")):
            result = make_plugin().fetch_data()
        assert result.available is False
        assert "plex.tv" in result.error

    def test_plex_tv_bad_answer(self):
        result = run(make_plugin(), Router(resources={"error": "x"}))
        assert result.available is False
        assert "plex.tv" in result.error

    def test_plex_tv_server_error(self):
        result = run(make_plugin(), Router(resources_status=503))
        assert result.available is False
        assert "plex.tv" in result.error


class TestRejection:
    def test_plex_tv_rejects_signed_in_token(self):
        plugin = make_plugin()
        result = run(plugin, Router(resources_status=401))
        assert result.available is False
        assert "Sign in with Plex again" in result.error
        plugin.report_oauth_rejected.assert_called_once()

    def test_plex_tv_rejects_pasted_token(self):
        plugin = make_plugin(signed_in_token=None, token="test_pasted")
        result = run(plugin, Router(resources_status=401))
        assert result.error == "Plex rejected the token (401 Unauthorized)"
        plugin.report_oauth_rejected.assert_not_called()

    def test_server_rejects_signed_in_token(self):
        plugin = make_plugin(server_url="http://plex.local:32400")
        result = run(plugin, Router(reachable=("http://plex.local:32400",), sessions_status=401))
        assert "Sign in with Plex again" in result.error
        plugin.report_oauth_rejected.assert_called_once()

    def test_discovered_server_rejects_shared_token_drops_cache(self):
        plugin = make_plugin()
        router = Router()
        run(plugin, router)
        router.sessions_status = 401
        result = run(plugin, router)
        assert result.error == "Plex rejected the token (401 Unauthorized)"
        plugin.report_oauth_rejected.assert_not_called()
        router.sessions_status = 200
        run(plugin, router)
        assert router.urls().count(PLEX_RESOURCES_URL) == 2

    def test_old_core_without_report(self):
        plugin = make_plugin(server_url="http://plex.local:32400")
        plugin.report_oauth_rejected = None
        result = run(plugin, Router(reachable=("http://plex.local:32400",), sessions_status=401))
        assert "Sign in with Plex again" in result.error


class TestValidateConfig:
    def test_nothing_required_now(self):
        assert make_plugin().validate_config({}) == []

    def test_bad_url_still_rejected(self):
        assert make_plugin().validate_config({"server_url": "plex.local"}) == [
            "Plex server URL must start with http:// or https://"
        ]
