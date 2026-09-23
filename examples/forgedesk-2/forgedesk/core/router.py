import html
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from forgedesk.core.http import Request, Response

logger = logging.getLogger("forgedesk.router")

HandlerFunc = Callable[[Request], Response]
MiddlewareFunc = Callable[[Request, HandlerFunc], Response]



class Route:
    """Represents a single registered URL route."""

    def __init__(self, method: str, pattern: str, handler: HandlerFunc, name: Optional[str] = None):
        self.method = method.upper()
        self.pattern = pattern
        self.handler = handler
        self.name = name or pattern
        self.param_count = len(re.findall(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", pattern))
        self.is_static = self.param_count == 0

        # Compile pattern to regex if it contains path parameters like {id} or {user_id}
        # e.g., /machines/{id}/edit -> ^/machines/(?P<id>[^/]+)/edit$
        regex_pattern = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", r"(?P<\1>[^/]+)", pattern)
        self.regex = re.compile(f"^{regex_pattern}$")

    def match(self, path: str) -> Optional[Dict[str, str]]:
        """Check if path matches this route. Returns dict of extracted parameters if matched."""
        match = self.regex.match(path)
        if match:
            return match.groupdict()
        return None


class Router:
    """Manages route definitions, middlewares, and request dispatching."""

    def __init__(self):
        self.routes: List[Route] = []
        self.middlewares: List[MiddlewareFunc] = []

    def use(self, middleware: MiddlewareFunc) -> None:
        """Register a middleware function."""
        self.middlewares.append(middleware)

    def add_route(self, method: str, pattern: str, handler: HandlerFunc, name: Optional[str] = None) -> None:
        """Add a route for specific HTTP method."""
        self.routes.append(Route(method, pattern, handler, name))

    def get(self, pattern: str, name: Optional[str] = None) -> Callable[[HandlerFunc], HandlerFunc]:
        """Decorator for GET routes."""
        def decorator(handler: HandlerFunc) -> HandlerFunc:
            self.add_route("GET", pattern, handler, name)
            return handler
        return decorator

    def post(self, pattern: str, name: Optional[str] = None) -> Callable[[HandlerFunc], HandlerFunc]:
        """Decorator for POST routes."""
        def decorator(handler: HandlerFunc) -> HandlerFunc:
            self.add_route("POST", pattern, handler, name)
            return handler
        return decorator

    def put(self, pattern: str, name: Optional[str] = None) -> Callable[[HandlerFunc], HandlerFunc]:
        """Decorator for PUT routes."""
        def decorator(handler: HandlerFunc) -> HandlerFunc:
            self.add_route("PUT", pattern, handler, name)
            return handler
        return decorator

    def delete(self, pattern: str, name: Optional[str] = None) -> Callable[[HandlerFunc], HandlerFunc]:
        """Decorator for DELETE routes."""
        def decorator(handler: HandlerFunc) -> HandlerFunc:
            self.add_route("DELETE", pattern, handler, name)
            return handler
        return decorator

    def patch(self, pattern: str, name: Optional[str] = None) -> Callable[[HandlerFunc], HandlerFunc]:
        """Decorator for PATCH routes."""
        def decorator(handler: HandlerFunc) -> HandlerFunc:
            self.add_route("PATCH", pattern, handler, name)
            return handler
        return decorator

    def dispatch(self, request: Request) -> Response:
        """Match and dispatch an incoming request through the middleware chain to its handler."""
        # Find matching route prioritizing static / more specific routes
        matched_route: Optional[Route] = None
        allowed_methods: List[str] = []
        matching_routes: List[Tuple[Route, Dict[str, str]]] = []

        for route in self.routes:
            params = route.match(request.path)
            if params is not None:
                allowed_methods.append(route.method)
                if route.method == request.method:
                    matching_routes.append((route, params))

        if matching_routes:
            # Sort routes with fewer parameters first (0 parameters / static exact matches first)
            matching_routes.sort(key=lambda item: item[0].param_count)
            matched_route, request.route_params = matching_routes[0]

        if matched_route is None:
            is_api = (
                request.path.startswith("/api/")
                or "application/json" in request.headers.get("accept", "")
                or request.headers.get("content-type", "").startswith("application/json")
            )
            if allowed_methods:
                headers = {"Allow": ", ".join(sorted(set(allowed_methods)))}
                if is_api:
                    return Response.json(
                        {
                            "error": "Method Not Allowed",
                            "message": f"Method {request.method} is not allowed for this endpoint.",
                            "status_code": 405,
                        },
                        status_code=405,
                        headers=headers,
                    )
                return Response.html(
                    "<h1>405 Method Not Allowed</h1><p>The requested method is not allowed for this URL.</p>",
                    status_code=405,
                    headers=headers,
                )
            if is_api:
                return Response.json(
                    {
                        "error": "Not Found",
                        "message": f"Endpoint not found: {request.path}",
                        "status_code": 404,
                    },
                    status_code=404,
                )
            safe_path = html.escape(request.path, quote=True)
            return Response.html(
                f"<h1>404 Not Found</h1><p>The requested URL <code>{safe_path}</code> was not found on this server.</p>",
                status_code=404,
            )

        # Build middleware execution chain
        handler = matched_route.handler

        def build_chain(index: int) -> HandlerFunc:
            if index >= len(self.middlewares):
                return handler
            mw = self.middlewares[index]
            next_handler = build_chain(index + 1)
            return lambda req: mw(req, next_handler)

        chained_handler = build_chain(0)

        try:
            return chained_handler(request)
        except Exception as e:
            logger.error("Internal Server Error handling %s %s from %s: %s", request.method, request.path, request.client_address, e, exc_info=True)
            is_api = (
                request.path.startswith("/api/")
                or "application/json" in request.headers.get("accept", "")
                or request.headers.get("content-type", "").startswith("application/json")
            )
            if is_api:
                return Response.json(
                    {
                        "error": "Internal Server Error",
                        "message": "An unexpected server error occurred. Details have been recorded in the server log.",
                        "status_code": 500,
                    },
                    status_code=500,
                )
            return Response.html(
                "<h1>500 Internal Server Error</h1><p>An unexpected server error occurred. Please contact the administrator or consult system logs.</p>",
                status_code=500,
            )

