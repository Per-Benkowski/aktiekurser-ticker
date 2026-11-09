"""
Web interface: a small JSON API plus one static page (static/index.html).

Only the standard library is used (the Pi Zero 2 W has little memory to spare).

Security
  - Optional password via HTTP Basic auth (any user name, see settings.set_password).
  - Writes need `Content-Type: application/json`, so a page on another site cannot post to the Pi
    from the user's browser (that would need a CORS preflight, which is never granted).
  - Request bodies are capped; all input is validated in settings.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import avanza_client as av
import config
import netinfo
import portfolio
import settings
import views
from state import State

log = logging.getLogger("web")

MAX_BODY = 64 * 1024
_verified: set[str] = set()          # sha256 of Authorization headers already checked (PBKDF2 is slow)
_verified_lock = threading.Lock()


def _forget_verified() -> None:
    with _verified_lock:
        _verified.clear()


class Handler(BaseHTTPRequestHandler):
    server_version = "Aktiekurser"
    state: State                     # set on the subclass in start()

    # ── plumbing ─────────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):            # keep journald quiet
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _authorized(self) -> bool:
        if not settings.has_password():
            return True
        header = self.headers.get("Authorization", "")
        key = hashlib.sha256(header.encode("utf-8", "replace")).hexdigest()
        with _verified_lock:
            if key in _verified:
                return True
        ok = False
        if header.lower().startswith("basic "):
            try:
                user_pass = base64.b64decode(header[6:].strip()).decode("utf-8")
                ok = settings.check_password(user_pass.partition(":")[2])
            except (ValueError, UnicodeDecodeError):
                ok = False
        if ok:
            with _verified_lock:
                _verified.add(key)
            return True
        time.sleep(0.5)                                  # slow down guessing
        self._send(HTTPStatus.UNAUTHORIZED, "Lösenord krävs".encode("utf-8"), "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="Aktiekurser", charset="UTF-8"'})
        return False

    def _read_json(self):
        if "application/json" not in self.headers.get("Content-Type", ""):
            self._error(415, "Innehållet måste vara JSON")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._error(413, "För stor förfrågan")
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._error(400, "Ogiltig JSON")
            return None

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._authorized():
            return
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                with open(os.path.join(config.STATIC_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._json(self._state_payload())
            elif url.path == "/api/current.png":
                png, _ = self.state.snapshot()
                if png is None:
                    self._error(404, "Ingen bild ännu")
                else:
                    self._send(200, png, "image/png")
            elif url.path == "/api/names":
                ids = [x for x in qs.get("ids", [""])[0].split(",") if x.isdigit()][:60]
                kinds = {i["id"]: i["kind"] for i in settings.get()["instruments"]}
                self._json({"names": av.resolve_names([(kinds.get(i, "stock"), i) for i in ids])})
            elif url.path == "/api/search":
                q = (qs.get("q", [""])[0]).strip()
                if len(q) < 2 or len(q) > 60:
                    self._json({"results": []})
                else:
                    self._json({"results": av.search(q)})
            elif url.path == "/api/portfolio":
                view = qs.get("view", [None])[0] or settings.get()["portfolio"]["period"]
                if view not in views.PORTFOLIO_VIEWS:
                    self._error(400, "Ogiltig period")
                else:
                    pf = portfolio.build(settings.get(), view, max_age=120)
                    self._json(portfolio.to_json(pf))
            else:
                self._error(404, "Hittades inte")
        except Exception as exc:                          # never crash the handler thread
            log.exception("GET %s failed", url.path)
            self._error(502 if isinstance(exc, OSError) else 500, "Något gick fel: " + str(exc)[:120])

    def _state_payload(self) -> dict:
        _, current = self.state.snapshot()
        return {
            "settings": settings.public(settings.get()),
            "url": netinfo.display_url(self.state.port),
            "ip": netinfo.get_ip(),
            "names": av.known_names(),
            "current": current,
            "now": time.time(),
            "views": [{"key": k, "sv": views.VIEWS[k]["sv"]} for k in views.VIEW_ORDER],
            "portfolio_views": [{"key": k, "sv": views.VIEWS[k]["sv"]} for k in views.PORTFOLIO_VIEWS],
            "limits": {"min_seconds": settings.MIN_SECONDS, "max_seconds": settings.MAX_SECONDS},
        }

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        if not self._authorized():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()
        if body is None:
            return
        try:
            if path == "/api/settings":
                saved = settings.save(body)
                self.state.request_reload()
                self._json({"settings": settings.public(saved)})
            elif path == "/api/next":
                self.state.request_skip()
                self._json({"ok": True})
            elif path == "/api/password":
                settings.set_password(body.get("password") or None)
                _forget_verified()
                self._json({"password_set": settings.has_password()})
            else:
                self._error(404, "Hittades inte")
        except settings.ValidationError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            log.exception("POST %s failed", path)
            self._error(500, "Något gick fel: " + str(exc)[:120])


def start(state: State) -> int:
    """Start the server in a daemon thread. Returns the port actually bound."""
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = None
    port = config.WEB_PORT
    for candidate in (config.WEB_PORT, config.WEB_FALLBACK_PORT):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", candidate), handler)
            port = candidate
            break
        except OSError as exc:
            log.warning("Cannot bind port %d: %s", candidate, exc)
    if server is None:
        raise RuntimeError("Could not start the web server on any port")
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("Web server listening on port %d", port)
    state.port = port
    return port
