"""HTTP Server implementation for ForgeDesk using standard library threading HTTP server."""

import http.server
import logging
import mimetypes
import os
from pathlib import Path
import socketserver
from typing import Optional

from forgedesk.config import (
    COOKIE_SECURE,
    HOST,
    MAX_REQUEST_BODY_SIZE,
    PORT,
    STATIC_DIR,
    UPLOAD_DIR,
    validate_security_configuration,
)
from forgedesk.core.http import Request, Response
from forgedesk.core.router import Router

logger = logging.getLogger("forgedesk.server")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded HTTP Server handling concurrent requests cleanly."""
    daemon_threads = True
    allow_reuse_address = True


class ForgeDeskHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """Handles raw HTTP connections, dispatches to Router, and returns responses."""

    server_version = "ForgeDesk/1.0"
    router: Optional[Router] = None

    def log_message(self, format: str, *args) -> None:
        """Custom clean logging for incoming HTTP requests."""
        # Clean stdout logger: e.g., "GET / HTTP/1.1" 200
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    def do_PATCH(self) -> None:
        self._handle_request("PATCH")

    def _serve_static_file(self, static_path: str) -> Optional[Response]:
        """Serve static assets from static/ directory with strict path traversal checks."""
        clean_rel_path = static_path.lstrip("/")
        target_path = (STATIC_DIR / clean_rel_path).resolve()

        # Prevent directory traversal attacks
        try:
            target_path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return Response.html("<h1>403 Forbidden</h1><p>Access denied.</p>", status_code=403)

        if not target_path.is_file():
            return Response.html("<h1>404 Not Found</h1><p>Static asset not found.</p>", status_code=404)

        mime_type, _ = mimetypes.guess_type(str(target_path))
        mime_type = mime_type or "application/octet-stream"
        content = target_path.read_bytes()

        return Response(
            body=content,
            status_code=200,
            content_type=mime_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Length": str(len(content)),
            }
        )

    def _handle_request(self, method: str) -> None:
        """Process incoming request through static file server or router."""
        path = self.path

        # Handle static files directly
        if path.startswith("/static/"):
            static_subpath = path[len("/static/"):]
            response = self._serve_static_file(static_subpath)
            self._send_response(response)
            return

        # Read request body with bounded size check (SEC-REQ-BODY-UNBOUNDED)
        content_length_header = self.headers.get("Content-Length")
        body_bytes = b""
        if content_length_header is not None:
            try:
                length = int(content_length_header.strip())
                if length < 0:
                    self._send_response(Response.html("<h1>400 Bad Request</h1><p>Invalid Content-Length.</p>", status_code=400))
                    return
            except ValueError:
                self._send_response(Response.html("<h1>400 Bad Request</h1><p>Invalid Content-Length header format.</p>", status_code=400))
                return

            if length > MAX_REQUEST_BODY_SIZE:
                logger.warning("Request body of %d bytes exceeded MAX_REQUEST_BODY_SIZE (%d bytes)", length, MAX_REQUEST_BODY_SIZE)
                self._send_response(Response.html("<h1>413 Payload Too Large</h1><p>Request body exceeds maximum allowed size.</p>", status_code=413))
                return

            try:
                # Read safely in chunks up to length
                bytes_left = length
                chunks = []
                while bytes_left > 0:
                    read_chunk_size = min(bytes_left, 65536)
                    chunk = self.rfile.read(read_chunk_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    bytes_left -= len(chunk)
                body_bytes = b"".join(chunks)
            except Exception as e:
                logger.error("Error reading request body: %s", e)
                self._send_response(Response.html("<h1>400 Bad Request</h1><p>Error reading request body.</p>", status_code=400))
                return

        # Convert headers to dict
        headers_dict = {key: val for key, val in self.headers.items()}

        # Create Request instance
        req = Request(
            method=method,
            path=path,
            headers=headers_dict,
            body=body_bytes,
            client_address=self.client_address,
        )

        # Dispatch via Router
        if self.router:
            response = self.router.dispatch(req)
        else:
            response = Response.html("<h1>Server Router Not Configured</h1>", status_code=500)

        self._send_response(response)


    def _send_response(self, response: Response) -> None:
        """Send complete HTTP response back to the client socket."""
        try:
            self.send_response(response.status_code, response.STATUS_MESSAGES.get(response.status_code, "OK"))

            # Send standard response headers
            for header_key, header_val in response.headers.items():
                self.send_header(header_key, header_val)

            # Send Set-Cookie headers
            for cookie_str in response._cookies:
                self.send_header("Set-Cookie", cookie_str)

            self.end_headers()

            # Send body
            if response.body and response.status_code != 204:
                self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def create_server(router: Router, host: str = HOST, port: int = PORT) -> ThreadedHTTPServer:
    """Create and configure a threaded HTTP server instance with transport security checks."""
    validate_security_configuration(host=host, cookie_secure=COOKIE_SECURE)
    ForgeDeskHTTPRequestHandler.router = router
    return ThreadedHTTPServer((host, port), ForgeDeskHTTPRequestHandler)
