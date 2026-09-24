#!/usr/bin/env python3
"""Local static server for the FX paper trading terminal.

Usage:  python3 server.py [port]     (default 8317, falls back to the next
        free port if occupied)
Then open the printed URL — market data comes straight from the
biquote public feed in the browser; nothing is proxied or stored server-side.
"""
import errno
import http.server
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8317


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
    }

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    for port in range(PORT, PORT + 20):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    else:
        raise SystemExit(f"no free port in range {PORT}-{PORT + 19}")
    print(f"Serving FX paper trading terminal at http://127.0.0.1:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
