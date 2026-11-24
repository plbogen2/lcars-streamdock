# LCARS Dock for MiraBox StreamDock (macOS)

Star Trek LCARS-themed StreamDock plugin (`com.plbogen.lcarsdock.sdPlugin`) for macOS with live 144x144 canvas-rendered tiles and a zero-dependency local Python bridge.

## Included Actions
- **Spotify**: `Now Playing` (live album art, track/artist, play/pause), `Previous`, `Next` (upcoming queue preview), `Like` (live liked status + toggle).
- **Google Meet**: `Meet Window` (focus/minimize call tab), `Meet Mic` (live mute state + toggle), `Meet Camera` (live cam state + toggle), `Meet Hand` (live raised state + toggle).
- **Android Messages**: Live unread count from `messages.google.com/web` + focus on press.
- **Gmail & Calendar**: Live unread/tab status + focus on press.
- **LCARS Widgets**: `LCARS Clock` (stardate + time), `LCARS Date`, and `LCARS Weather` (local temperature, condition, high/low).

## Install
1. Copy `com.plbogen.lcarsdock.sdPlugin` into `~/Library/Application Support/HotSpot/StreamDock/plugins/`.
2. Install and start the macOS bridge:
   ```bash
   bash bridge/install_launchagent.sh
   ```
3. In Google Chrome, enable **View > Developer > Allow JavaScript from Apple Events** for zero-focus Google Meet controls and Android Messages unread counts.
4. *(Optional, for Spotify `Next` queue preview and live `Like` status)*:
   - Create a Spotify Developer app with Redirect URI `http://127.0.0.1:15890/spotify_callback`.
   - Save `~/.lcars_dock/spotify_oauth.json` (`chmod 600`) with `{"client_id": "...", "client_secret": "..."}`.
   - Open `http://127.0.0.1:15890/spotify_auth` in Chrome and approve.
5. Restart `StreamDock.app` and drag actions from the **LCARS Dock** category onto your deck.
