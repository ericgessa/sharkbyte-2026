"""Run the API so phones, emulators and teammates can reach it, and print the URLs to use.

    python -m scripts.dev_server [--port 8000] [--no-reload]     (run from server/)

Listens on all network interfaces (0.0.0.0). Everything except /health requires a Supabase
login token, but anyone on your network can reach the port, so use trusted networks.
"""

from __future__ import annotations

import argparse
import socket

import uvicorn


def lan_ips() -> list[str]:
    """Best-effort: the address(es) other devices on your network would use."""
    ips: set[str] = set()
    try:
        # No packet is sent; connecting a UDP socket just makes the OS pick the outbound interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-reload", action="store_true")
    args = p.parse_args()

    print("\nSharkByte API. Use these as EXPO_PUBLIC_API_URL in mobile/.env:")
    for ip in lan_ips():
        print(f"  physical phone / teammate on same Wi-Fi : http://{ip}:{args.port}")
    print(f"  Android emulator                        : http://10.0.2.2:{args.port}")
    print(f"  iOS simulator / Expo web on this machine: http://localhost:{args.port}")
    print(f"  interactive API docs                    : http://localhost:{args.port}/docs\n")

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=["app"] if not args.no_reload else None,
    )


if __name__ == "__main__":
    main()
