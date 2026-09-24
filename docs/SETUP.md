# Plex Now Playing Setup Guide

Connect FiestaBoard to your Plex Media Server so your board shows what's playing.

## Overview

**What it does:** Shows the movie, TV episode, or song currently streaming from your Plex Media Server, laid out to fit your board. Long titles wrap over two lines, and the layout adapts to Notes, Flagships, and Note Arrays.

**Prerequisites:**

- A Plex Media Server that FiestaBoard can reach on your network
- The server's address (usually `http://<server-ip>:32400`)
- A Plex token (X-Plex-Token) for an account with access to the server

## Quick Setup

1. **Find your Plex token**
   1. Open Plex Web (app.plex.tv) and sign in with an account on your server.
   2. Open any movie or episode in your library.
   3. Click the **⋯** menu and choose **Get Info**, then **View XML**.
   4. The XML opens in a new tab. Copy the value after `X-Plex-Token=` at the end of its address.

   Treat the token like a password: it grants access to your Plex account.

2. **Enable** — In FiestaBoard, go to **Integrations**, find **Plex Now Playing**, and turn it on.

3. **Configure** — Enter your **Plex Server URL** (for example `http://192.168.1.100:32400`) and paste your **Plex Token**. Optionally set a **Plex User** to only show that person's streams.

4. **Template** — Create a page from the plugin's demo, or add the display lines to your own page:

   ```
   {{plex.line_1}}
   {{plex.line_2}}
   {{plex.line_3}}
   ```

   Center-align the lines for the classic look.

5. **View** — Start playing something in Plex. The board updates on the next refresh (30 seconds by default).

## Template Variables

| Variable | Description |
|----------|-------------|
| `{{plex.line_1}}` | First display line: movie title, show, or track |
| `{{plex.line_2}}` | Second display line: title overflow, season/episode, or artist |
| `{{plex.line_3}}` | Third display line: year, episode title, season/episode, or album |
| `{{plex.title}}` | Title of the movie, episode, or track |
| `{{plex.year}}` | Release year |
| `{{plex.show}}` | TV show name |
| `{{plex.season_episode}}` | Season and episode, e.g. `S05 E14` |
| `{{plex.artist}}` | Artist (music) |
| `{{plex.album}}` | Album (music) |
| `{{plex.media_type}}` | `Movie`, `Episode`, `Track`, or `Clip` |
| `{{plex.state}}` | `Playing`, `Paused`, `Buffering`, or `Idle` |
| `{{plex.is_playing}}` | Whether something is playing |
| `{{plex.user}}` | Plex user watching or listening |
| `{{plex.player}}` | Device playing the stream |
| `{{plex.progress_percent}}` | Progress through the item (0-100) |
| `{{plex.minutes_left}}` | Minutes remaining |
| `{{plex.stream_count}}` | Number of active streams |
| `{{plex.sessions.N.title}}` | Any field above for stream `N` (0, 1, 2, …) |

## Configuration Reference

| Setting | Required | Default | Description |
|---------|----------|---------|-------------|
| Plex Server URL | Yes | - | Your server's address, including the port |
| Plex Token | Yes | - | Your X-Plex-Token |
| Plex User | No | - | Only show streams from this Plex user (case-insensitive) |
| Plex yellow accents | No | On | Frame the year or season/episode line with two yellow tiles on each side |
| Refresh Interval | No | 30 seconds | How often to check Plex (10-600 seconds) |

**Environment variables** (alternative to the UI settings):

| Variable | Description |
|----------|-------------|
| `PLEX_URL` | Plex server URL |
| `PLEX_TOKEN` | Plex token |

## Troubleshooting

### "Could not reach Plex server"

- Check the URL includes the port (`:32400`) and the right protocol.
- FiestaBoard must be able to reach the server. If FiestaBoard runs in Docker, use the server's LAN IP, not `localhost`.
- Using `https://` with a plain IP address fails the certificate check. Use `http://` on your local network. If your server's **Secure connections** setting is **Required**, change it to **Preferred**.

### "Plex rejected the token (401 Unauthorized)"

- The token is wrong or has expired. Signing out of all devices in Plex resets tokens, so find it again and paste the new one.
- Make sure the account has access to this server.

### The board says "Nothing playing" while something is playing

- If **Plex User** is set, it must match the name Plex shows for that user.
- The board updates on the refresh interval, so wait up to 30 seconds.

### Titles are cut off

- The display lines (`line_1`–`line_3`) are sized to the board. The full values are in `{{plex.title}}`, `{{plex.show}}` and the other media variables.
