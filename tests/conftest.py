# SPDX-License-Identifier: Apache-2.0
"""Fixtures over the fakes of `fakes.py` (a loopback ntfy server; nothing reaches the network)."""

from collections.abc import Iterator

import pytest

from fakes import FakeNtfy


@pytest.fixture
def ntfy() -> Iterator[FakeNtfy]:
    with FakeNtfy() as server:
        yield server
