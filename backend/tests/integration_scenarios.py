from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app.infra.database import init_db
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
    from app.infra.database import AgentTask, Message, SessionLocal

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
    from app.infra.database import AgentTask, Message, SessionLocal, TaskMailboxMessage

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


def main() -> None:
    results = {}
    with TestClient(app) as client:
        for name, scenario in (
            ("auth", auth_scenario), ("approval", approval_scenario),
            ("restart", restart_scenario), ("concurrency", concurrency_scenario),
            ("external_worker_recovery", external_worker_recovery_scenario),
            ("terminal_websocket", terminal_websocket_scenario),
            ("framework_features", framework_features_scenario),
        ):
            print(f"running integration scenario: {name}", flush=True)
            scenario(client)
            results[name] = "passed"
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
