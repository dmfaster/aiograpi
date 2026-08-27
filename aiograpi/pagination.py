from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Optional

from aiograpi.types import UserShort

UserListRoute = Literal["private_v1", "private_graphql"]

_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")

_CURSOR_PATHS = (
    ("next_max_id",),
    ("max_id",),
    ("paging_info", "max_id"),
    ("paging_info", "next_max_id"),
    ("page_info", "end_cursor"),
    ("page_info", "next_max_id"),
)
_HAS_MORE_PATHS = (
    ("has_more",),
    ("has_next_page",),
    ("paging_info", "has_more"),
    ("paging_info", "has_next_page"),
    ("page_info", "has_more"),
    ("page_info", "has_next_page"),
)


@dataclass(frozen=True, slots=True)
class UserListPage:
    """One upstream follow-list page plus non-sensitive pagination metadata.

    ``next_cursor`` is intentionally kept separate from the safe shape fields.
    Callers may persist the cursor as an opaque checkpoint, but must never log it.
    ``response_keys`` and ``root_keys`` contain field names only, never values.
    """

    users: list[UserShort]
    next_cursor: str
    cursor_field: str
    has_more: Optional[bool]
    route: UserListRoute
    response_keys: tuple[str, ...]
    root_keys: tuple[str, ...]
    raw_user_count: int
    http_status: int
    response_bytes: int
    page_size: Optional[int] = None
    big_list: Optional[bool] = None
    should_limit_list_of_followers: Optional[bool] = None


def safe_mapping_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    keys = {key for key in value if isinstance(key, str) and _SAFE_FIELD_NAME.fullmatch(key)}
    return tuple(sorted(keys))[:80]


def normalize_collection_cursor(payload: object) -> tuple[str, str]:
    for path in _CURSOR_PATHS:
        value = _mapping_path(payload, path)
        if value is None or isinstance(value, bool):
            continue
        normalized = str(value).strip()
        if normalized and normalized.casefold() not in {"0", "null", "none"}:
            return normalized, ".".join(path)
    return "", ""


def normalize_collection_has_more(payload: object, *, next_cursor: str) -> Optional[bool]:
    for path in _HAS_MORE_PATHS:
        normalized = _optional_bool(_mapping_path(payload, path))
        if normalized is not None:
            return normalized
    if next_cursor:
        return True
    return None


def normalize_collection_page_size(payload: object) -> Optional[int]:
    """Return a bounded, non-sensitive page-size hint when Instagram exposes one."""
    value = _mapping_path(payload, ("page_size",))
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if 0 <= normalized <= 10_000 else None


def normalize_collection_bool(payload: object, field: str) -> Optional[bool]:
    """Read one allowlisted collection boolean without retaining response data."""
    if field not in {"big_list", "should_limit_list_of_followers"}:
        raise ValueError("unsupported collection boolean")
    return _optional_bool(_mapping_path(payload, (field,)))


def response_observability(response: object) -> tuple[int, int]:
    raw_status = getattr(response, "status_code", 0)
    try:
        status = int(raw_status or 0)
    except (TypeError, ValueError):
        status = 0
    if status < 100 or status > 599:
        status = 0

    content = getattr(response, "content", b"")
    if isinstance(content, str):
        size = len(content.encode())
    elif isinstance(content, (bytes, bytearray, memoryview)):
        size = len(content)
    else:
        size = 0
    return status, size


def _mapping_path(payload: object, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _optional_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None
