"""
Persistent settings (settings.json), validation, first-run import and the optional web password.

The file holds the instrument list, per-instrument views, holdings (quantity / purchase price)
and a salted password hash — so it is written with mode 600.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading

import config
import views

log = logging.getLogger(__name__)

MIN_SECONDS, MAX_SECONDS = 5, 3600
MAX_INSTRUMENTS = 50
_PBKDF2_ITER = 100_000

DEFAULTS: dict = {
    "seconds_per_view": 10,
    "show_ip": True,
    "portfolio": {"summary": True, "table": True, "period": "year"},
    "instruments": [],
    "password": None,     # {"salt": hex, "hash": hex, "iter": int} — never sent to the browser
}

_lock = threading.RLock()


class ValidationError(ValueError):
    """Message is shown to the user as-is (Swedish)."""


# ── validation ────────────────────────────────────────────────────────────────

def _num(value, field: str, *, lo: float = 0.0, hi: float = 1e9):
    """Optional number: None / '' -> None. Accepts '12,5' as well as 12.5."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field}: ogiltigt tal")
    try:
        x = float(str(value).replace(" ", "").replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field}: ogiltigt tal") from None
    if not math.isfinite(x) or x < lo or x > hi:
        raise ValidationError(f"{field}: måste vara mellan {lo:g} och {hi:g}")
    return x


def clean_instrument(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("Ogiltigt instrument")
    oid = str(raw.get("id", "")).strip()
    if not re.fullmatch(r"\d{1,12}", oid):
        raise ValidationError(f"Ogiltigt orderbook-ID: {oid[:20]!r}")
    kind = raw.get("kind", "stock")
    if kind not in views.KINDS:
        raise ValidationError("Typ måste vara aktie eller fond")
    name = str(raw.get("name") or "").strip()[:40]
    chosen = raw.get("views", ["day", "year"])
    if not isinstance(chosen, list) or any(v not in views.VIEWS for v in chosen):
        raise ValidationError("Ogiltig vy vald")
    chosen = [v for v in views.VIEW_ORDER if v in chosen]
    if not chosen:
        raise ValidationError(f"Välj minst en vy för {name or oid}")
    label = name or oid
    return {
        "id": oid,
        "kind": kind,
        "name": name,
        "views": chosen,
        "enabled": bool(raw.get("enabled", True)),
        "qty": _num(raw.get("qty"), f"Antal ({label})"),
        "cost": _num(raw.get("cost"), f"Snittkurs ({label})"),
        "cost_total_sek": _num(raw.get("cost_total_sek"), f"Anskaffningsvärde ({label})"),
    }


def clean(raw: dict, password=None) -> dict:
    """Validate everything the browser may change. The password is carried over, never taken from `raw`."""
    if not isinstance(raw, dict):
        raise ValidationError("Ogiltiga inställningar")
    secs = raw.get("seconds_per_view", DEFAULTS["seconds_per_view"])
    try:
        secs = int(float(secs))
    except (TypeError, ValueError):
        raise ValidationError("Sekunder per vy måste vara ett tal") from None
    if not MIN_SECONDS <= secs <= MAX_SECONDS:
        raise ValidationError(f"Sekunder per vy måste vara {MIN_SECONDS}–{MAX_SECONDS}")

    pf = raw.get("portfolio") or {}
    period = pf.get("period", "year")
    if period not in views.PORTFOLIO_VIEWS:
        raise ValidationError("Ogiltig period för portföljkurvan")

    items = raw.get("instruments", [])
    if not isinstance(items, list) or len(items) > MAX_INSTRUMENTS:
        raise ValidationError(f"Högst {MAX_INSTRUMENTS} instrument")
    instruments, seen = [], set()
    for it in items:
        ins = clean_instrument(it)
        if ins["id"] in seen:
            raise ValidationError(f"{ins['name'] or ins['id']} finns redan i listan")
        seen.add(ins["id"])
        instruments.append(ins)

    return {
        "seconds_per_view": secs,
        "show_ip": bool(raw.get("show_ip", True)),
        "portfolio": {"summary": bool(pf.get("summary", True)),
                      "table": bool(pf.get("table", True)),
                      "period": period},
        "instruments": instruments,
        "password": password,
    }


# ── first-run import of the old stocks.txt ────────────────────────────────────

def import_stocks_txt(path: str) -> list[dict]:
    """Lines: `<id> [name]` for stocks/ETFs/indices, `FUND <id> [name]` for funds, `#` comments."""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                m = re.match(r"^(?:(fund)\s+)?(\d+)(?:\s+(.+))?$", line, re.I)
                if not m:
                    continue
                kind = "fund" if m.group(1) else "stock"
                out.append({"id": m.group(2), "kind": kind, "name": (m.group(3) or "").strip(),
                            "views": ["year"] if kind == "fund" else ["day", "year"]})
    except OSError:
        pass
    seen, uniq = set(), []
    for i in out:                      # the old list contained duplicates (the same id listed twice)
        if i["id"] not in seen:
            seen.add(i["id"])
            uniq.append(i)
    return uniq


# ── load / save ───────────────────────────────────────────────────────────────

_cache: dict | None = None
_cache_mtime: float = 0.0


def _read_file() -> dict:
    with open(config.SETTINGS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    return clean(raw, password=raw.get("password"))


def get() -> dict:
    """Current settings (deep copy). Creates the file on first run."""
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(config.SETTINGS_FILE)
        except OSError:
            mtime = None
        if mtime is None:
            first = copy.deepcopy(DEFAULTS)
            first["instruments"] = [clean_instrument(i) for i in import_stocks_txt(config.STOCKS_SEED)]
            _write(first)
            log.info("Created %s with %d instruments imported from %s",
                     config.SETTINGS_FILE, len(first["instruments"]), config.STOCKS_SEED)
            return copy.deepcopy(first)
        if _cache is None or mtime != _cache_mtime:
            try:
                _cache = _read_file()
            except (OSError, ValueError, ValidationError) as exc:
                log.error("Could not read %s (%s) — using defaults", config.SETTINGS_FILE, exc)
                _cache = copy.deepcopy(DEFAULTS)
            _cache_mtime = mtime
        return copy.deepcopy(_cache)


def _write(data: dict) -> None:
    global _cache, _cache_mtime
    d = os.path.dirname(config.SETTINGS_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, config.SETTINGS_FILE)       # atomic: never a half-written file
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache = copy.deepcopy(data)
    _cache_mtime = os.path.getmtime(config.SETTINGS_FILE)


def save(raw: dict) -> dict:
    """Validate and store settings coming from the browser. Returns the stored settings."""
    with _lock:
        new = clean(raw, password=get()["password"])
        _write(new)
        return copy.deepcopy(new)


def public(data: dict) -> dict:
    """Settings as sent to the browser: no password hash."""
    out = {k: v for k, v in data.items() if k != "password"}
    out["password_set"] = bool(data.get("password"))
    return out


# ── password (optional HTTP Basic auth for the web interface) ─────────────────

def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def has_password() -> bool:
    return bool(get().get("password"))


def set_password(password: str | None) -> None:
    """Set a new password, or remove it when `password` is empty/None."""
    with _lock:
        data = get()
        if not password:
            data["password"] = None
        else:
            if len(password) < 4 or len(password) > 200:
                raise ValidationError("Lösenordet måste vara minst 4 tecken")
            salt = secrets.token_bytes(16)
            data["password"] = {"salt": salt.hex(), "iter": _PBKDF2_ITER,
                                "hash": _hash(password, salt, _PBKDF2_ITER).hex()}
        _write(data)


def check_password(password: str) -> bool:
    p = get().get("password")
    if not p:
        return True
    try:
        expect = bytes.fromhex(p["hash"])
        got = _hash(password, bytes.fromhex(p["salt"]), int(p["iter"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(expect, got)
