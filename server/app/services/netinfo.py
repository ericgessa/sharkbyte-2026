"""Which address should a laptop bridge on ANOTHER computer use to reach this server, and can it?

The app may be talking to the server as ``http://localhost:8000`` (Expo web on the same computer), which is useless
to a second laptop: there, ``localhost`` is the laptop itself. So the pairing reply lists addresses that work from
the network (this computer's LAN addresses), and checks the one thing that most often breaks pairing: a server
started with plain ``uvicorn app.main:app`` listens on 127.0.0.1 only, and every other computer is refused.
"""

from __future__ import annotations

import socket
import time
from typing import Any

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
CHECK_TIMEOUT_S = 0.5
CHECK_CACHE_S = 30.0

LOCAL_ONLY_WARNING = (
    "This server only accepts connections from its own computer, so a bridge on another laptop will be refused. "
    "Restart it with: python -m scripts.dev_server (from server/), or add --host 0.0.0.0 to the uvicorn command."
)

_reach_cache: dict[tuple[str, int], tuple[float, bool]] = {}


def is_loopback(host: str | None) -> bool:
    h = (host or "").strip("[]").lower()
    return h in LOOPBACK_HOSTS or h.startswith("127.")


def lan_ips() -> list[str]:
    """Best effort: the IPv4 addresses other devices on the network would use for this computer."""
    ips: set[str] = set()
    try:
        # No packet is sent: connecting a UDP socket only makes the OS pick the outbound interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def accepts_from_network(ip: str, port: int, now: float | None = None) -> bool:
    """True if something accepts TCP connections on ``ip:port``. Connecting to our own LAN address tells a server
    bound to 0.0.0.0 (reachable) apart from one bound to 127.0.0.1 (refused). Cached briefly."""
    now = time.monotonic() if now is None else now
    hit = _reach_cache.get((ip, port))
    if hit is not None and now - hit[0] < CHECK_CACHE_S:
        return hit[1]
    try:
        with socket.create_connection((ip, port), timeout=CHECK_TIMEOUT_S):
            ok = True
    except OSError:
        ok = False
    _reach_cache[(ip, port)] = (now, ok)
    return ok


def bridge_addresses(base_url: str, url_host: str | None, local_port: int | None, ips: list[str] | None = None) -> tuple[list[str], str | None]:
    """(addresses for the bridge, best first; a warning or None).

    ``base_url``: the address the app used. ``url_host``: its host. ``local_port``: the port this server's socket
    really listens on (from the ASGI scope, so it is right even when the app's URL has no port).
    """
    base = base_url.rstrip("/")
    urls: list[str] = []
    if not is_loopback(url_host):
        urls.append(base)  # the app reached us at a network address (LAN IP, tunnel, deployment): the bridge can too
    warning = None
    if local_port:
        found = lan_ips() if ips is None else ips
        lan = [f"http://{ip}:{local_port}" for ip in found]
        urls += [u for u in lan if u not in urls]
        if is_loopback(url_host) and found and not any(accepts_from_network(ip, local_port) for ip in found):
            warning = LOCAL_ONLY_WARNING
    if not urls:
        urls.append(base)  # nothing better is known: at least a bridge on this same computer works
    return urls, warning


def listening_port(scope: dict[str, Any]) -> int | None:
    """The port this server's socket listens on, from an ASGI scope (None if unknown)."""
    server = scope.get("server")
    if isinstance(server, (tuple, list)) and len(server) >= 2 and isinstance(server[1], int):
        return server[1]
    return None
