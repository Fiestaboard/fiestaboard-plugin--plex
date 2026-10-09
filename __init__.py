"""Plex Now Playing plugin for FiestaBoard.

Polls a Plex Media Server's active sessions and exposes what is playing —
movies, TV episodes and music — as template variables, plus a ready-made
board-shaped rendering: three lines for the featured session, and, on
boards with rows to spare, more of the active session list and playback
detail.
"""

import logging
import math
import os
import textwrap
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

DEFAULT_COLS = 22
DEFAULT_ROWS = 6
ACCENT = "{65}{65}"  # two yellow tiles, Plex's brand colour
REQUEST_TIMEOUT = 10
PROBE_TIMEOUT = 3
# Plex drops a session the moment an episode ends and only starts the next one after its
# Up Next countdown; holding the last stream this long keeps the board from blanking out.
DEFAULT_HOLD_SECONDS = 60
MAX_HOLD_SECONDS = 600
PLEX_RESOURCES_URL = "https://plex.tv/api/v2/resources"

NEEDS_TOKEN = "Sign in with Plex, or paste a Plex token, in the plugin settings"
SIGN_IN_REJECTED = "Plex stopped accepting the sign-in. Sign in with Plex again."
TOKEN_REJECTED = "Plex rejected the token (401 Unauthorized)"
SET_SERVER_URL = "set the Plex Server URL in the plugin settings"
BAD_HOLD = f"Hold must be between 0 and {MAX_HOLD_SECONDS} seconds"

STATE_LABELS = {
    "playing": "Playing",
    "buffering": "Buffering",
    "paused": "Paused",
}
# Which session becomes the "primary" one when several are active
STATE_PRIORITY = ["Playing", "Buffering", "Paused"]

MEDIA_TYPE_LABELS = {
    "movie": "Movie",
    "episode": "Episode",
    "track": "Track",
    "clip": "Clip",
    "photo": "Photo",
}


def ellipsize(text: str, width: int) -> str:
    """Shorten *text* to *width* characters, ending in '...' when cut.

    Cuts at the last whole word that fits, or mid-word when a single word is too long.
    """
    if len(text) <= width:
        return text
    cut = text[: max(0, width - 3)]
    if text[len(cut)] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip() + "..."


def wrap_two_lines(text: str, width: int) -> Tuple[str, str]:
    """Word-wrap *text* over at most two lines of *width*; the second ends in '...' on overflow."""
    lines = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=False)
    if not lines:
        return "", ""
    if len(lines) == 1:
        return lines[0], ""
    return lines[0], ellipsize(" ".join(lines[1:]), width)


def season_episode(season: Any, episode: Any) -> str:
    """Format as 'S05 E14'; either half is omitted when Plex does not supply it."""
    parts = []
    if season not in (None, ""):
        parts.append(f"S{int(season):02d}")
    if episode not in (None, ""):
        parts.append(f"E{int(episode):02d}")
    return " ".join(parts)


def parse_session(item: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one /status/sessions Metadata entry into template variables."""
    plex_type = item.get("type") or ""
    player = item.get("Player") or {}
    duration = item.get("duration") or 0
    offset = item.get("viewOffset") or 0

    session = {
        "state": STATE_LABELS.get(player.get("state"), "Playing"),
        "media_type": MEDIA_TYPE_LABELS.get(plex_type, plex_type.title()),
        "title": item.get("title") or "",
        "year": item.get("year") or "",
        "show": "",
        "season_episode": "",
        "artist": "",
        "album": "",
        "user": (item.get("User") or {}).get("title") or "",
        "player": player.get("title") or player.get("product") or "",
        "progress_percent": round(offset * 100 / duration) if duration else 0,
        "minutes_left": math.ceil(max(0, duration - offset) / 60000),
    }

    if plex_type == "episode":
        session["show"] = item.get("grandparentTitle") or ""
        session["season_episode"] = season_episode(item.get("parentIndex"), item.get("index"))
    elif plex_type == "track":
        # originalTitle is the track artist when it differs from the album artist
        session["artist"] = item.get("originalTitle") or item.get("grandparentTitle") or ""
        session["album"] = item.get("parentTitle") or ""

    return session


def session_summary(session: Dict[str, Any]) -> str:
    """One-line summary of a *secondary* session, before width fitting."""
    media_type = session["media_type"]
    if media_type == "Episode":
        subject = session["show"] or session["title"]
    elif media_type == "Track":
        subject = f"{session['artist']} - {session['title']}" if session["artist"] else session["title"]
    else:
        subject = session["title"]
    who = session["user"] or session["player"]
    return f"{subject} ({who})" if who else subject


def playback_detail_lines(session: Dict[str, Any]) -> List[str]:
    """Extra detail about the featured *session*, used once other sessions are shown."""
    lines = []
    if session.get("player"):
        lines.append(session["player"])
    if session.get("minutes_left"):
        lines.append(f"{session['minutes_left']}m left")
    return lines


def layout_lines(
    session: Optional[Dict[str, Any]],
    cols: int,
    accents: bool,
    rows: int = 3,
    others: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Display lines for a board *cols* tiles wide and *rows* tiles tall.

    The featured session always gets the first three lines:

    Movies:   title over up to two lines, then the year.
    Episodes: show / season+episode / episode title when the show fits one
              line, otherwise the show over two lines, then season+episode.
    Music:    the same shape as episodes with track / artist / album.

    Rows beyond those three (whenever the board has them) are filled, in
    priority order, with other active sessions and then playback detail for
    the featured one -- never more than *rows* lines are returned, and never
    fewer just because there was nothing left to add.
    """
    if session is None:
        return ["", "Nothing playing", ""]

    def accent(text: str) -> str:
        if accents and text and len(text) + 6 <= cols:
            return f"{ACCENT} {text} {ACCENT}"
        return text

    media_type = session["media_type"]
    if media_type == "Episode":
        top, middle, bottom = session["show"], accent(session["season_episode"]), session["title"]
    elif media_type == "Track":
        top, middle, bottom = session["title"], session["artist"], session["album"]
    else:
        line_1, line_2 = wrap_two_lines(session["title"], cols)
        year = accent(str(session["year"])) if media_type == "Movie" else ""
        primary = [line_1, line_2, year]
        return primary + _extra_lines(session, others, cols, rows - len(primary))

    if len(top) <= cols:
        bottom_line = ellipsize(bottom, cols)
        middle_line = ellipsize(middle, cols) if media_type == "Track" else middle
        primary = [top, middle_line, bottom_line]
    else:
        line_1, line_2 = wrap_two_lines(top, cols)
        middle_line = ellipsize(middle, cols) if media_type == "Track" else middle
        primary = [line_1, line_2, middle_line]
    return primary + _extra_lines(session, others, cols, rows - len(primary))


def _extra_lines(
    session: Dict[str, Any], others: Optional[List[Dict[str, Any]]], cols: int, budget: int
) -> List[str]:
    """Up to *budget* more lines: other active sessions first, then playback detail."""
    if budget <= 0:
        return []
    extra = [ellipsize(session_summary(other), cols) for other in (others or [])[:budget]]
    remaining = budget - len(extra)
    if remaining > 0:
        extra += [ellipsize(text, cols) for text in playback_detail_lines(session)[:remaining]]
    return extra


def pick_primary(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The session to feature: playing beats buffering beats paused, then Plex's order."""
    return min(sessions, key=lambda s: STATE_PRIORITY.index(s["state"]))


def candidate_uris(resource: Dict[str, Any]) -> List[str]:
    """A server's connection addresses, best first: on the home network, then remote, then Plex's relay."""
    connections = [c for c in resource.get("connections") or [] if isinstance(c, dict) and c.get("uri")]
    connections.sort(key=lambda c: (bool(c.get("relay")), not c.get("local")))
    return [c["uri"].rstrip("/") for c in connections]


def pick_servers(resources: Any, server_name: str) -> List[Dict[str, Any]]:
    """The Plex Media Servers in a plex.tv resources answer, owned ones first.

    Raises LookupError when there is none (or none with *server_name*).
    """
    if not isinstance(resources, list):
        raise ValueError("plex.tv gave an unexpected answer")
    servers = [
        r for r in resources if isinstance(r, dict) and "server" in str(r.get("provides") or "").split(",")
    ]
    if server_name:
        servers = [s for s in servers if str(s.get("name") or "").strip().lower() == server_name.lower()]
        if not servers:
            raise LookupError(f"No Plex server named {server_name} on this Plex account")
    if not servers:
        raise LookupError(f"No Plex server found on this Plex account; {SET_SERVER_URL}")
    servers.sort(key=lambda s: (not s.get("owned"), not s.get("presence", True)))
    return servers


class PlexPlugin(PluginBase):
    """Plex Now Playing plugin."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client_identifier = str(uuid.uuid4())
        # ((token, server_name), server_url, server_token) from the last plex.tv lookup
        self._discovered: Optional[Tuple[Tuple[str, str], str, str]] = None
        # The streams last seen, and when Plex first reported none since then
        self._last_sessions: List[Dict[str, Any]] = []
        self._idle_since: Optional[float] = None

    @property
    def plugin_id(self) -> str:
        return "plex"

    @staticmethod
    def _server_url(config: Dict[str, Any]) -> str:
        return (config.get("server_url") or os.getenv("PLEX_URL", "")).strip().rstrip("/")

    @staticmethod
    def _token(config: Dict[str, Any]) -> str:
        return (config.get("token") or os.getenv("PLEX_TOKEN", "")).strip()

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        # Both are optional: Sign in with Plex supplies a token, and plex.tv finds the server.
        errors = []

        server_url = self._server_url(config)
        if server_url and not server_url.startswith(("http://", "https://")):
            errors.append("Plex server URL must start with http:// or https://")

        hold = config.get("hold_seconds", DEFAULT_HOLD_SECONDS)
        if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0 <= hold <= MAX_HOLD_SECONDS:
            errors.append(BAD_HOLD)

        errors.extend(self._validate_refresh_seconds(config))
        return errors

    def _hold_through_gaps(self, sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """*sessions*, or the last streams seen while Plex has reported none for under the hold time."""
        if sessions:
            self._last_sessions, self._idle_since = sessions, None
            return sessions
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        hold = self.config.get("hold_seconds", DEFAULT_HOLD_SECONDS)
        return self._last_sessions if now - self._idle_since < hold else sessions

    def _signed_in_token(self) -> str:
        """The token from Sign in with Plex, or "" (not signed in, or a core without sign-in)."""
        getter = getattr(self, "get_oauth_token", None)
        if not callable(getter):
            return ""
        try:
            return (getter() or "").strip()
        except Exception as e:  # never let the sign-in store break a fetch
            logger.warning("Could not read the Plex sign-in: %s", e)
            return ""

    def _report_rejected(self) -> None:
        report = getattr(self, "report_oauth_rejected", None)
        if callable(report):
            report()

    def _headers(self, token: str) -> Dict[str, str]:
        """Headers for the Plex server: exactly what 1.0.x sent."""
        return {
            "X-Plex-Token": token,
            "X-Plex-Product": "FiestaBoard",
            "Accept": "application/json",
        }

    def _plex_tv_headers(self, token: str) -> Dict[str, str]:
        # plex.tv's v2 API needs a client identifier; the server does not, and would list each new one as a device.
        return {**self._headers(token), "X-Plex-Client-Identifier": self._client_identifier}

    def _reachable(self, uri: str, token: str) -> bool:
        try:
            return requests.get(f"{uri}/identity", headers=self._headers(token), timeout=PROBE_TIMEOUT).status_code == 200
        except requests.exceptions.RequestException:
            return False

    def _discover_server(self, token: str) -> Tuple[str, str]:
        """(server_url, server_token) for the account's server, found through plex.tv and cached."""
        server_name = (self.config.get("server_name") or "").strip()
        key = (token, server_name)
        if self._discovered and self._discovered[0] == key:
            return self._discovered[1], self._discovered[2]

        response = requests.get(
            PLEX_RESOURCES_URL,
            params={"includeHttps": 1, "includeRelay": 1},
            headers=self._plex_tv_headers(token),
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            raise PermissionError(TOKEN_REJECTED)
        response.raise_for_status()
        for resource in pick_servers(response.json(), server_name):
            server_token = resource.get("accessToken") or token
            for uri in candidate_uris(resource):
                if self._reachable(uri, server_token):
                    self._discovered = (key, uri, server_token)
                    return uri, server_token
        raise LookupError(f"Found your Plex server but the board cannot reach it; {SET_SERVER_URL}")

    def _fetch_sessions(self, server_url: str, token: str) -> List[Dict[str, Any]]:
        response = requests.get(
            f"{server_url}/status/sessions",
            headers=self._headers(token),
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            raise PermissionError(TOKEN_REJECTED)
        response.raise_for_status()
        container = response.json().get("MediaContainer") or {}
        return container.get("Metadata") or []

    def _rejected(self, signed_in: bool) -> PluginResult:
        if signed_in:
            self._report_rejected()
            return PluginResult(available=False, error=SIGN_IN_REJECTED)
        return PluginResult(available=False, error=TOKEN_REJECTED)

    def _locate_server(self, token: str, signed_in: bool) -> Tuple[Optional[Tuple[str, str]], Optional[PluginResult]]:
        """The server to ask (from settings, or found through plex.tv), or the result explaining why not."""
        server_url = self._server_url(self.config)
        if server_url:
            return (server_url, token), None
        try:
            return self._discover_server(token), None
        except PermissionError:
            return None, self._rejected(signed_in)
        except LookupError as e:
            return None, PluginResult(available=False, error=str(e))
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.warning("Could not look up the Plex server at plex.tv: %s", e)
            return None, PluginResult(available=False, error=f"Could not find your Plex server through plex.tv: {e}")

    def fetch_data(self) -> PluginResult:
        """Fetch the active Plex sessions and build the template variables."""
        # A pasted token (or PLEX_TOKEN) wins over Sign in with Plex.
        token = self._token(self.config)
        signed_in = False
        if not token:
            token = self._signed_in_token()
            signed_in = bool(token)
        if not token:
            return PluginResult(available=False, error=NEEDS_TOKEN)

        located, failure = self._locate_server(token, signed_in)
        if failure:
            return failure
        server_url, server_token = located

        try:
            items = self._fetch_sessions(server_url, server_token)
        except requests.exceptions.RequestException as e:
            self._discovered = None
            logger.warning("Could not reach Plex at %s: %s", server_url, e)
            return PluginResult(available=False, error=f"Could not reach Plex server: {e}")
        except PermissionError:
            self._discovered = None
            # Only the account token itself proves the sign-in is dead; a server's shared token can be revoked alone.
            return self._rejected(signed_in and server_token == token)
        except ValueError as e:
            logger.warning("Plex request failed: %s", e)
            return PluginResult(available=False, error=str(e))

        # A session with no title is one Plex has not loaded yet: nothing to show
        sessions = [parse_session(item) for item in items if item.get("title")]

        plex_user = (self.config.get("plex_user") or "").strip().lower()
        if plex_user:
            sessions = [s for s in sessions if s["user"].lower() == plex_user]
        sessions = self._hold_through_gaps(sessions)

        primary = pick_primary(sessions) if sessions else None
        others = [s for s in sessions if s is not primary]
        board = getattr(self, "board", None)
        cols = board.cols if board else DEFAULT_COLS
        rows = board.rows if board else DEFAULT_ROWS
        lines = layout_lines(primary, cols, self.config.get("show_accents", True), rows=rows, others=others)

        data: Dict[str, Any] = dict(primary) if primary else {
            "state": "Idle",
            "media_type": "",
            "title": "",
            "year": "",
            "show": "",
            "season_episode": "",
            "artist": "",
            "album": "",
            "user": "",
            "player": "",
            "progress_percent": 0,
            "minutes_left": 0,
        }
        data.update(
            {
                "is_playing": data["state"] == "Playing",
                "stream_count": len(sessions),
                "line_1": lines[0],
                "line_2": lines[1],
                "line_3": lines[2],
                "sessions": sessions,
            }
        )
        return PluginResult(available=True, data=data, formatted_lines=lines)


# Export the plugin class
Plugin = PlexPlugin
