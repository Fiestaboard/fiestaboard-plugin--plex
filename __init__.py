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
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

DEFAULT_COLS = 22
DEFAULT_ROWS = 6
ACCENT = "{65}{65}"  # two yellow tiles, Plex's brand colour
REQUEST_TIMEOUT = 10

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


class PlexPlugin(PluginBase):
    """Plex Now Playing plugin."""

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
        errors = []

        server_url = self._server_url(config)
        if not server_url:
            errors.append("Plex server URL is required")
        elif not server_url.startswith(("http://", "https://")):
            errors.append("Plex server URL must start with http:// or https://")

        if not self._token(config):
            errors.append("Plex token is required")

        errors.extend(self._validate_refresh_seconds(config))
        return errors

    def _fetch_sessions(self, server_url: str, token: str) -> List[Dict[str, Any]]:
        response = requests.get(
            f"{server_url}/status/sessions",
            headers={
                "X-Plex-Token": token,
                "X-Plex-Product": "FiestaBoard",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 401:
            raise PermissionError("Plex rejected the token (401 Unauthorized)")
        response.raise_for_status()
        container = response.json().get("MediaContainer") or {}
        return container.get("Metadata") or []

    def fetch_data(self) -> PluginResult:
        """Fetch the active Plex sessions and build the template variables."""
        server_url = self._server_url(self.config)
        token = self._token(self.config)
        if not server_url or not token:
            return PluginResult(available=False, error="Plex server URL or token not configured")

        try:
            items = self._fetch_sessions(server_url, token)
        except requests.exceptions.RequestException as e:
            logger.warning("Could not reach Plex at %s: %s", server_url, e)
            return PluginResult(available=False, error=f"Could not reach Plex server: {e}")
        except (PermissionError, ValueError) as e:
            logger.warning("Plex request failed: %s", e)
            return PluginResult(available=False, error=str(e))

        sessions = [parse_session(item) for item in items]

        plex_user = (self.config.get("plex_user") or "").strip().lower()
        if plex_user:
            sessions = [s for s in sessions if s["user"].lower() == plex_user]

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
