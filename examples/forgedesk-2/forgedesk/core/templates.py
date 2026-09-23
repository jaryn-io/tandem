"""Safe HTML template rendering and layout generation."""

import html
from typing import Any, Dict, List, Optional

from forgedesk.auth.permissions import get_user_role, is_operator_or_admin


def escape_html(text: Any) -> str:
    """Escape text for safe HTML embedding to prevent XSS."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def csrf_input(csrf_token: str) -> str:
    """Generate a hidden input field containing the CSRF token for HTML forms."""
    if not csrf_token:
        return ""
    return f'<input type="hidden" name="csrf_token" value="{escape_html(csrf_token)}">'


def render_page(
    title: str,
    content_html: str,
    user: Optional[Dict[str, Any]] = None,
    flash_messages: Optional[List[Dict[str, str]]] = None,
    active_nav: Optional[str] = None,
    extra_head: str = "",
    extra_scripts: str = "",
    csrf_token: str = "",
) -> str:
    """Render a full HTML page inside the standard ForgeDesk application layout."""
    escaped_title = escape_html(title)

    # Base Navigation items
    nav_items = [
        ("dashboard", "/", "Dashboard"),
        ("reservations", "/reservations", "Reservations & Calendar"),
        ("waiting-list", "/waiting-list", "Waiting List"),
        ("machines", "/machines", "Machines & Maintenance"),
        ("members", "/members", "Members & Qualifications"),
        ("inventory", "/inventory", "Inventory Ledger"),
        ("charges", "/charges", "Charges & Billing"),
        ("audit", "/audit", "Audit Log"),
    ]

    # If Admin or Operator, include User Accounts and Data Management in navigation
    if user and is_operator_or_admin(user):
        nav_items.append(("users", "/auth/users", "Users & Roles"))
        nav_items.append(("data", "/system/data", "Data & Backup"))

    # Build Navigation Links
    nav_links_html = []
    for nav_id, url, label in nav_items:
        is_active = " active" if active_nav == nav_id else ""
        nav_links_html.append(f'<a href="{url}" class="nav-link{is_active}">{escape_html(label)}</a>')
    nav_html = "\n".join(nav_links_html)

    # User Section in Header
    if user:
        user_role = (user.get("role") or "viewer").lower()
        role_name = escape_html(user_role.upper())
        user_name = escape_html(user.get("full_name") or user.get("name") or user.get("username", "User"))
        role_class = f"role-badge role-{escape_html(user_role)}"
        logout_csrf = csrf_input(csrf_token)
        user_section_html = f"""
        <div class="user-profile">
            <a href="/auth/profile" class="user-profile-link" title="View Profile & Settings" style="color: inherit; text-decoration: none; display: flex; align-items: center; gap: 0.4rem;">
                <span class="user-name">{user_name}</span>
                <span class="{role_class}">{role_name}</span>
            </a>
            <form action="/auth/logout" method="POST" style="display:inline; margin-left: 0.5rem;">
                {logout_csrf}
                <button type="submit" class="btn btn-sm btn-outline-danger" title="Log Out">Logout</button>
            </form>
        </div>
        """
    else:
        user_section_html = """
        <div class="user-profile">
            <a href="/auth/login" class="btn btn-sm btn-primary">Sign In</a>
        </div>
        """

    # Flash Messages
    flash_html = ""
    if flash_messages:
        flash_items = []
        for msg in flash_messages:
            category = escape_html(msg.get("category", "info"))
            text = escape_html(msg.get("text", ""))
            flash_items.append(
                f'<div class="alert alert-{category} alert-dismissible">'
                f'<span>{text}</span>'
                f'<button type="button" class="btn-close" onclick="this.parentElement.remove();">&times;</button>'
                f'</div>'
            )
        flash_html = f'<div class="flash-container">{"".join(flash_items)}</div>'

    csrf_meta = f'<meta name="csrf-token" content="{escape_html(csrf_token)}">' if csrf_token else ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escaped_title} - ForgeDesk Makerspace</title>
    {csrf_meta}
    <link rel="stylesheet" href="/static/css/forgedesk.css">
    {extra_head}
</head>
<body>
    <header class="app-header">
        <div class="header-container">
            <div class="brand-section">
                <a href="/" class="brand-link">
                    <span class="brand-icon">⚡</span>
                    <span class="brand-name">ForgeDesk</span>
                    <span class="brand-tag">Makerspace</span>
                </a>
            </div>
            <nav class="main-nav">
                {nav_html}
            </nav>
            <div class="header-right">
                <span class="tz-badge" title="Makerspace standard operating timezone">Europe/Rome (TZ)</span>
                {user_section_html}
            </div>
        </div>
    </header>

    <main class="main-container">
        {flash_html}
        {content_html}
    </main>

    <footer class="app-footer">
        <div class="footer-container">
            <p>&copy; ForgeDesk Makerspace Management System &bull; Offline Linux Service &bull; Europe/Rome</p>
        </div>
    </footer>

    <script src="/static/js/forgedesk.js"></script>
    {extra_scripts}
</body>
</html>
"""

