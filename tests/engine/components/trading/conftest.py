"""
What the trading tests share.
"""

import pytest
from engine.components.trading import brakes
from engine.helper import config


@pytest.fixture(autouse=True)
def halt_file(tmp_path, monkeypatch):
    """
    The live halt file in the test's own folder, so no test touches data/live_halt.txt.
    """
    monkeypatch.setattr(brakes, "HALT_FILE", tmp_path / "live_halt.txt")
    return brakes.HALT_FILE


@pytest.fixture(autouse=True)
def half_share(request, monkeypatch):
    """
    The trading scenarios ask for half of what the books show, so their numbers show the share at work. A test marked
    full_share runs with the configured one.
    """
    if "full_share" not in request.keywords:
        monkeypatch.setattr(config, "FILL_SHARE", 0.5)
