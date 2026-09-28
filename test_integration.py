"""API integration test against a running Agent Relay and its PostgreSQL DB.

Unlike ``test_agent_relay.py`` (in-process TestClient on a scratch database),
this talks HTTP to a real server and then reads the app's own database, so it
exercises the deployed stack end to end.  Start the stack first:

    docker compose up -d --build
    uv run pytest -q test_integration.py

``RELAY_BASE_URL`` (default http://localhost:8000) selects the API and
``RELAY_IT_DATABASE_URL`` (default: the compose ``agent_relay`` database via
the loopback-published port) selects the database to verify.  The test only
adds rows; it never resets data.  Set ``RELAY_IT_TOKEN_FILE`` to save the
sender's token there so the result can be inspected in the dashboard.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

BASE_URL = os.getenv("RELAY_BASE_URL", "http://localhost:8000")
DATABASE_URL = os.getenv("RELAY_IT_DATABASE_URL", "postgresql://relay:relay@localhost:5432/agent_relay")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=10) as http:
        # Allow for a stack that was just started with `docker compose up`.
        deadline = time.monotonic() + 30
        while True:
            try:
                if http.get("/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                # In CI a skip would read as a pass, so a missing API fails.
                if os.getenv("CI"):
                    pytest.fail(f"Agent Relay is not ready at {BASE_URL}")
                pytest.skip(f"Agent Relay is not ready at {BASE_URL}")
            time.sleep(1)
        yield http


def register(client: httpx.Client, name: str) -> tuple[str, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201, response.text
    data = response.json()
    return data["agent_id"], {"Authorization": f"Bearer {data['token']}"}


def test_two_agents_exchange_a_task_and_result(client: httpx.Client):
    run = uuid.uuid4().hex[:8]
    sender_id, sender = register(client, f"it-sender-{run}")
    recipient_id, recipient = register(client, f"it-recipient-{run}")
    if token_file := os.getenv("RELAY_IT_TOKEN_FILE"):
        Path(token_file).write_text(sender["Authorization"].removeprefix("Bearer ") + "\n")

    sent = client.post(
        "/api/v1/tasks",
        headers=sender,
        json={"to": recipient_id, "input": f"hello relay {run}"},
    )
    assert sent.status_code == 201, sent.text
    task_id = sent.json()["task_id"]
    assert client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()["status"] == "queued"

    claim = client.post("/api/v1/tasks/claim", headers=recipient, json={"worker_id": "it", "wait_seconds": 0})
    assert claim.status_code == 200, claim.text
    claimed = claim.json()
    assert claimed["task_id"] == task_id
    assert claimed["from"] == sender_id
    assert client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()["status"] == "processing"

    output = claimed["input"].upper()
    complete = client.post(
        f"/api/v1/tasks/{task_id}/complete",
        headers=recipient,
        json={"claim_token": claimed["claim_token"], "output": output},
    )
    assert complete.status_code == 200, complete.text

    seen_by_sender = client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()
    assert seen_by_sender["status"] == "completed"
    assert seen_by_sender["output"] == f"HELLO RELAY {run.upper()}"
    assert seen_by_sender["attempt_count"] == 1

    # The API's answer must come from PostgreSQL: read the row back directly.
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT sender_id, recipient_id, status, output FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        attempt = conn.execute(
            "SELECT attempt_number, outcome, worker_id FROM attempts WHERE task_id = %s", (task_id,)
        ).fetchone()
    assert row == (sender_id, recipient_id, "completed", output)
    assert attempt == (1, "completed", "it")
