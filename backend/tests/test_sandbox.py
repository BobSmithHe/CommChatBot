from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.extensions.support import sandbox


def test_docker_runner_isolated_and_does_not_forward_secrets(tmp_path, monkeypatch) -> None:
    settings = SimpleNamespace(
        sandbox_network="deny",
        sandbox_memory_mb=512,
        sandbox_max_processes=8,
        sandbox_cpu_seconds=30,
        sandbox_cpus=0.5,
        sandbox_tmpfs_mb=64,
        sandbox_image="commchatbot-sandbox:test",
    )
    monkeypatch.setattr(sandbox, "get_settings", lambda: settings)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "docker")
    argv = sandbox.docker_run_argv(
        workspace=tmp_path,
        name="sandbox-test",
        command=["python3", "main.py"],
        env={"PYTHONIOENCODING": "utf-8", "DEEPSEEK_API_KEY": "must-not-leak"},
    )
    rendered = " ".join(argv)
    assert "--network none" in rendered
    assert "--read-only" in argv
    assert "--cap-drop ALL" in rendered
    assert "--security-opt no-new-privileges" in rendered
    assert "--user 65534:65534" in rendered
    assert f"source={Path(tmp_path).resolve()},target=/workspace" in rendered
    assert "PYTHONIOENCODING=utf-8" in rendered
    assert "must-not-leak" not in rendered


def test_docker_command_translates_host_python_and_workspace_path(tmp_path) -> None:
    script = tmp_path / "src" / "main.py"
    translated = sandbox._docker_command(
        [r"C:\Python\python.exe", str(script), "--flag"],
        Path(tmp_path).resolve(),
    )
    assert translated == ["python3", "/workspace/src/main.py", "--flag"]
