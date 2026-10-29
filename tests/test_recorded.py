# SPDX-License-Identifier: Apache-2.0
"""Recorded pushes, replayed through MCP as the core's host calls `ntfy.apply`.

`horae_sdk.testing.replay` replays source and transform calls only; a notifier is driven
through its sink operations, so each case of `tests/fixtures/pushes.json` (synthetic
parameters, intent and clock) is sent with `horae_sdk.testing.operation` and its `expect`
holds what the fake ntfy server must receive (`request`, `null` when nothing is sent) and
what the core gets back (`result`). `HORAE_SDK_RECORD=1 uv run pytest` rewrites `expect`
(review the diff); record mode is refused in CI, like the SDK's own."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from horae_sdk.testing import CI_ENVS, RECORD_ENV, operation

from fakes import TOKEN, Recorder, recorded_notifier
from horae_ntfy import TOOL, build

FIXTURES = Path(__file__).parent / "fixtures" / "pushes.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURES.read_text("utf-8"))


def recording() -> bool:
    if not os.environ.get(RECORD_ENV):
        return False
    if any(os.environ.get(name) for name in CI_ENVS):
        raise RuntimeError("record mode is refused in CI")
    return True


def replay(case: dict[str, Any]) -> dict[str, Any]:
    recorder = Recorder()
    notifier = recorded_notifier(recorder, now=datetime.fromisoformat(case["now"]))
    plugin = build(notifier)
    arguments = {
        "paths": {"read": [], "write": []},
        "intent": case["intent"],
        "tool": TOOL,
        "params": case["params"],
        "run_id": "run-synthetic",
        "index": 0,
        "dedupe_key": "0" * 64,
    }
    result = anyio.run(operation, plugin, f"{TOOL.split('.')[0]}.apply", arguments)
    request: dict[str, Any] | None = None
    if recorder.requests:
        [sent] = recorder.requests
        assert sent.headers["authorization"] == f"Bearer {TOKEN}"
        request = {"url": str(sent.url), "json": recorder.json()}
    return {"request": request, "result": result}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_recorded_push(case: dict[str, Any]) -> None:
    actual = replay(case)
    assert TOKEN not in json.dumps(actual)
    if recording():
        case["expect"] = actual
        return
    assert actual == case["expect"]


def test_write_recordings() -> None:
    """Runs last (file order): writes the rewritten expectations in record mode."""
    if recording():
        FIXTURES.write_text(json.dumps(CASES, indent=2) + "\n", "utf-8")
    assert len(CASES) >= 3
