# SPDX-License-Identifier: Apache-2.0
"""Fixtures over the fakes of `fakes.py` (a loopback ntfy server; nothing reaches the network)."""

from collections.abc import Iterator

import pytest

from fakes import FakeNtfy


@pytest.fixture
def ntfy() -> Iterator[FakeNtfy]:
    with FakeNtfy() as server:
        yield server


@pytest.fixture(autouse=True)
def instance_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host always sets `TZ` (the instance zone) for a plugin; notifiers built on the
    process environment get it too. Tests of a missing zone pass their own environment."""
    monkeypatch.setenv("TZ", "Europe/Paris")
