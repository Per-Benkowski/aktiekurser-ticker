"""Find out which IPv4 address the Pi currently has (it is assigned by DHCP and can change)."""
from __future__ import annotations

import socket
import subprocess


def _usable(ip: str | None) -> bool:
    return bool(ip) and not ip.startswith(("127.", "169.254.", "0."))


def get_ip() -> str | None:
    """Return the primary IPv4 address, or None when there is no network."""
    # The UDP "connect" sends no packet; it only asks the kernel which interface would be used.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.5)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        if _usable(ip):
            return ip
    except OSError:
        pass
    finally:
        s.close()
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout
        for part in out.split():
            if "." in part and _usable(part):
                return part
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def display_url(port: int) -> str:
    """Text shown on the e-ink screen, e.g. 'http://192.168.1.50'."""
    ip = get_ip()
    if ip is None:
        return "NO NETWORK"
    return f"http://{ip}" + ("" if port == 80 else f":{port}")
