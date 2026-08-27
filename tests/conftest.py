import os
from pathlib import Path

import pytest

_LIVE_TEST_ROOT = Path(__file__).parent / "live"


def pytest_collection_modifyitems(items):
    """Keep broad local discovery network-dark unless live tests are explicit."""
    live_authorized = bool(os.getenv("TEST_ACCOUNTS_URL")) or os.getenv("AIOGRAPI_RUN_LIVE_TESTS") == "1"
    if live_authorized:
        return
    skip_live = pytest.mark.skip(reason="Instagram live tests require explicit live-test authorization")
    for item in items:
        try:
            item.path.relative_to(_LIVE_TEST_ROOT)
        except ValueError:
            continue
        item.add_marker(skip_live)
