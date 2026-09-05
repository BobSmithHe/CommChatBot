from __future__ import annotations

import json
import hashlib
import hmac
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app.platform.database import init_db
from app.main import app


def register_and_login(client: TestClient, prefix: str) -> tuple[dict, dict]:
    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    account = {"username": username, "password": "ValidPass123!"}
    response = client.post("/api/auth/register", json={**account, "email": f"{username}@example.test"})
    assert response.status_code == 200, response.text
    session = client.post("/api/auth/login", json=account)
    assert session.status_code == 200, session.text
    return account, session.json()


def headers(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def auth_scenario(client: TestClient) -> None:
    account, session = register_and_login(client, "security")
    def rotate_once(_index: int):
        return client.post("/api/auth/refresh", json={"refresh_token": session["refresh_token"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        rotations = list(pool.map(rotate_once, range(2)))
    assert sorted(response.status_code for response in rotations) == [200, 401]
    new_session = next(response.json() for response in rotations if response.status_code == 200)
    assert new_session["refresh_token"] != session["refresh_token"]
    assert client.post("/api/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401
    forgot = client.post("/api/auth/password/forgot", json={"identity": account["username"]})
    assert forgot.status_code == 200
    reset = client.post("/api/auth/password/reset", json={
        "reset_token": forgot.json()["reset_token"], "new_password": "ChangedPass456!",
    })
    assert reset.status_code == 200
    assert client.post("/api/auth/login", json=account).status_code == 401
    logged_in = client.post("/api/auth/login", json={
        "username": account["username"], "password": "ChangedPass456!",
    })
    assert logged_in.status_code == 200
    audit = client.get("/api/auth/audit", headers=headers(logged_in.json()))
    events = {item["event"] for item in audit.json()["events"]}
    assert {"login", "refresh", "password-reset-request", "password-reset"} <= events
    limited_name = "limited_" + uuid.uuid4().hex[:8]
    statuses = [client.post("/api/auth/login", json={
        "username": limited_name, "password": "bad-password",
    }).status_code for _ in range(9)]
    assert statuses[-1] == 429


def approval_scenario(client: TestClient) -> None:
    from app.infra.config import get_settings

    _account, session = register_and_login(client, "approval")
    auth = headers(session)
    conversation = client.post("/api/conversations", headers=auth, json={"mode": "coding-agent"}).json()
    me = client.get("/api/auth/me", headers=auth).json()
    task_id, approval_id = uuid.uuid4().hex, uuid.uuid4().hex
    with sqlite3.connect(get_settings().sqlite_path) as db:
        db.execute(
            "INSERT INTO agent_tasks (id,user_id,conversation_id,mode,prompt,status) VALUES (?,?,?,?,?,?)",
            (task_id, me["id"], conversation["id"], "coding-agent", "test approval", "waiting-approval"),
        )
        db.execute(
            "INSERT INTO approval_requests (id,task_id,tool_name,tool_input_json,capability,status) VALUES (?,?,?,?,?,?)",
            (approval_id, task_id, "run_command", '{"command":"echo ok"}', "execute", "pending"),
        )
        db.commit()
    response = client.post(f"/api/tasks/{task_id}/approvals/{approval_id}", headers=auth, json={"approved": True})
    assert response.status_code == 200, response.text
    task = client.get(f"/api/tasks/{task_id}", headers=auth).json()
    assert task["status"] == "running"
    assert task["approvals"][0]["status"] == "approved"


def restart_scenario(client: TestClient) -> None:
    from app.infra.config import get_settings

    _account, session = register_and_login(client, "restart")
    auth = headers(session)
    conversation = client.post("/api/conversations", headers=auth, json={"mode": "coding-agent"}).json()
    me = client.get("/api/auth/me", headers=auth).json()
    task_id, approval_id = uuid.uuid4().hex, uuid.uuid4().hex
    with sqlite3.connect(get_settings().sqlite_path) as db:
        db.execute(
            "INSERT INTO agent_tasks (id,user_id,conversation_id,mode,prompt,checkpoint_json,status) VALUES (?,?,?,?,?,?,?)",
            (task_id, me["id"], conversation["id"], "coding-agent", "resume me", '{"version":1}', "waiting-approval"),
        )
        db.execute(
            "INSERT INTO approval_requests (id,task_id,tool_name,tool_input_json,capability,status) VALUES (?,?,?,?,?,?)",
            (approval_id, task_id, "write_file", "{}", "write", "pending"),
        )
        db.commit()
    init_db()
    task = client.get(f"/api/tasks/{task_id}", headers=auth).json()
    assert task["status"] == "queued"
    assert task["approvals"][0]["status"] == "expired"


def concurrency_scenario(client: TestClient) -> None:
    _account, session = register_and_login(client, "concurrent")
    auth = headers(session)

    def create_one(_: int) -> int:
        response = client.post("/api/conversations", headers=auth, json={"mode": "chatbot"})
        assert response.status_code == 200, response.text
        return response.json()["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create_one, range(12)))
    assert len(set(ids)) == 12


def external_worker_recovery_scenario(client: TestClient) -> None:
    """A separate worker must recover and finish an API task after process loss."""
    from app.platform.database import AgentTask, Message, SessionLocal

    _account, session = register_and_login(client, "worker")
    auth = headers(session)
    conversation = client.post("/api/conversations", headers=auth, json={"mode": "chatbot"}).json()
    me = client.get("/api/auth/me", headers=auth).json()
    task_id = uuid.uuid4().hex
    request = {
        "model_message": "continue after restart",
        "history": [],
        "use_rag": False,
        "use_web": False,
        "system_context": None,
    }
    checkpoint = {
        "version": 1,
        "next_turn": 0,
        "messages": [{"type": "user", "content": "continue after restart"}],
    }
    with SessionLocal() as db:
        db.add(AgentTask(
            id=task_id,
            user_id=me["id"],
            conversation_id=conversation["id"],
            mode="chatbot",
            prompt="continue after restart",
            status="running",
            request_json=json.dumps(request),
            checkpoint_json=json.dumps(checkpoint),
        ))
        db.commit()

    worker = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        status = "running"
        while time.monotonic() < deadline:
            response = client.get(f"/api/tasks/{task_id}", headers=auth)
            assert response.status_code == 200, response.text
            status = response.json()["status"]
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert status == "completed"
        with SessionLocal() as db:
            answers = db.query(Message).filter(
                Message.conversation_id == conversation["id"], Message.role == "assistant"
            ).all()
            assert answers and "continue after restart" in answers[-1].content
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()


def terminal_websocket_scenario(client: TestClient) -> None:
    _account, session = register_and_login(client, "terminal")
    auth = headers(session)
    conversation = client.post("/api/conversations", headers=auth, json={"mode": "coding-agent"}).json()
    terminal = client.post(
        f"/api/conversations/{conversation['id']}/workspace/terminals", headers=auth, json={}
    ).json()
    url = f"/api/conversations/{conversation['id']}/workspace/terminals/{terminal['id']}/ws"
    with client.websocket_connect(url) as websocket:
        websocket.send_json({
            "type": "auth", "token": session["access_token"], "cols": 121, "rows": 37,
        })
        ready = False
        while not ready:
            message = websocket.receive_json()
            ready = message["type"] == "ready"
        websocket.send_json({
            "type": "input",
            "data": "printf 'websocket-pty-ok\\n'\r" if terminal["backend"] == "docker" else "Write-Output 'websocket-pty-ok'\r",
        })
        output = ""
        while "websocket-pty-ok" not in output:
            message = websocket.receive_json()
            if message["type"] == "output":
                output += message["data"]
        websocket.send_json({"type": "resize", "cols": 143, "rows": 45})
        resized = False
        while not resized:
            message = websocket.receive_json()
            resized = message.get("type") == "resize" and message.get("cols") == 143 and message.get("rows") == 45
    terminals = client.get(
        f"/api/conversations/{conversation['id']}/workspace/terminals", headers=auth
    ).json()["terminals"]
    updated = next(item for item in terminals if item["id"] == terminal["id"])
    assert updated["backend"] in {"conpty", "docker"}
    assert (updated["cols"], updated["rows"]) == (143, 45)
    client.delete(f"/api/conversations/{conversation['id']}/workspace/terminals/{terminal['id']}", headers=auth)


def framework_features_scenario(client: TestClient) -> None:
    from app.platform.database import AgentTask, Message, SessionLocal, TaskMailboxMessage

    _account, session = register_and_login(client, "framework")
    auth = headers(session)
    me = client.get("/api/auth/me", headers=auth).json()
    conversation = client.post("/api/conversations", headers=auth, json={"mode": "chatbot"}).json()
    with SessionLocal() as db:
        first = Message(user_id=me["id"], conversation_id=conversation["id"], role="user", content="root")
        db.add(first)
        db.commit()
        db.refresh(first)
        first_id = first.id

    forked = client.post(
        f"/api/conversations/{conversation['id']}/fork",
        headers=auth,
        json={"from_message_id": first_id, "branch_name": "alternative"},
    )
    assert forked.status_code == 200, forked.text
    branch = forked.json()
    assert branch["parent_conversation_id"] == conversation["id"]
    tree = client.get(f"/api/conversations/{branch['id']}/branches", headers=auth).json()
    assert {item["id"] for item in tree["branches"]} == {conversation["id"], branch["id"]}
    restored = client.post(
        f"/api/conversations/{branch['id']}/branches/{conversation['id']}/restore", headers=auth,
    )
    assert restored.status_code == 200

    task_id = uuid.uuid4().hex
    with SessionLocal() as db:
        db.add(AgentTask(
            id=task_id,
            user_id=me["id"],
            conversation_id=conversation["id"],
            mode="chatbot",
            prompt="initial",
            status="running",
        ))
        db.commit()
    queued = client.post(
        f"/api/tasks/{task_id}/messages",
        headers=auth,
        json={"kind": "follow_up", "content": "continue later"},
    )
    assert queued.status_code == 200, queued.text
    with SessionLocal() as db:
        row = db.query(TaskMailboxMessage).filter(TaskMailboxMessage.task_id == task_id).one()
        assert row.kind == "follow_up" and row.status == "pending"

    catalog = client.get("/api/models").json()
    assert {item["id"] for item in catalog["providers"]} >= {"deepseek", "anthropic"}

    settings = client.get("/api/memories/settings", headers=auth)
    assert settings.status_code == 200 and settings.json()["enabled"] is True
    created = client.post("/api/memories", headers=auth, json={
        "scope": "user",
        "key": "response language",
        "value": "Chinese",
        "category": "preference",
        "importance": 0.9,
    })
    assert created.status_code == 200, created.text
    memory = created.json()
    listed = client.get("/api/memories?scope=user&query=language", headers=auth).json()
    assert [item["id"] for item in listed["memories"]] == [memory["id"]]
    privacy = client.patch(
        "/api/memories/settings", headers=auth, json={"auto_capture": False},
    )
    assert privacy.status_code == 200 and privacy.json()["auto_capture"] is False
    assert client.delete(f"/api/memories/{memory['id']}", headers=auth).status_code == 200


def platform_scenario(client: TestClient) -> None:
    from app.platform.services.task_runtime import runtime_task_manager

    _account, session = register_and_login(client, "platform")
    auth = headers(session)
    catalog = client.get("/api/extensions").json()
    assert "chat.rag" in catalog["profiles"]["chatbot"]
    assert "coding.workspace" in catalog["profiles"]["coding"]
    conversation = client.post("/api/conversations", headers=auth, json={"mode": "chatbot"}).json()
    automation = client.post("/api/platform/automations", headers=auth, json={
        "conversation_id": conversation["id"], "name": "health report",
        "prompt": "Summarize the project health", "interval_seconds": 3600,
        "notification_policy": "completion",
    })
    assert automation.status_code == 200, automation.text
    calendar = client.post("/api/platform/automations", headers=auth, json={
        "conversation_id": conversation["id"], "name": "daily health",
        "prompt": "Daily health", "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        "timezone": "Asia/Shanghai", "notification_policy": "none",
    })
    assert calendar.status_code == 200 and calendar.json()["rrule"].startswith("FREQ=DAILY")
    invalid_calendar = client.post("/api/platform/automations", headers=auth, json={
        "conversation_id": conversation["id"], "name": "invalid timezone",
        "prompt": "Never queued", "rrule": "FREQ=DAILY", "timezone": "Mars/Olympus",
    })
    assert invalid_calendar.status_code == 422
    triggered = client.post(
        f"/api/platform/automations/{automation.json()['id']}/run", headers=auth,
    )
    assert triggered.status_code == 200 and triggered.json()["task_id"]
    runtime_task_manager.finish(triggered.json()["task_id"], "completed")
    notifications = client.get("/api/platform/notifications?unread_only=true", headers=auth).json()
    assert notifications and notifications[0]["task_id"] == triggered.json()["task_id"]
    subtask_id = runtime_task_manager.create_subtask(
        triggered.json()["task_id"], "inspect tests", "read-only", model="eval-model",
    )
    runtime_task_manager.update_subtask(
        triggered.json()["task_id"], subtask_id, "completed", result="all clear",
    )
    tree = client.get(f"/api/tasks/{triggered.json()['task_id']}/subtasks", headers=auth)
    assert tree.status_code == 200 and tree.json()[0]["result"] == "all clear"
    live_subtask_id = runtime_task_manager.create_subtask(
        triggered.json()["task_id"], "inspect runtime", "messages", model="eval-model",
        status="running", request={"task": "inspect runtime", "focus": "messages"},
    )
    runtime_task_manager.save_subtask_checkpoint(
        triggered.json()["task_id"], live_subtask_id,
        {"version": 2, "phase": "before_llm", "next_turn": 1, "messages": []},
    )
    child = client.get(
        f"/api/tasks/{triggered.json()['task_id']}/subtasks/{live_subtask_id}", headers=auth,
    )
    assert child.status_code == 200 and child.json()["has_checkpoint"] is True
    child_message = client.post(
        f"/api/tasks/{triggered.json()['task_id']}/subtasks/{live_subtask_id}/messages",
        headers=auth, json={"kind": "steering", "content": "focus on recovery"},
    )
    assert child_message.status_code == 200
    consumed = runtime_task_manager.consume_subtask_messages(
        triggered.json()["task_id"], live_subtask_id, ("steering",),
    )
    assert consumed[0]["content"] == "focus on recovery"
    interrupted = client.post(
        f"/api/tasks/{triggered.json()['task_id']}/subtasks/{live_subtask_id}/interrupt",
        headers=auth,
    )
    assert interrupted.status_code == 200 and interrupted.json()["status"] == "cancelled"

    endpoint = client.post("/api/platform/notification-endpoints", headers=auth, json={
        "kind": "email", "name": "ops", "target": "ops@example.test",
    })
    assert endpoint.status_code == 200
    assert client.get("/api/platform/notification-endpoints", headers=auth).json()[0]["name"] == "ops"
    subscription = client.post("/api/platform/github/subscriptions", headers=auth, json={
        "repository": "openai/codex",
    })
    assert subscription.status_code == 200
    webhook_body = json.dumps({
        "action": "opened", "repository": {"full_name": "openai/codex"},
        "pull_request": {"number": 42},
    }).encode()
    signature = "sha256=" + hmac.new(b"integration-webhook-secret", webhook_body, hashlib.sha256).hexdigest()
    webhook = client.post("/api/platform/github/webhook", content=webhook_body, headers={
        "X-Hub-Signature-256": signature, "X-GitHub-Delivery": uuid.uuid4().hex,
        "X-GitHub-Event": "pull_request", "Content-Type": "application/json",
    })
    assert webhook.status_code == 200 and webhook.json()["notifications"] == 1
    duplicate = client.post("/api/platform/github/webhook", content=webhook_body, headers={
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": webhook.request.headers["X-GitHub-Delivery"],
        "X-GitHub-Event": "pull_request", "Content-Type": "application/json",
    })
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    assert client.post("/api/platform/github/webhook", content=webhook_body, headers={
        "X-Hub-Signature-256": "sha256=invalid", "X-GitHub-Event": "pull_request",
    }).status_code == 401

    runner_auth = {"X-Runner-Token": "integration-runner-secret"}
    unsafe = client.post("/api/platform/remote/runner/heartbeat", headers=runner_auth, json={
        "name": "unsafe-runner", "capabilities": {"os": "test", "isolated": False},
    })
    unsafe_job = client.post("/api/platform/remote/jobs", headers=auth, json={
        "runner_id": unsafe.json()["runner_id"], "argv": ["python", "-V"], "timeout": 30,
    })
    assert unsafe_job.status_code == 409
    heartbeat = client.post("/api/platform/remote/runner/heartbeat", headers=runner_auth, json={
        "name": "ci-runner", "capabilities": {"os": "test", "isolated": True, "sandbox": "test"},
    })
    assert heartbeat.status_code == 200, heartbeat.text
    runner_id = heartbeat.json()["runner_id"]
    job = client.post("/api/platform/remote/jobs", headers=auth, json={
        "runner_id": runner_id, "argv": ["python", "-V"], "timeout": 30,
    })
    assert job.status_code == 200, job.text
    claimed = client.post(
        f"/api/platform/remote/runner/jobs/claim?runner_id={runner_id}", headers=runner_auth,
    ).json()["job"]
    assert claimed["id"] == job.json()["job_id"]
    completed = client.post(
        f"/api/platform/remote/runner/jobs/{claimed['id']}/complete",
        headers=runner_auth, json={"output": "Python 3", "exit_code": 0},
    )
    assert completed.status_code == 200 and completed.json()["status"] == "completed"
    assert client.delete(f"/api/platform/notification-endpoints/{endpoint.json()['id']}", headers=auth).status_code == 200


def main() -> None:
    results = {}
    with TestClient(app) as client:
        for name, scenario in (
            ("auth", auth_scenario), ("approval", approval_scenario),
            ("restart", restart_scenario), ("concurrency", concurrency_scenario),
            ("external_worker_recovery", external_worker_recovery_scenario),
            ("terminal_websocket", terminal_websocket_scenario),
            ("framework_features", framework_features_scenario),
            ("platform", platform_scenario),
        ):
            print(f"running integration scenario: {name}", flush=True)
            scenario(client)
            results[name] = "passed"
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
