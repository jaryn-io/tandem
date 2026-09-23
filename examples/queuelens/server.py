#!/usr/bin/env python3
"""
QueueLens - Local Static Server
A zero-dependency, local-only HTTP server for QueueLens.
Binds strictly to localhost (127.0.0.1) for complete privacy and isolation.
"""

import sys
import os
import argparse
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler

class QueueLensRequestHandler(SimpleHTTPRequestHandler):
    """Custom request handler enforcing strict security headers and MIME types."""

    def end_headers(self):
        # Security headers for local-only execution
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self' data:; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'none'; object-src 'none';"
        )
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, format, *args):
        # Clean local terminal logging
        sys.stderr.write(f"[QueueLens] {self.address_string()} - {format % args}\n")

ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

def is_loopback(host: str) -> bool:
    """Verify that host interface resolves strictly to loopback."""
    if host in ALLOWED_LOOPBACK_HOSTS:
        return True
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        return ip.is_loopback
    except ValueError:
        return False

def run_server(host="127.0.0.1", port=8080, open_browser=False):
    if not is_loopback(host):
        raise ValueError(
            f"Security policy violation: QueueLens is a local-only application and strictly prohibits "
            f"binding to non-loopback interface '{host}'. Allowed interfaces: 127.0.0.1, localhost."
        )

    app_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)

    # Try binding to requested port, incrementing if already in use
    server = None
    actual_port = port
    max_attempts = 10

    for attempt in range(max_attempts):
        try:
            server = HTTPServer((host, actual_port), QueueLensRequestHandler)
            break
        except OSError as e:
            if "Address already in use" in str(e) and attempt < max_attempts - 1:
                actual_port += 1
                continue
            raise

    url = f"http://{host}:{actual_port}/index.html"
    print("=" * 60)
    print("  QueueLens - Support Request Backlog Explorer")
    print("=" * 60)
    print(f"  Local Address:     {url}")
    print(f"  Serving Directory: {app_dir}")
    print(f"  Security:          Strictly bound to {host} (Local loopback only)")
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[QueueLens] Server stopped by user. Goodbye!")
    finally:
        server.server_close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Start QueueLens local static HTTP server (loopback-only)."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback host interface (strictly restricted to 127.0.0.1 or localhost; non-loopback is prohibited for security)"
    )
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--open", action="store_true", help="Automatically open default browser")
    args = parser.parse_args()

    if not is_loopback(args.host):
        parser.error(
            f"Security policy violation: QueueLens is a local-only application and strictly prohibits "
            f"binding to non-loopback interface '{args.host}'. Permitted interfaces: 127.0.0.1, localhost."
        )

    run_server(host=args.host, port=args.port, open_browser=args.open)
