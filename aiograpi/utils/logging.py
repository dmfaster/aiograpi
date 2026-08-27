from urllib.parse import urlsplit

DEFAULT_LOG_TEXT_LIMIT = 512


def truncate_log_text(text, limit: int = DEFAULT_LOG_TEXT_LIMIT) -> str:
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}... [truncated {omitted} chars; total {len(text)}]"


def redact_url_for_log(value) -> str:
    """Return an origin/path-only URL safe for request logs.

    Query strings can contain opaque cursors, user IDs, search terms, and
    signed request material. User info and fragments are also excluded.
    """
    try:
        parsed = urlsplit(str(value or ""))
        hostname = parsed.hostname or ""
        if not hostname:
            return parsed.path or "<invalid-url>"
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        return f"{scheme}{hostname}{port}{parsed.path or '/'}"
    except Exception:
        return "<invalid-url>"
