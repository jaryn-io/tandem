"""HTTP Request and Response abstractions with robust parsing and security defaults."""

import email.message
import http.cookies
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, unquote


class UploadedFile:
    """Represents a file uploaded via multipart/form-data."""

    def __init__(self, filename: str, content_type: str, data: bytes):
        self.filename = filename
        self.content_type = content_type
        self.data = data
        self.size = len(data)

    def save_to(self, target_path: Union[str, Path]) -> int:
        """Write file data safely to target location."""
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.write_bytes(self.data)


class Request:
    """Encapsulates an incoming HTTP request."""

    def __init__(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes = b"",
        client_address: Tuple[str, int] = ("127.0.0.1", 0),
    ):
        self.method = method.upper()
        raw_path, _, query_string = path.partition("?")
        self.path = unquote(raw_path)
        self.query_string = query_string
        self.raw_headers = headers
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body
        self.client_address = client_address

        # Parsed attributes (lazy or initialized)
        # Parsed attributes (lazy or initialized)
        self.query_params: Dict[str, List[str]] = parse_qs(query_string, keep_blank_values=True)
        self._parsed_form: Optional[Dict[str, str]] = None
        self._parsed_form_lists: Optional[Dict[str, List[str]]] = None
        self._parsed_files: Optional[Dict[str, UploadedFile]] = None
        self._parsed_json: Optional[Any] = None
        self._cookies: Optional[Dict[str, str]] = None

        # Context variables populated by middleware / authentication
        self.user: Optional[Any] = None
        self.session_id: Optional[str] = None
        self.route_params: Dict[str, str] = {}

    def query(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a single query parameter value."""
        values = self.query_params.get(key)
        return values[0] if values else default

    @property
    def cookies(self) -> Dict[str, str]:
        """Parse HTTP Cookie header."""
        if self._cookies is None:
            self._cookies = {}
            cookie_header = self.headers.get("cookie", "")
            if cookie_header:
                simple_cookie = http.cookies.SimpleCookie()
                try:
                    simple_cookie.load(cookie_header)
                    for key, morsel in simple_cookie.items():
                        self._cookies[key] = morsel.value
                except Exception:
                    pass
        return self._cookies

    def get_cookie(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve cookie by name."""
        return self.cookies.get(name, default)

    def json(self) -> Any:
        """Parse JSON body."""
        if self._parsed_json is None:
            if not self.body:
                return {}
            try:
                self._parsed_json = json.loads(self.body.decode("utf-8"))
            except Exception as e:
                raise ValueError(f"Invalid JSON payload: {e}") from e
        return self._parsed_json

    def _parse_form_data(self) -> None:
        """Parse form data from urlencoded or multipart payloads."""
        if self._parsed_form is not None:
            return

        self._parsed_form = {}
        self._parsed_form_lists = {}
        self._parsed_files = {}

        content_type = self.headers.get("content-type", "")

        if "application/x-www-form-urlencoded" in content_type:
            raw_qs = self.body.decode("utf-8", errors="replace")
            parsed = parse_qs(raw_qs, keep_blank_values=True)
            for k, v in parsed.items():
                self._parsed_form[k] = v[0] if v else ""
                self._parsed_form_lists[k] = list(v)

        elif "multipart/form-data" in content_type:
            self._parse_multipart(content_type)

    def _parse_multipart(self, content_type_header: str) -> None:
        """Parse multipart/form-data cleanly using standard email parser."""
        header_data = f"Content-Type: {content_type_header}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin1")
        msg = email.message_from_bytes(header_data + self.body)

        for part in msg.walk():
            if part.is_multipart():
                continue

            content_disp = part.get("Content-Disposition", "")
            if not content_disp or "form-data" not in content_disp:
                continue

            # Extract parameter name and filename
            params = {}
            for item in content_disp.split(";")[1:]:
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    params[k.strip().lower()] = v.strip(' "')

            field_name = params.get("name")
            if not field_name:
                continue

            filename = params.get("filename")
            payload = part.get_payload(decode=True) or b""

            if filename is not None:
                # Sanitize filename (remove path components)
                safe_name = os.path.basename(filename)
                part_type = part.get_content_type()
                self._parsed_files[field_name] = UploadedFile(
                    filename=safe_name,
                    content_type=part_type,
                    data=payload,
                )
            else:
                charset = part.get_content_charset() or "utf-8"
                val = payload.decode(charset, errors="replace")
                self._parsed_form[field_name] = val
                if field_name not in self._parsed_form_lists:
                    self._parsed_form_lists[field_name] = []
                self._parsed_form_lists[field_name].append(val)

    def form(self) -> Dict[str, str]:
        """Get all parsed form fields."""
        self._parse_form_data()
        return self._parsed_form or {}

    def form_value(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a single form field value."""
        return self.form().get(key, default)

    def form_list(self, key: str) -> List[str]:
        """Get all values for a form field name (useful for multi-selects and checkboxes)."""
        self._parse_form_data()
        return self._parsed_form_lists.get(key, []) if self._parsed_form_lists else []

    def files(self) -> Dict[str, UploadedFile]:
        """Get all uploaded files."""
        self._parse_form_data()
        return self._parsed_files or {}

    def file_value(self, key: str) -> Optional[UploadedFile]:
        """Get uploaded file by input name."""
        return self.files().get(key)


class Response:
    """Encapsulates an outgoing HTTP response with security headers."""

    STATUS_MESSAGES = {
        200: "OK",
        201: "Created",
        204: "No Content",
        301: "Moved Permanently",
        302: "Found",
        303: "See Other",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        413: "Payload Too Large",
        422: "Unprocessable Entity",
        500: "Internal Server Error",
    }

    def __init__(
        self,
        body: Union[str, bytes] = b"",
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: Optional[Dict[str, str]] = None,
    ):
        self.status_code = status_code
        if isinstance(body, str):
            self.body = body.encode("utf-8")
        else:
            self.body = body

        self.headers: Dict[str, str] = {
            "Content-Type": content_type,
            "Content-Length": str(len(self.body)),
            # Security Headers
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "X-XSS-Protection": "1; mode=block",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Content-Security-Policy": (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "font-src 'self'; "
                "frame-ancestors 'none';"
            ),
        }

        if headers:
            self.headers.update(headers)

        self._cookies: List[str] = []

    def set_cookie(
        self,
        name: str,
        value: str,
        max_age: Optional[int] = None,
        path: str = "/",
        http_only: bool = True,
        same_site: str = "Lax",
        secure: bool = False,
    ) -> None:
        """Set an HTTP Set-Cookie header."""
        morsel = http.cookies.Morsel()
        morsel.set(name, value, value)
        morsel["path"] = path
        morsel["samesite"] = same_site
        if max_age is not None:
            morsel["max-age"] = str(max_age)
        if http_only:
            morsel["httponly"] = True
        if secure:
            morsel["secure"] = True

        cookie_str = morsel.OutputString()
        self._cookies.append(cookie_str)

    def delete_cookie(self, name: str, path: str = "/") -> None:
        """Delete a cookie by expiring it immediately."""
        self.set_cookie(name, "", max_age=0, path=path)

    @classmethod
    def html(cls, content: str, status_code: int = 200, headers: Optional[Dict[str, str]] = None) -> "Response":
        """Convenience method for HTML response."""
        return cls(body=content, status_code=status_code, content_type="text/html; charset=utf-8", headers=headers)

    @classmethod
    def json(cls, data: Any, status_code: int = 200, headers: Optional[Dict[str, str]] = None) -> "Response":
        """Convenience method for JSON response."""
        body = json.dumps(data, indent=2, ensure_ascii=False)
        return cls(body=body, status_code=status_code, content_type="application/json; charset=utf-8", headers=headers)

    @classmethod
    def redirect(cls, location: str, status_code: int = 303) -> "Response":
        """Convenience method for HTTP redirect."""
        headers = {"Location": location}
        return cls(body=f"Redirecting to {location}", status_code=status_code, headers=headers)

    @classmethod
    def file(
        cls,
        filepath: Union[str, Path],
        content_type: Optional[str] = None,
        download_filename: Optional[str] = None,
    ) -> "Response":
        """Serve a file from disk safely."""
        path = Path(filepath)
        if not path.is_file():
            return cls.html("<h1>404 Not Found</h1>", status_code=404)

        if content_type is None:
            content_type, _ = mimetypes.guess_type(str(path))
            content_type = content_type or "application/octet-stream"

        data = path.read_bytes()
        headers = {"Content-Length": str(len(data))}
        if download_filename:
            safe_name = os.path.basename(download_filename).replace('"', "")
            headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'

        return cls(body=data, status_code=200, content_type=content_type, headers=headers)
