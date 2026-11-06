from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mkbot.api.app import create_app
from mkbot.cli import admin_token, main

TOKEN = "test-token-local-000000000000"


@pytest.fixture
def client(tmp_path, clock):
    with TestClient(create_app(tmp_path / "api", TOKEN, clock=clock)) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client


def test_all_management_routes_require_auth(client):
    client.headers.pop("Authorization")
    for path in (
        "status",
        "inbox",
        "activities",
        "deliveries",
        "audit",
        "usage",
        "config/persona",
        "memory",
        "config/memory",
        "scheduler",
        "statistics",
    ):
        assert client.get(f"/api/{path}").status_code == 401
    assert client.get("/healthz").json()["status"] == "ok"
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert TOKEN not in response.text
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    assert client.get("/api/status").status_code == 200


def test_sleep_receive_wake_and_selective_review(client, clock):
    wake_at = (clock.now() + timedelta(hours=1)).isoformat()
    response = client.post(
        "/api/lifecycle/sleep",
        json={
            "earliest_wake_at": wake_at,
            "latest_wake_at": wake_at,
        },
    )
    assert response.status_code == 200
    assert client.get("/api/status").json()["phase"] == "sleeping"
    response = client.post(
        "/api/inbox",
        json={
            "source": "test",
            "external_id": "a",
            "conversation_id": "private/alice",
            "sender_id": "alice",
            "text": "随便聊聊",
            "occurred_at": clock.now().isoformat(),
        },
    )
    assert response.status_code == 201
    messages = client.get("/api/inbox").json()
    assert len(messages) == 1
    event_id = messages[0]["id"]
    decision = {"event_ids": [event_id], "decision": "no_reply", "reason": "无需逐条回复"}
    assert client.post("/api/inbox/review", json=decision).status_code == 423
    assert client.post("/api/lifecycle/wake").status_code == 200
    assert client.post("/api/lifecycle/awake").json()["phase"] == "awake"
    assert client.post("/api/inbox/review", json=decision).status_code == 200
    assert client.get("/api/inbox").json()[0]["decision"] == "no_reply"
    assert client.get("/api/deliveries").json() == []


def test_configuration_revision_protects_concurrent_edit(client):
    config = client.get("/api/config/persona").json()
    update = {"expected_revision": config["revision"], "content": {"name": "大肥鱼"}}
    assert client.put("/api/config/persona", json=update).status_code == 200
    assert client.put("/api/config/persona", json=update).status_code == 409
    assert len(client.get("/api/config/persona/history").json()) == 2
    assert client.get("/api/config/unknown").status_code == 404


def test_scheduler_validation_status_and_statistics_ranges(client):
    config = client.get("/api/config/scheduler").json()
    assert not config["content"]["enabled"]
    payload = {
        "expected_revision": config["revision"],
        "content": config["content"] | {"max_batch_events": 0},
    }
    assert client.put("/api/config/scheduler", json=payload).status_code == 422
    payload["content"]["max_batch_events"] = 20
    assert client.put("/api/config/scheduler", json=payload).status_code == 200
    assert client.put("/api/config/scheduler", json=payload).status_code == 409
    assert client.get("/api/scheduler").json()["jobs"] == []
    assert client.post("/api/scheduler/absent/resume").status_code == 404
    assert client.get("/api/statistics?hours=24").json()["summary"]["calls"] == 0
    assert client.get("/api/statistics?hours=168").status_code == 200
    assert client.get("/api/statistics?hours=720").status_code == 200
    assert client.get("/api/statistics?hours=72").status_code == 422


def test_invalid_timestamp_and_transition_have_clear_errors(client):
    assert (
        client.post(
            "/api/lifecycle/sleep",
            json={
                "earliest_wake_at": "2026-11-07T00:00:00",
                "latest_wake_at": "2026-11-07T00:00:00",
            },
        ).status_code
        == 422
    )
    assert client.post("/api/lifecycle/awake").json()["phase"] == "awake"
    assert (
        client.post(
            "/api/inbox/review",
            json={
                "event_ids": ["fake"],
                "decision": "defer",
                "reason": "稍后处理",
            },
        ).status_code
        == 422
    )


def test_manual_reconciliation_cannot_finish_live_work(client):
    kernel = client.app.state.kernel
    work_id = kernel.lifecycle.start_work("tool")
    endpoint = f"/api/work/{work_id}/resolve"
    payload = {"outcome": "failed", "detail": "已核对外部服务"}
    assert client.post(endpoint, json=payload).status_code == 409
    kernel.lifecycle.finish_work(work_id, "unknown")
    assert client.post(endpoint, json=payload).status_code == 200


def test_init_keeps_credentials_and_does_not_print_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MKBOT_ADMIN_TOKEN", raising=False)
    main(["--data-dir", str(tmp_path), "init"])
    token = admin_token(tmp_path)
    assert len(token) >= 32
    assert token not in capsys.readouterr().out
    main(["--data-dir", str(tmp_path), "init"])
    assert admin_token(tmp_path) == token


def test_memory_management_scope_revision_and_forget_preview(client):
    payload = {
        "kind": "preference",
        "subjects": ["qq/alice"],
        "content": "喜欢安静的咖啡馆",
        "tier": "long",
        "scope": ["private:alice"],
    }
    created = client.post("/api/memory", json=payload)
    assert created.status_code == 201
    item = created.json()
    assert item["confidence"] == "operator"
    url = f"/api/memory/{item['id']}"
    assert client.post("/api/memory/search", json={"audience": "group:friends"}).json() == []
    profile = client.post(
        "/api/memory/profile",
        json={
            "subject": "qq/alice",
            "audience": "private:alice",
        },
    ).json()
    assert profile["items"][0]["id"] == item["id"]
    assert client.get("/api/memory", params={"subject": "qq/bob"}).json() == []
    edit = {"expected_revision": 1, "use_scope": ["*"], "disclosure_scope": ["*"]}
    assert client.put(url, json=edit).status_code == 409
    edit["allow_wider_sharing"] = True
    assert client.put(url, json=edit).json()["revision"] == 2
    assert client.put(url, json=edit).status_code == 409
    assert len(client.get(url + "/history").json()) == 2
    preview = client.get(url + "/forget-preview").json()
    assert preview["affected_ids"] == [item["id"]]
    assert preview["expected_revision"] == 2
    assert client.post(url + "/forget", json={"expected_revision": 1}).status_code == 409
    forgotten = client.post(url + "/forget", json={"expected_revision": 2}).json()
    assert forgotten["original_chat_retained"]
    assert client.get(url).json()["content"] == ""
    assert len(client.get(url + "/history").json()) == 1
    assert client.app.state.kernel.db.rows("SELECT content FROM memory_sources") == [
        {"content": None}
    ]


def test_memory_manual_failure_is_atomic_and_expiry_rules_validated(client, clock):
    payload = {
        "kind": "current_state",
        "subjects": ["self"],
        "content": "正在画画",
        "scope": ["@self"],
    }
    assert client.post("/api/memory", json=payload).status_code == 422
    payload["valid_until"] = (clock.now() - timedelta(seconds=1)).isoformat()
    assert client.post("/api/memory", json=payload).status_code == 422
    assert client.app.state.kernel.db.rows("SELECT * FROM memory_sources") == []
    payload["valid_until"] = (clock.now() + timedelta(hours=1)).isoformat()
    item = client.post("/api/memory", json=payload).json()
    assert (
        client.put(
            f"/api/memory/{item['id']}",
            json={
                "expected_revision": 1,
                "valid_until": None,
            },
        ).status_code
        == 422
    )
    assert client.get("/api/memory/absent/history").status_code == 404
    config = client.get("/api/config/memory").json()
    config["content"]["daily_reward_limit"] = -1
    assert (
        client.put(
            "/api/config/memory",
            json={
                "expected_revision": config["revision"],
                "content": config["content"],
            },
        ).status_code
        == 422
    )
