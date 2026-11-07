"""Run the API so phones, emulators and teammates can reach it, and print the URLs to use.

    python -m scripts.dev_server [--port 8000] [--reload]     (run from server/)

Listens on all network interfaces (0.0.0.0). Everything except /health requires a Supabase
login token, but anyone on your network can reach the port, so use trusted networks.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys


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
    p.add_argument(
        "--reload",
        action="store_true",
        help="auto-restart on code changes. Off by default: with the Windows Store build of Python the old\n"
        "process keeps serving stale code after a 'reload'. Works fine on macOS/Linux/python.org Python.",
    )
    args = p.parse_args()

    print("\nSharkByte API. Use these as EXPO_PUBLIC_API_URL in mobile/.env:")
    for ip in lan_ips():
        print(f"  physical phone / teammate on same Wi-Fi : http://{ip}:{args.port}")
    print(f"  Android emulator                        : http://10.0.2.2:{args.port}")
    print(f"  iOS simulator / Expo web on this machine: http://localhost:{args.port}")
    print(f"  interactive API docs                    : http://localhost:{args.port}/docs\n")

    # Launch uvicorn's own CLI in a child process. Calling uvicorn.run(reload=True) from this
    # script re-ran it on every reload on Windows and left a stale server serving old code.
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(args.port)]
    if args.reload:
        cmd += ["--reload", "--reload-dir", "app"]
    else:
        print("Auto-reload is OFF: restart this script (Ctrl+C, run again) after changing backend code.\n")
    try:
        raise SystemExit(subprocess.call(cmd))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
