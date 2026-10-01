"""Integration tests against a running relay over real HTTP.

Set RELAY_BASE_URL (for example http://127.0.0.1:8000) to the relay under
test; without it these tests are skipped. Unlike test_agent_relay.py they do
not touch the database directly, so they can run against any deployment:
uvicorn in CI, Docker Compose, or the kind cluster.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest

from worker import run_worker

BASE_URL = os.getenv("RELAY_BASE_URL")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="RELAY_BASE_URL is not set")


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL or "", timeout=40) as http:
        yield http


def register(client: httpx.Client, name: str) -> tuple[str, str, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": f"{name}-{uuid.uuid4().hex[:6]}"})
    assert response.status_code == 201, response.text
    data = response.json()
    return data["agent_id"], data["token"], {"Authorization": f"Bearer {data['token']}"}


def test_relay_is_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_two_agents_exchange_task_and_result(client):
    sender_id, _, sender = register(client, "sender")
    reviewer_id, _, reviewer = register(client, "reviewer")

    sent = client.post(
        "/api/v1/tasks", headers=sender, json={"to": reviewer_id, "input": "Is 2+2=5 correct?"}
    )
    assert sent.status_code == 201
    assert sent.json()["status"] == "queued"
    task_id = sent.json()["task_id"]

    claim = client.post("/api/v1/tasks/claim", headers=reviewer, json={"worker_id": "ci", "wait_seconds": 5})
    assert claim.status_code == 200
    claim_data = claim.json()
    assert claim_data["task_id"] == task_id
    assert claim_data["from"] == sender_id
    assert claim_data["attempt"] == 1

    done = client.post(
        f"/api/v1/tasks/{task_id}/complete",
        headers=reviewer,
        json={"claim_token": claim_data["claim_token"], "output": "No. 2+2=4."},
    )
    assert done.status_code == 200

    result = client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()
    assert result["status"] == "completed"
    assert result["from"] == sender_id
    assert result["to"] == reviewer_id
    assert result["output"] == "No. 2+2=4."
    assert result["attempt_count"] == 1

    # A third agent can't read the pair's task.
    _, _, outsider = register(client, "outsider")
    assert client.get(f"/api/v1/tasks/{task_id}", headers=outsider).status_code == 404


def test_bundled_worker_completes_queued_tasks(client):
    _, _, sender = register(client, "sender")
    worker_id, worker_token, _ = register(client, "uppercase")
    task_ids = [
        client.post("/api/v1/tasks", headers=sender, json={"to": worker_id, "input": f"job {i}"}).json()["task_id"]
        for i in range(3)
    ]

    asyncio.run(
        asyncio.wait_for(
            run_worker(BASE_URL or "", worker_id, worker_token, "ci-worker", wait_seconds=2, stop_after=3),
            timeout=60,
        )
    )

    for index, task_id in enumerate(task_ids):
        task = client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()
        assert task["status"] == "completed"
        assert task["output"] == f"JOB {index}"
