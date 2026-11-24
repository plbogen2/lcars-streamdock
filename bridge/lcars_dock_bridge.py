#!/usr/bin/env python3
"""Standalone macOS HTTP bridge for the LCARS Dock StreamDock plugin.

Zero third-party dependencies. Serves http://127.0.0.1:15890 with:
  - GET  /state             -> Spotify, Google Meet, Android Messages, Gmail, Calendar, Weather
  - POST /action            -> Play/Pause, Prev, Next, Like, Meet Mic/Cam/Hand/Focus, App Focus, Weather
  - GET  /spotify_auth      -> Redirects to Spotify OAuth (if client_id/secret configured)
  - GET  /spotify_callback  -> Exchanges OAuth code and stores tokens in ~/.lcars_dock/spotify_oauth.json
"""

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple


UNREAD_TITLE_RE = re.compile(r"(^\(\d+\+?\)|\bInbox\s*\(\d+\)|^[•●*]\s)")

CHROME_TAB_APPLE_SCRIPT = r'''
if application "Google Chrome" is running then
  tell application "Google Chrome"
    set outText to ""
    repeat with w in windows
      repeat with t in tabs of w
        set outText to outText & (URL of t) & "\t" & (title of t) & "\n"
      end repeat
    end repeat
    return outText
  end tell
end if
return ""
'''

APP_TARGETS: Dict[str, Dict[str, Any]] = {
    "messages": {
        "app_path": os.path.expanduser("~/Applications/Chrome Apps.localized/Messages.app"),
        "patterns": ("messages.google.com",),
        "url": "https://messages.google.com/web",
    },
    "gmail": {
        "app_path": "",
        "patterns": ("mail.google.com",),
        "url": "https://mail.google.com",
    },
    "calendar": {
        "app_path": "",
        "patterns": ("calendar.google.com",),
        "url": "https://calendar.google.com",
    },
}


def focus_target(target: str) -> None:
    spec = APP_TARGETS.get(target)
    if not spec:
        return
    app_path = spec.get("app_path", "")
    if app_path and os.path.exists(app_path):
        subprocess.Popen(["open", app_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    conds = " or ".join(f'(u contains "{p}")' for p in spec["patterns"])
    script = f'''
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      set tabIdx to 1
      repeat with t in tabs of w
        set u to (URL of t)
        if {conds} then
          set active tab index of w to tabIdx
          set index of w to 1
          activate
          return "FOCUSED"
        end if
        set tabIdx to tabIdx + 1
      end repeat
    end repeat
  end tell
end if
return "NOT_FOUND"
'''
    try:
        res = subprocess.check_output(
            ["/usr/bin/osascript", "-e", script], stderr=subprocess.DEVNULL, text=True, timeout=3
        ).strip()
        if res == "FOCUSED":
            return
    except Exception:
        pass
    subprocess.Popen(["open", spec["url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def poll_meet_controls_state() -> Optional[Dict[str, bool]]:
    js_code = (
        "(() => {"
        "const isOn = (sel) => {"
        "  const b = document.querySelector(sel);"
        "  if (!b) return false;"
        "  return b.getAttribute('aria-pressed') === 'true' || b.dataset.isMuted === 'false';"
        "};"
        "const micOffBtn = document.querySelector('button[aria-label^=\"Turn on microphone\" i]');"
        "const micOnBtn = document.querySelector('button[aria-label^=\"Turn off microphone\" i]');"
        "const camOffBtn = document.querySelector('button[aria-label^=\"Turn on camera\" i]');"
        "const camOnBtn = document.querySelector('button[aria-label^=\"Turn off camera\" i]');"
        "const handBtn = document.querySelector('button[aria-label^=\"Lower hand\" i], button[aria-label^=\"Raise hand\" i]');"
        "const handRaised = handBtn ? ((handBtn.getAttribute('aria-label') || '').toLowerCase().startsWith('lower hand') || handBtn.getAttribute('aria-pressed') === 'true') : false;"
        "return JSON.stringify({"
        "  micOn: micOnBtn ? true : (micOffBtn ? false : isOn('[data-is-muted]')),"
        "  camOn: camOnBtn ? true : (camOffBtn ? false : false),"
        "  handRaised"
        "});"
        "})()"
    ).replace('"', '\\"')
    script = f'''
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      repeat with t in tabs of w
        if (URL of t) contains "meet.google.com/" then
          return execute t javascript "{js_code}"
        end if
      end repeat
    end repeat
  end tell
end if
return ""
'''
    try:
        out = subprocess.check_output(
            ["/usr/bin/osascript", "-e", script], stderr=subprocess.DEVNULL, text=True, timeout=2.5
        ).strip()
        if out.startswith("{"):
            data = json.loads(out)
            return {
                "micOn": bool(data.get("micOn")),
                "camOn": bool(data.get("camOn")),
                "handRaised": bool(data.get("handRaised")),
            }
    except Exception:
        pass
    return None


def run_google_meet_action(action: str) -> Optional[Dict[str, bool]]:
    if action == "toggle_meet_window":
        script = '''
tell application "System Events"
  set frontApp to name of first application process whose frontmost is true
end tell
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      set tabIdx to 1
      repeat with t in tabs of w
        if (URL of t) contains "meet.google.com/" then
          if frontApp is "Google Chrome" and (id of w) is (id of window 1) and (active tab index of w) is tabIdx and not (miniaturized of w) then
            set miniaturized of w to true
            return "HIDDEN"
          else
            set miniaturized of w to false
            set active tab index of w to tabIdx
            set index of w to 1
            activate
            return "SHOWN"
          end if
        end if
        set tabIdx to tabIdx + 1
      end repeat
    end repeat
  end tell
end if
return "NO_MEET"
'''
        subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None

    selectors = {
        "meet_mic": 'button[aria-label^="Turn on microphone" i], button[aria-label^="Turn off microphone" i]',
        "meet_cam": 'button[aria-label^="Turn on camera" i], button[aria-label^="Turn off camera" i]',
        "meet_hand": 'button[aria-label^="Raise hand" i], button[aria-label^="Lower hand" i]',
    }
    shortcuts = {
        "meet_mic": 'keystroke "d" using {command down}',
        "meet_cam": 'keystroke "e" using {command down}',
        "meet_hand": 'keystroke "h" using {control down, command down}',
    }
    sel = selectors.get(action)
    key_cmd = shortcuts.get(action)
    if not sel or not key_cmd:
        return None

    js_code = (
        f"(() => {{ const b = document.querySelector('{sel}'); if (!b) return 'NO_BTN'; b.click(); return 'OK'; }})()"
    ).replace('"', '\\"')
    js_script = f'''
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      repeat with t in tabs of w
        if (URL of t) contains "meet.google.com/" then
          return execute t javascript "{js_code}"
        end if
      end repeat
    end repeat
  end tell
end if
return "NO_MEET"
'''
    try:
        out = subprocess.check_output(
            ["/usr/bin/osascript", "-e", js_script], stderr=subprocess.DEVNULL, text=True, timeout=2.5
        ).strip()
        if out.startswith("OK"):
            return poll_meet_controls_state()
    except Exception:
        pass

    key_script = f'''
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      set tabIdx to 1
      repeat with t in tabs of w
        if (URL of t) contains "meet.google.com/" then
          set active tab index of w to tabIdx
          set index of w to 1
          activate
          delay 0.12
          tell application "System Events" to {key_cmd}
          return "OK_KEY"
        end if
        set tabIdx to tabIdx + 1
      end repeat
    end repeat
  end tell
end if
return "NO_MEET"
'''
    subprocess.Popen(["/usr/bin/osascript", "-e", key_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


def poll_apps() -> Dict[str, Any]:
    apps: Dict[str, Any] = {
        k: {"running": False, "notify": False, "count": ""}
        for k in ("messages", "gmail", "calendar", "meet")
    }
    try:
        tab_out = subprocess.check_output(
            ["/usr/bin/osascript", "-e", CHROME_TAB_APPLE_SCRIPT], stderr=subprocess.DEVNULL, text=True, timeout=3
        )
    except Exception:
        tab_out = ""

    url_map = (
        ("messages", ("messages.google.com",)),
        ("gmail", ("mail.google.com",)),
        ("calendar", ("calendar.google.com",)),
        ("meet", ("meet.google.com/",)),
    )
    for line in tab_out.splitlines():
        if "\t" not in line:
            continue
        url, title = line.split("\t", 1)
        u = url.lower()
        t_clean = title.strip()
        for key, patterns in url_map:
            if any(p in u for p in patterns):
                apps[key]["running"] = True
                if UNREAD_TITLE_RE.search(t_clean):
                    apps[key]["notify"] = True
                m_cnt = re.search(r"\bInbox\s*\((\d+\+?)\)|^\((\d+\+?)\)", t_clean)
                if m_cnt:
                    apps[key]["count"] = m_cnt.group(1) or m_cnt.group(2) or ""
                    apps[key]["hasCount"] = True
                break

    if apps["messages"]["running"]:
        try:
            msg_js = "document.querySelectorAll('[data-e2e-is-unread=true]').length"
            msg_script = (
                'if application "Google Chrome" is running then\n'
                '  tell application "Google Chrome"\n'
                '    repeat with w in windows\n'
                '      repeat with t in tabs of w\n'
                '        if (URL of t) contains "messages.google.com" then\n'
                f'          return execute t javascript "{msg_js}"\n'
                '        end if\n'
                '      end repeat\n'
                '    end repeat\n'
                '  end tell\n'
                'end if\n'
                'return ""\n'
            )
            cnt_out = subprocess.check_output(
                ["/usr/bin/osascript", "-e", msg_script], stderr=subprocess.DEVNULL, text=True, timeout=2.0
            ).strip()
            if cnt_out.isdigit():
                apps["messages"]["hasCount"] = True
                n = int(cnt_out)
                apps["messages"]["notify"] = n > 0
                apps["messages"]["count"] = str(n) if n > 0 else ""
        except Exception:
            pass

    if apps["meet"]["running"]:
        apps["meetState"] = poll_meet_controls_state() or {"micOn": False, "camOn": False, "handRaised": False}
    else:
        apps["meetState"] = {"micOn": False, "camOn": False, "handRaised": False}
    return apps


def poll_spotify_info() -> Dict[str, Any]:
    empty = {"running": False, "playing": False, "track": "", "artist": "", "album": "", "artworkUrl": "", "trackId": ""}
    script = (
        'if application "Spotify" is running then\n'
        '  tell application "Spotify"\n'
        '    set pState to (player state as string)\n'
        '    try\n'
        '      set tName to (name of current track) as string\n'
        '      set tArtist to (artist of current track) as string\n'
        '      set tAlbum to (album of current track) as string\n'
        '      set tArt to (artwork url of current track) as string\n'
        '      set tId to (id of current track) as string\n'
        '      return "1" & tab & pState & tab & tName & tab & tArtist & tab & tAlbum & tab & tArt & tab & tId\n'
        '    on error\n'
        '      return "1" & tab & pState & tab & "" & tab & "" & tab & "" & tab & "" & tab & ""\n'
        '    end try\n'
        '  end tell\n'
        'end if\n'
        'return "0"\n'
    )
    try:
        res = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=2, check=False)
        if res.returncode != 0 or not res.stdout.strip().startswith("1\t"):
            return empty
        parts = res.stdout.strip().split("\t")
        while len(parts) < 7:
            parts.append("")
        tid = parts[6].strip()
        tid = tid.split(":")[-1] if tid.startswith("spotify:track:") else ""
        return {
            "running": True,
            "playing": parts[1].strip().lower() == "playing",
            "track": parts[2].strip(),
            "artist": parts[3].strip(),
            "album": parts[4].strip(),
            "artworkUrl": parts[5].strip(),
            "trackId": tid,
        }
    except Exception:
        return empty


class SpotifyWebApiClient:
    def __init__(self, port: int = 15890) -> None:
        self._path = os.path.expanduser("~/.lcars_dock/spotify_oauth.json")
        self._redirect_uri = f"http://127.0.0.1:{port}/spotify_callback"
        self._lock = threading.Lock()
        self._cache_track_id = ""
        self._cache_liked = False
        self._cache_next: Tuple[str, str] = ("", "")
        self._last_api_poll = 0.0

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(self._path, 0o600)

    def authorize_url(self) -> str:
        cid = self._load().get("client_id", "")
        if not cid:
            return ""
        params = urllib.parse.urlencode({
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": self._redirect_uri,
            "scope": "user-read-playback-state user-read-currently-playing user-library-read user-library-modify user-modify-playback-state",
        })
        return "https://accounts.spotify.com/authorize?" + params

    def exchange_code(self, code: str) -> Tuple[bool, str]:
        with self._lock:
            cfg = self._load()
            cid, csec = cfg.get("client_id", ""), cfg.get("client_secret", "")
            if not cid or not csec:
                return (False, "Missing client_id/client_secret in ~/.lcars_dock/spotify_oauth.json")
            body = urllib.parse.urlencode({
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
            }).encode("utf-8")
            auth = base64.b64encode(f"{cid}:{csec}".encode("utf-8")).decode("ascii")
            req = urllib.request.Request(
                "https://accounts.spotify.com/api/token",
                data=body,
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    tok = json.loads(resp.read().decode("utf-8"))
                cfg["access_token"] = tok["access_token"]
                if tok.get("refresh_token"):
                    cfg["refresh_token"] = tok["refresh_token"]
                cfg["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
                self._save(cfg)
                self._last_api_poll = 0.0
                return (True, "Spotify connected!")
            except Exception as exc:
                return (False, str(exc))

    def _get_token(self) -> str:
        cfg = self._load()
        tok = cfg.get("access_token", "")
        if tok and time.time() < float(cfg.get("expires_at", 0)):
            return tok
        rtok, cid, csec = cfg.get("refresh_token", ""), cfg.get("client_id", ""), cfg.get("client_secret", "")
        if not (rtok and cid and csec):
            return ""
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rtok}).encode("utf-8")
        auth = base64.b64encode(f"{cid}:{csec}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            cfg["access_token"] = res["access_token"]
            if res.get("refresh_token"):
                cfg["refresh_token"] = res["refresh_token"]
            cfg["expires_at"] = time.time() + int(res.get("expires_in", 3600)) - 60
            self._save(cfg)
            return cfg["access_token"]
        except Exception:
            return ""

    def _api(self, path: str, method: str = "GET") -> Any:
        tok = self._get_token()
        if not tok:
            return None
        req = urllib.request.Request(
            "https://api.spotify.com/v1" + path,
            data=b"" if method in ("PUT", "DELETE") else None,
            headers={"Authorization": f"Bearer {tok}"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else True
        except Exception:
            return None

    def enrich(self, sp: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        tid = sp.get("trackId", "")
        now = time.time()
        with self._lock:
            if not tid:
                sp.update({"liked": False, "nextTrack": "", "nextArtist": ""})
                return sp
            if force or tid != self._cache_track_id or (now - self._last_api_poll) >= 8.0:
                if self._get_token():
                    self._last_api_poll = now
                    self._cache_track_id = tid
                    uri = urllib.parse.quote(f"spotify:track:{tid}")
                    liked_res = self._api(f"/me/library/contains?uris={uri}")
                    if isinstance(liked_res, list) and liked_res:
                        self._cache_liked = bool(liked_res[0])
                    q_res = self._api("/me/player/queue")
                    if isinstance(q_res, dict) and isinstance(q_res.get("queue"), list) and q_res["queue"]:
                        nxt = q_res["queue"][0]
                        arts = nxt.get("artists") or []
                        self._cache_next = (str(nxt.get("name") or ""), str(arts[0].get("name") or "") if arts else "")
                    else:
                        self._cache_next = ("", "")
            sp["liked"] = self._cache_liked
            sp["nextTrack"], sp["nextArtist"] = self._cache_next
            return sp

    def toggle_like(self, tid: str) -> Optional[bool]:
        if not tid or not self._get_token():
            return None
        with self._lock:
            uri = urllib.parse.quote(f"spotify:track:{tid}")
            liked_res = self._api(f"/me/library/contains?uris={uri}")
            currently = bool(isinstance(liked_res, list) and liked_res and liked_res[0])
            res = self._api(f"/me/library?uris={uri}", method="DELETE" if currently else "PUT")
            if res is not None:
                self._cache_track_id = tid
                self._cache_liked = not currently
                self._last_api_poll = time.time()
                return self._cache_liked
            return None


class LcarsDockBridge:
    def __init__(self, port: int = 15890) -> None:
        self._port = port
        self._spotify = SpotifyWebApiClient(port)
        self._weather_last = 0.0
        self._state: Dict[str, Any] = {
            "apps": {},
            "weather": {"tempF": "", "cond": "", "highF": "", "lowF": "", "city": ""},
            "spotify": {"running": False, "playing": False, "track": "", "artist": "", "artworkUrl": "", "liked": False, "nextTrack": "", "nextArtist": ""},
        }

    def _poll_weather(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and (now - self._weather_last) < 900.0 and self._state["weather"].get("tempF"):
            return self._state["weather"]
        self._weather_last = now
        try:
            req = urllib.request.Request("https://wttr.in/?format=j1", headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cur = (data.get("current_condition") or [{}])[0]
            today = (data.get("weather") or [{}])[0]
            area = (data.get("nearest_area") or [{}])[0]
            w = {
                "tempF": str(cur.get("temp_F", "")),
                "cond": str(((cur.get("weatherDesc") or [{}])[0]).get("value", "")).strip(),
                "highF": str(today.get("maxtempF", "")),
                "lowF": str(today.get("mintempF", "")),
                "city": str(((area.get("areaName") or [{}])[0]).get("value", "")).strip(),
            }
            if w["tempF"]:
                self._state["weather"] = w
        except Exception:
            pass
        return self._state["weather"]

    def _dispatch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = str(payload.get("action", "")).strip()
        if action in ("spotify_play_pause", "spotify_prev", "spotify_next", "spotify_like"):
            def _do() -> None:
                if action == "spotify_play_pause":
                    if not self._state["spotify"].get("running"):
                        subprocess.Popen(["open", "-a", "Spotify"])
                    else:
                        subprocess.run(["/usr/bin/osascript", "-e", 'tell application "Spotify" to playpause'], check=False)
                elif action == "spotify_prev":
                    subprocess.run(["/usr/bin/osascript", "-e", 'tell application "Spotify" to previous track'], check=False)
                elif action == "spotify_next":
                    subprocess.run(["/usr/bin/osascript", "-e", 'tell application "Spotify" to next track'], check=False)
                elif action == "spotify_like":
                    tid = self._state["spotify"].get("trackId", "")
                    if tid:
                        self._spotify.toggle_like(tid)
                time.sleep(0.25)
                self._state["spotify"] = self._spotify.enrich(poll_spotify_info(), force=(action == "spotify_like"))
            threading.Thread(target=_do, daemon=True).start()
            return {"ok": True}
        if action in ("meet_mic", "meet_cam", "meet_hand", "meet_focus", "toggle_meet_window"):
            mapped = "toggle_meet_window" if action == "meet_focus" else action
            def _do_meet() -> None:
                st = run_google_meet_action(mapped)
                if st is not None:
                    self._state["apps"]["meetState"] = st
            threading.Thread(target=_do_meet, daemon=True).start()
            return {"ok": True}
        if action == "focus_app":
            threading.Thread(target=focus_target, args=(str(payload.get("target", "")),), daemon=True).start()
            return {"ok": True}
        if action == "weather_refresh":
            threading.Thread(target=self._poll_weather, args=(True,), daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": f"unknown action: {action}"}

    async def _poll_loop(self) -> None:
        while True:
            try:
                self._state["apps"] = await asyncio.to_thread(poll_apps)
                self._state["spotify"] = await asyncio.to_thread(lambda: self._spotify.enrich(poll_spotify_info()))
                await asyncio.to_thread(self._poll_weather)
            except Exception:
                pass
            await asyncio.sleep(2.5)

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except Exception:
            writer.close()
            return
        lines = raw_head.decode("utf-8", errors="ignore").split("\r\n")
        req_line = lines[0].split(" ") if lines else []
        method = req_line[0].upper() if len(req_line) >= 1 else "GET"
        parsed = urllib.parse.urlsplit(req_line[1] if len(req_line) >= 2 else "/")
        path = parsed.path or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        clen = 0
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                if k.strip().lower() == "content-length":
                    clen = max(0, min(65536, int(v.strip() or 0)))
        body = await asyncio.wait_for(reader.readexactly(clen), timeout=5) if clen > 0 else b""
        cors = "Access-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nConnection: close\r\n"

        if method == "OPTIONS":
            writer.write(f"HTTP/1.1 204 No Content\r\n{cors}\r\n".encode("ascii"))
        elif method == "GET" and path == "/spotify_auth":
            url = self._spotify.authorize_url()
            if not url:
                b = b"Missing client_id in ~/.lcars_dock/spotify_oauth.json"
                writer.write(f"HTTP/1.1 400 Bad Request\r\n{cors}Content-Length: {len(b)}\r\n\r\n".encode("ascii") + b)
            else:
                writer.write(f"HTTP/1.1 302 Found\r\n{cors}Location: {url}\r\nContent-Length: 0\r\n\r\n".encode("ascii"))
        elif method == "GET" and path == "/spotify_callback":
            code = (qs.get("code") or [""])[0]
            ok, msg = await asyncio.to_thread(self._spotify.exchange_code, code) if code else (False, "Missing code")
            html = f"<html><body style='background:#000;color:#ff9900;font-family:sans-serif;padding:40px'><h2>{msg}</h2><p>You can close this tab.</p></body></html>".encode("utf-8")
            writer.write(f"HTTP/1.1 {'200 OK' if ok else '400 Bad Request'}\r\n{cors}Content-Type: text/html\r\nContent-Length: {len(html)}\r\n\r\n".encode("ascii") + html)
        elif method == "GET" and path == "/state":
            data = json.dumps({**self._state, "ts": time.time()}).encode("utf-8")
            writer.write(f"HTTP/1.1 200 OK\r\n{cors}Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
        elif method == "POST" and path == "/action":
            payload = json.loads(body.decode("utf-8", errors="ignore") or "{}")
            data = json.dumps(self._dispatch(payload)).encode("utf-8")
            writer.write(f"HTTP/1.1 200 OK\r\n{cors}Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
        else:
            b = b'{"ok":false,"error":"not found"}'
            writer.write(f"HTTP/1.1 404 Not Found\r\n{cors}Content-Type: application/json\r\nContent-Length: {len(b)}\r\n\r\n".encode("ascii") + b)
        await writer.drain()
        writer.close()

    async def run(self) -> None:
        srv = await asyncio.start_server(self._handle_http, "127.0.0.1", self._port)
        print(f"[lcars-dock] Listening on http://127.0.0.1:{self._port}", flush=True)
        asyncio.create_task(self._poll_loop())
        async with srv:
            await srv.serve_forever()


def main() -> None:
    p = argparse.ArgumentParser(description="Standalone LCARS Dock macOS Bridge")
    p.add_argument("--port", type=int, default=15890)
    args = p.parse_args()
    asyncio.run(LcarsDockBridge(args.port).run())


if __name__ == "__main__":
    main()
