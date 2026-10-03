"""Unit tests for the Plex Now Playing plugin."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

from plugins.plex import (
    PlexPlugin,
    Plugin,
    ellipsize,
    layout_lines,
    parse_session,
    season_episode,
    session_summary,
    wrap_two_lines,
)
from src.devices import BoardContext
from src.plugins.geometry_conformance import assert_board_conformance
from src.templates.engine import TemplateEngine

MANIFEST = json.loads((Path(__file__).resolve().parent.parent / "manifest.json").read_text())

DECLARED_SIMPLE = set(MANIFEST["variables"]["simple"])
DECLARED_SESSION_FIELDS = set(MANIFEST["variables"]["arrays"]["sessions"]["item_fields"])

NOTE = BoardContext("note", rows=3, cols=15)
FLAGSHIP = BoardContext("flagship", rows=6, cols=22)
NOTE_ARRAY_2_WIDE = BoardContext("note_array", rows=3, cols=30)
NOTE_ARRAY_1_TALL = BoardContext("note_array", rows=12, cols=15)

Y2 = "{65}{65}"


def movie_item(**overrides):
    item = {
        "type": "movie",
        "title": "Interstellar",
        "year": 2014,
        "duration": 10140000,
        "viewOffset": 2535000,
        "User": {"title": "Alex"},
        "Player": {"title": "Living Room TV", "product": "Plex for Roku", "state": "playing"},
    }
    item.update(overrides)
    return item


def episode_item(**overrides):
    item = {
        "type": "episode",
        "grandparentTitle": "Breaking Bad",
        "parentIndex": 5,
        "index": 14,
        "title": "Ozymandias",
        "year": 2013,
        "duration": 2820000,
        "viewOffset": 300000,
        "User": {"title": "Alex"},
        "Player": {"title": "Living Room TV", "state": "playing"},
    }
    item.update(overrides)
    return item


def track_item(**overrides):
    item = {
        "type": "track",
        "title": "One More Time",
        "grandparentTitle": "Daft Punk",
        "parentTitle": "Discovery",
        "year": 2001,
        "duration": 320000,
        "viewOffset": 60000,
        "User": {"title": "Sam"},
        "Player": {"title": "Kitchen", "state": "paused"},
    }
    item.update(overrides)
    return item


def sessions_response(*items, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"MediaContainer": {"size": len(items), "Metadata": list(items)}}
    response.raise_for_status.return_value = None
    return response


def make_plugin(**config):
    plugin = PlexPlugin(manifest=MANIFEST)
    plugin.config = {"server_url": "http://192.168.1.100:32400", "token": "test_token", **config}
    return plugin


def fetch(plugin, *items, board=None):
    """Run a board-scoped fetch against a mocked /status/sessions response."""
    with patch("plugins.plex.requests.get", return_value=sessions_response(*items)) as get:
        result = plugin.get_data(board)
    return result, get


class TestPluginBasics:
    def test_plugin_id_matches_manifest(self):
        assert make_plugin().plugin_id == MANIFEST["id"] == "plex"

    def test_plugin_export(self):
        assert Plugin is PlexPlugin


class TestValidateConfig:
    def test_valid_config(self):
        assert make_plugin().validate_config({"server_url": "http://plex.local:32400", "token": "t"}) == []

    def test_url_and_token_optional(self, monkeypatch):
        """Sign in with Plex supplies the token, and plex.tv finds the server."""
        monkeypatch.delenv("PLEX_URL", raising=False)
        monkeypatch.delenv("PLEX_TOKEN", raising=False)
        assert make_plugin().validate_config({}) == []

    def test_url_without_scheme(self):
        errors = make_plugin().validate_config({"server_url": "192.168.1.100:32400", "token": "t"})
        assert errors == ["Plex server URL must start with http:// or https://"]

    def test_env_vars_satisfy_required_settings(self, monkeypatch):
        monkeypatch.setenv("PLEX_URL", "http://plex.local:32400")
        monkeypatch.setenv("PLEX_TOKEN", "env_token")
        assert make_plugin().validate_config({}) == []

    def test_refresh_below_minimum(self):
        errors = make_plugin().validate_config(
            {"server_url": "http://plex.local:32400", "token": "t", "refresh_seconds": 5}
        )
        assert errors == ["Refresh interval must be at least 10 seconds"]


class TestHelpers:
    def test_ellipsize_leaves_fitting_text(self):
        assert ellipsize("Ozymandias", 15) == "Ozymandias"

    def test_ellipsize_marks_cut_text(self):
        assert ellipsize("The Rains of Castamere", 15) == "The Rains of..."

    def test_ellipsize_cuts_at_word_boundary(self):
        assert ellipsize("Everywhere All at Once", 15) == "Everywhere..."

    def test_ellipsize_cuts_single_long_word(self):
        assert ellipsize("Supercalifragilistic", 15) == "Supercalifra..."

    def test_wrap_two_lines_single_line(self):
        assert wrap_two_lines("Interstellar", 15) == ("Interstellar", "")

    def test_wrap_two_lines_breaks_at_word(self):
        assert wrap_two_lines("The Shawshank Redemption", 15) == ("The Shawshank", "Redemption")

    def test_wrap_two_lines_ellipsizes_third_line(self):
        assert wrap_two_lines("Everything Everywhere All at Once", 15) == ("Everything", "Everywhere...")

    def test_wrap_two_lines_empty(self):
        assert wrap_two_lines("", 15) == ("", "")

    def test_season_episode(self):
        assert season_episode(5, 14) == "S05 E14"

    def test_season_episode_missing_parts(self):
        assert season_episode(None, 3) == "E03"
        assert season_episode(2, None) == "S02"
        assert season_episode(None, None) == ""


class TestParseSession:
    def test_movie(self):
        session = parse_session(movie_item())
        assert session["media_type"] == "Movie"
        assert session["title"] == "Interstellar"
        assert session["year"] == 2014
        assert session["show"] == ""
        assert session["user"] == "Alex"
        assert session["player"] == "Living Room TV"
        assert session["state"] == "Playing"

    def test_progress_and_minutes_left(self):
        session = parse_session(movie_item(duration=10140000, viewOffset=2535000))
        assert session["progress_percent"] == 25
        assert session["minutes_left"] == 127  # 7,605,000 ms remaining, rounded up

    def test_missing_duration(self):
        session = parse_session(movie_item(duration=None, viewOffset=None))
        assert session["progress_percent"] == 0
        assert session["minutes_left"] == 0

    def test_episode(self):
        session = parse_session(episode_item())
        assert session["media_type"] == "Episode"
        assert session["show"] == "Breaking Bad"
        assert session["season_episode"] == "S05 E14"
        assert session["title"] == "Ozymandias"

    def test_track_uses_album_artist(self):
        session = parse_session(track_item())
        assert session["media_type"] == "Track"
        assert session["artist"] == "Daft Punk"
        assert session["album"] == "Discovery"
        assert session["state"] == "Paused"

    def test_track_prefers_track_artist(self):
        session = parse_session(track_item(originalTitle="Romanthony"))
        assert session["artist"] == "Romanthony"

    def test_unknown_type_and_state(self):
        session = parse_session({"type": "livetv", "title": "News", "Player": {"state": "stopped"}})
        assert session["media_type"] == "Livetv"
        assert session["state"] == "Playing"
        assert session["year"] == ""

    def test_player_falls_back_to_product(self):
        session = parse_session(movie_item(Player={"product": "Plex Web", "state": "buffering"}))
        assert session["player"] == "Plex Web"
        assert session["state"] == "Buffering"

    def test_long_values_are_not_truncated(self):
        title = "Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb"
        assert parse_session(movie_item(title=title))["title"] == title


class TestLayoutLines:
    """The smart display rules, applied at whatever width the board has."""

    def test_idle(self):
        assert layout_lines(None, 15, True) == ["", "Nothing playing", ""]

    def test_movie_short_title(self):
        assert layout_lines(parse_session(movie_item()), 15, True) == [
            "Interstellar",
            "",
            f"{Y2} 2014 {Y2}",
        ]

    def test_movie_title_wraps_to_two_lines(self):
        session = parse_session(movie_item(title="The Shawshank Redemption", year=1994))
        assert layout_lines(session, 15, True) == ["The Shawshank", "Redemption", f"{Y2} 1994 {Y2}"]

    def test_movie_title_fits_on_wider_board(self):
        session = parse_session(movie_item(title="The Shawshank Redemption", year=1994))
        assert layout_lines(session, 30, True) == ["The Shawshank Redemption", "", f"{Y2} 1994 {Y2}"]

    def test_movie_without_year(self):
        session = parse_session(movie_item(year=None))
        assert layout_lines(session, 15, True) == ["Interstellar", "", ""]

    def test_episode_short_show(self):
        assert layout_lines(parse_session(episode_item()), 15, True) == [
            "Breaking Bad",
            f"{Y2} S05 E14 {Y2}",
            "Ozymandias",
        ]

    def test_episode_short_show_long_episode_title_is_ellipsized(self):
        session = parse_session(episode_item(grandparentTitle="Game of Thrones", title="The Rains of Castamere"))
        assert layout_lines(session, 15, True) == [
            "Game of Thrones",
            f"{Y2} S05 E14 {Y2}",
            "The Rains of...",
        ]

    def test_episode_long_show_wraps_and_moves_season_episode_down(self):
        session = parse_session(episode_item(grandparentTitle="The Marvelous Mrs. Maisel", parentIndex=1, index=1))
        assert layout_lines(session, 15, True) == ["The Marvelous", "Mrs. Maisel", f"{Y2} S01 E01 {Y2}"]

    def test_episode_long_show_fits_on_note_array(self):
        session = parse_session(
            episode_item(grandparentTitle="The Marvelous Mrs. Maisel", parentIndex=1, index=1, title="Pilot")
        )
        assert layout_lines(session, 30, True) == ["The Marvelous Mrs. Maisel", f"{Y2} S01 E01 {Y2}", "Pilot"]

    def test_track(self):
        assert layout_lines(parse_session(track_item()), 15, True) == ["One More Time", "Daft Punk", "Discovery"]

    def test_track_long_title(self):
        session = parse_session(track_item(title="Harder Better Faster Stronger"))
        assert layout_lines(session, 15, True) == ["Harder Better", "Faster Stronger", "Daft Punk"]

    def test_other_media_type(self):
        session = parse_session({"type": "clip", "title": "Behind the Scenes"})
        assert layout_lines(session, 15, True) == ["Behind the", "Scenes", ""]

    def test_accents_off(self):
        assert layout_lines(parse_session(episode_item()), 15, False)[1] == "S05 E14"

    def test_accents_dropped_when_they_do_not_fit(self):
        assert layout_lines(parse_session(episode_item()), 12, True)[1] == "S05 E14"

    def test_track_long_artist_is_ellipsized(self):
        # Regression: the artist ("middle") used to be returned verbatim while
        # the album ("bottom") was correctly ellipsized -- an artist longer
        # than the board produced a row wider than the board.
        session = parse_session(track_item(originalTitle="The Alan Parsons Project Presents"))
        middle = layout_lines(session, 15, True)[1]
        assert middle == ellipsize("The Alan Parsons Project Presents", 15)
        assert len(middle) <= 15

    def test_rows_beyond_three_are_ignored_by_default(self):
        """The 3-arg call sites throughout this file keep working unchanged."""
        assert len(layout_lines(parse_session(episode_item()), 15, True)) == 3

    def test_extra_rows_list_other_sessions_then_playback_detail(self):
        primary = parse_session(episode_item())
        others = [parse_session(movie_item(title="Dune", User={"title": "Sam"}))]
        lines = layout_lines(primary, 15, True, rows=6, others=others)
        assert lines == [
            "Breaking Bad",
            f"{Y2} S05 E14 {Y2}",
            "Ozymandias",
            "Dune (Sam)",
            "Living Room TV",
            "42m left",
        ]

    def test_extra_rows_never_exceed_the_board(self):
        primary = parse_session(episode_item())
        others = [parse_session(movie_item(title=f"Movie {i}")) for i in range(10)]
        lines = layout_lines(primary, 15, True, rows=5, others=others)
        assert len(lines) == 5

    def test_no_spare_rows_returns_exactly_three_lines(self):
        primary = parse_session(episode_item())
        others = [parse_session(movie_item())]
        lines = layout_lines(primary, 15, True, rows=3, others=others)
        assert len(lines) == 3

    def test_session_summary_movie(self):
        session = parse_session(movie_item(title="Dune", User={"title": "Sam"}))
        assert session_summary(session) == "Dune (Sam)"

    def test_session_summary_episode_uses_show(self):
        session = parse_session(episode_item(User={"title": "Alex"}))
        assert session_summary(session) == "Breaking Bad (Alex)"

    def test_session_summary_track_uses_artist_and_title(self):
        session = parse_session(track_item(User={"title": "Sam"}))
        assert session_summary(session) == "Daft Punk - One More Time (Sam)"

    def test_session_summary_without_user_falls_back_to_player(self):
        session = parse_session(movie_item(title="Dune", User={}, Player={"title": "Kitchen"}))
        assert session_summary(session) == "Dune (Kitchen)"

    def test_idle_ignores_extra_rows(self):
        # No session to feature means nothing more to say, regardless of rows.
        assert layout_lines(None, 15, True, rows=12, others=[parse_session(movie_item())]) == [
            "",
            "Nothing playing",
            "",
        ]


class TestFetchData:
    def test_missing_config(self, monkeypatch):
        monkeypatch.delenv("PLEX_URL", raising=False)
        monkeypatch.delenv("PLEX_TOKEN", raising=False)
        plugin = PlexPlugin(manifest=MANIFEST)
        plugin.config = {}
        plugin.get_oauth_token = lambda: None
        result = plugin.fetch_data()
        assert result.available is False
        assert result.error == "Sign in with Plex, or paste a Plex token, in the plugin settings"

    def test_request(self):
        plugin = make_plugin(server_url="http://192.168.1.100:32400/")
        _, get = fetch(plugin)
        args, kwargs = get.call_args
        assert args[0] == "http://192.168.1.100:32400/status/sessions"
        assert kwargs["headers"]["X-Plex-Token"] == "test_token"
        assert kwargs["headers"]["Accept"] == "application/json"
        assert kwargs["timeout"] == 10

    def test_server_request_headers_unchanged(self):
        """A pasted token plus server URL sends exactly what 1.0.x sent (no per-restart client id the server would list as a new device)."""
        _, get = fetch(make_plugin())
        assert get.call_args.kwargs["headers"] == {
            "X-Plex-Token": "test_token",
            "X-Plex-Product": "FiestaBoard",
            "Accept": "application/json",
        }

    def test_env_vars_used_when_settings_empty(self, monkeypatch):
        monkeypatch.setenv("PLEX_URL", "http://plex.local:32400")
        monkeypatch.setenv("PLEX_TOKEN", "env_token")
        plugin = PlexPlugin(manifest=MANIFEST)
        plugin.config = {"enabled": True}
        _, get = fetch(plugin)
        assert get.call_args.args[0] == "http://plex.local:32400/status/sessions"
        assert get.call_args.kwargs["headers"]["X-Plex-Token"] == "env_token"

    def test_nothing_playing(self):
        result, _ = fetch(make_plugin())
        assert result.available is True
        assert result.data["state"] == "Idle"
        assert result.data["is_playing"] is False
        assert result.data["stream_count"] == 0
        assert result.data["sessions"] == []
        assert result.data["line_2"] == "Nothing playing"

    def test_empty_media_container(self):
        response = sessions_response()
        response.json.return_value = {"MediaContainer": {"size": 0}}
        with patch("plugins.plex.requests.get", return_value=response):
            result = make_plugin().fetch_data()
        assert result.available is True
        assert result.data["stream_count"] == 0

    def test_episode_playing(self):
        result, _ = fetch(make_plugin(), episode_item(), board=NOTE)
        data = result.data
        assert result.available is True
        assert data["is_playing"] is True
        assert data["show"] == "Breaking Bad"
        assert data["season_episode"] == "S05 E14"
        assert data["title"] == "Ozymandias"
        assert [data["line_1"], data["line_2"], data["line_3"]] == [
            "Breaking Bad",
            f"{Y2} S05 E14 {Y2}",
            "Ozymandias",
        ]

    def test_paused_is_not_playing(self):
        result, _ = fetch(make_plugin(), track_item())
        assert result.data["state"] == "Paused"
        assert result.data["is_playing"] is False

    def test_playing_stream_is_featured_over_paused(self):
        result, _ = fetch(make_plugin(), track_item(), movie_item())
        assert result.data["title"] == "Interstellar"
        assert result.data["stream_count"] == 2
        assert [s["title"] for s in result.data["sessions"]] == ["One More Time", "Interstellar"]

    def test_user_filter_is_case_insensitive(self):
        result, _ = fetch(make_plugin(plex_user="sam"), movie_item(), track_item())
        assert result.data["title"] == "One More Time"
        assert result.data["stream_count"] == 1

    def test_user_filter_without_match_is_idle(self):
        result, _ = fetch(make_plugin(plex_user="Nobody"), movie_item())
        assert result.available is True
        assert result.data["state"] == "Idle"
        assert result.data["stream_count"] == 0

    def test_show_accents_setting(self):
        result, _ = fetch(make_plugin(show_accents=False), episode_item(), board=NOTE)
        assert result.data["line_2"] == "S05 E14"

    def test_lines_follow_the_board_width(self):
        item = episode_item(grandparentTitle="The Marvelous Mrs. Maisel", parentIndex=1, index=1, title="Pilot")
        plugin = make_plugin()
        on_note, _ = fetch(plugin, item, board=NOTE)
        on_array, _ = fetch(plugin, item, board=NOTE_ARRAY_2_WIDE)
        assert on_note.data["line_1"] == "The Marvelous"
        assert on_array.data["line_1"] == "The Marvelous Mrs. Maisel"

    def test_no_board_uses_flagship_width(self):
        item = movie_item(title="Everything Everywhere All at Once")
        result, _ = fetch(make_plugin(), item)
        assert (result.data["line_1"], result.data["line_2"]) == ("Everything Everywhere", "All at Once")

    def test_formatted_lines_follow_the_board_height(self):
        primary_item = episode_item()
        secondary_item = movie_item(title="Dune", User={"title": "Sam"})
        plugin = make_plugin()
        on_note, _ = fetch(plugin, primary_item, secondary_item, board=NOTE)
        on_tall, _ = fetch(plugin, primary_item, secondary_item, board=NOTE_ARRAY_1_TALL)
        assert len(on_note.formatted_lines) == 3
        assert len(on_tall.formatted_lines) > 3
        assert "Dune (Sam)" in on_tall.formatted_lines
        assert len(on_tall.formatted_lines) <= NOTE_ARRAY_1_TALL.rows

    def test_no_board_uses_flagship_height(self):
        primary_item = episode_item()
        secondary_item = movie_item(title="Dune", User={"title": "Sam"})
        result, _ = fetch(make_plugin(), primary_item, secondary_item)
        assert len(result.formatted_lines) > 3
        assert len(result.formatted_lines) <= 6  # flagship's 6 rows, the assumed default

    def test_unauthorized(self):
        with patch("plugins.plex.requests.get", return_value=sessions_response(status_code=401)):
            result = make_plugin().fetch_data()
        assert result.available is False
        assert result.error == "Plex rejected the token (401 Unauthorized)"

    def test_http_error(self):
        response = sessions_response(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        with patch("plugins.plex.requests.get", return_value=response):
            result = make_plugin().fetch_data()
        assert result.available is False
        assert "500 Server Error" in result.error

    def test_server_unreachable(self):
        with patch("plugins.plex.requests.get", side_effect=requests.ConnectionError("no route to host")):
            result = make_plugin().fetch_data()
        assert result.available is False
        assert result.error.startswith("Could not reach Plex server")

    def test_invalid_json(self):
        response = sessions_response()
        response.json.side_effect = ValueError("Expecting value")
        with patch("plugins.plex.requests.get", return_value=response):
            result = make_plugin().fetch_data()
        assert result.available is False
        assert result.error == "Expecting value"


class TestManifestContract:
    @pytest.mark.parametrize(
        "items", [(), (movie_item(),), (episode_item(),), (track_item(),)], ids=["idle", "movie", "episode", "track"]
    )
    def test_data_matches_declared_variables(self, items):
        result, _ = fetch(make_plugin(), *items)
        assert set(result.data) - {"sessions"} == DECLARED_SIMPLE
        for session in result.data["sessions"]:
            assert set(session) == DECLARED_SESSION_FIELDS


def render(template_entry, data, board):
    """Render a demo template through core's engine, with the manifest's colour rules."""
    registry = Mock(get_manifest=Mock(return_value=SimpleNamespace(color_rules_schema=MANIFEST["color_rules_schema"])))
    with patch("src.templates.engine.get_plugin_registry", return_value=registry):
        engine = TemplateEngine()
    engine._config_manager = Mock(get_color_rules=Mock(return_value=None))
    notes_wide = board.cols // 15 if board.device_type == "note_array" else 1
    notes_tall = board.rows // 3 if board.device_type == "note_array" else 1
    text = engine.render_lines(
        template_entry["template"],
        context={"plex": data},
        line_metadata=template_entry["line_metadata"],
        device_type=board.device_type,
        notes_wide=notes_wide,
        notes_tall=notes_tall,
    )
    return [line.rstrip() for line in text.split("\n")]


def preview(device_type, label=None):
    """The literal rows of one manifest preview.

    Two ``note_array`` entries exist (wide and tall), so ``label`` picks
    between them; it is ignored for the single-entry device types.
    """
    match = next(
        p
        for p in MANIFEST["previews"]
        if p["device_type"] == device_type and (label is None or p.get("label") == label)
    )
    return [row.upper() for row in match["rows"]]


class TestRenderedBoards:
    """The manifest previews are what the demo templates really render."""

    def test_flagship_demo_matches_preview(self):
        result, _ = fetch(make_plugin(), episode_item(), board=FLAGSHIP)
        rendered = render(MANIFEST["demo"]["flagship"], result.data, FLAGSHIP)
        assert [line.upper() for line in rendered] == preview("flagship")

    def test_note_demo_matches_preview(self):
        result, _ = fetch(make_plugin(), episode_item(), board=NOTE)
        rendered = render(MANIFEST["demo"]["note"], result.data, NOTE)
        assert [line.upper() for line in rendered] == preview("note")

    def test_note_template_on_note_array_matches_preview(self):
        item = episode_item(grandparentTitle="The Marvelous Mrs. Maisel", parentIndex=1, index=1, title="Pilot")
        result, _ = fetch(make_plugin(), item, board=NOTE_ARRAY_2_WIDE)
        rendered = render(MANIFEST["demo"]["note"], result.data, NOTE_ARRAY_2_WIDE)
        assert [line.upper() for line in rendered] == preview("note_array", label="Note Array (2 wide)")

    def test_note_template_on_tall_note_array_matches_preview(self):
        # Same width as a Note (15 cols) but four notes tall (12 rows) --
        # the "narrower than a Flagship, much taller" shape from the audit.
        # The fixed-length "note" demo template only ever fills the first
        # three rows; rows never longer than the board is proven separately
        # by the plugin's own formatted_lines (see TestBoardConformance).
        result, _ = fetch(make_plugin(), episode_item(), board=NOTE_ARRAY_1_TALL)
        rendered = [line.upper() for line in render(MANIFEST["demo"]["note"], result.data, NOTE_ARRAY_1_TALL)]
        expected = preview("note_array", label="Note Array (tall)")
        assert rendered[: len(expected)] == expected
        assert all(line == "" for line in rendered[len(expected) :])


class TestBoardConformance:
    """Shared conformance suite: the plugin must render on every board shape.

    ``strict_growth=True`` because this plugin renders a session list --
    other active streams fill the rows a shorter board has no room for, so a
    taller board that leaves a full shorter board's content unrepeated must
    show strictly more.
    """

    def test_renders_on_every_board_shape(self, monkeypatch):
        items = [
            episode_item(grandparentTitle="The Marvelous Mrs. Maisel", parentIndex=1, index=1, title="Pilot"),
            movie_item(title="Dune", User={"title": "Sam"}, Player={"title": "Home Theater", "state": "paused"}),
            track_item(title="Harder Better Faster Stronger", User={"title": "Jo"}),
        ]
        monkeypatch.setattr("plugins.plex.requests.get", lambda *args, **kwargs: sessions_response(*items))

        assert_board_conformance(
            make_plugin,
            manifest=MANIFEST,
            strict_growth=True,
            require_note_array_preview=True,
        )
