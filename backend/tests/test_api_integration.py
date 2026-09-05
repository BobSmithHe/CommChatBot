from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def integration_results(tmp_path_factory) -> dict:
    backend_root = Path(__file__).resolve().parents[1]
    data_dir = tmp_path_factory.mktemp("backend-integration")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_root) + os.pathsep + env.get("PYTHONPATH", "")
    env.update({
        "SQLITE_PATH": str(data_dir / "integration.db"),
        "DATABASE_BACKEND": "sqlite",
        "AGENT_WORKER_MODE": "external",
        "DATA_DIR": str(data_dir),
        "UPLOAD_DIR": str(data_dir / "uploads"),
        "KNOWLEDGE_INDEX_PATH": str(data_dir / "knowledge.json"),
        "CODING_WORKSPACE_DIR": str(data_dir / "workspaces"),
        "DEEPSEEK_API_KEY": "",
        "JWT_SECRET_KEY": "integration-test-secret-at-least-32-characters",
        "ALLOW_ANONYMOUS": "false",
        "PASSWORD_RESET_DEBUG": "true",
        "REGISTRATION_RATE_LIMIT": "50",
        "REDIS_PORT": "1",
        "DB_PORT": "1",
        "OBSERVABILITY_ENABLED": "false",
        "MEMORY_SEMANTIC_RECALL": "false",
        "REMOTE_RUNNER_TOKEN": "integration-runner-secret",
        "GITHUB_WEBHOOK_SECRET": "integration-webhook-secret",
        "SANDBOX_MODE": "local",
    })
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("integration_scenarios.py"))],
        cwd=backend_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_auth_rotation_password_reset_audit_and_rate_limit(integration_results: dict) -> None:
    assert integration_results["auth"] == "passed"


def test_approval_resolution(integration_results: dict) -> None:
    assert integration_results["approval"] == "passed"


def test_restart_recovers_running_tasks(integration_results: dict) -> None:
    assert integration_results["restart"] == "passed"


def test_concurrent_authenticated_requests(integration_results: dict) -> None:
    assert integration_results["concurrency"] == "passed"


def test_external_worker_recovers_and_completes_task(integration_results: dict) -> None:
    assert integration_results["external_worker_recovery"] == "passed"


def test_conpty_websocket_input_and_resize(integration_results: dict) -> None:
    assert integration_results["terminal_websocket"] == "passed"


def test_persistent_mailbox_conversation_branches_and_model_catalog(integration_results: dict) -> None:
    assert integration_results["framework_features"] == "passed"


def test_automations_notifications_and_remote_runner_protocol(integration_results: dict) -> None:
    assert integration_results["platform"] == "passed"
