"""Reference pull-based remote runner.

Run this on the execution host with COMMCHAT_RUNNER_TOKEN set to the same
value as REMOTE_RUNNER_TOKEN on the server.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--name", default=platform.node() or "remote-runner")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--mode", choices=("docker", "local"), default="docker")
    parser.add_argument("--image", default="commchatbot-sandbox:0.8.0")
    parser.add_argument("--network", default="none")
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--cpus", default="1")
    parser.add_argument("--pids-limit", type=int, default=64)
    args = parser.parse_args()
    token = os.getenv("COMMCHAT_RUNNER_TOKEN", "")
    if not token:
        raise SystemExit("COMMCHAT_RUNNER_TOKEN is required")
    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        raise SystemExit(f"Runner workdir does not exist: {workdir}")
    if args.mode == "docker" and not shutil.which("docker"):
        raise SystemExit("docker is required in the default isolated runner mode")
    headers = {"X-Runner-Token": token}
    runner_id = ""
    with httpx.Client(base_url=args.url, headers=headers, timeout=45) as client:
        while True:
            try:
                heartbeat = client.post("/api/platform/remote/runner/heartbeat", json={
                    "runner_id": runner_id or None, "name": args.name,
                    "capabilities": {
                        "os": platform.system(), "machine": platform.machine(),
                        "isolated": args.mode == "docker", "sandbox": args.mode,
                        "image": args.image if args.mode == "docker" else None,
                    },
                })
                heartbeat.raise_for_status()
                runner_id = heartbeat.json()["runner_id"]
                claim = client.post("/api/platform/remote/runner/jobs/claim", params={"runner_id": runner_id})
                claim.raise_for_status()
                job = claim.json().get("job")
                if not job:
                    time.sleep(2)
                    continue
                try:
                    command = job["argv"]
                    if args.mode == "docker":
                        command = [
                            "docker", "run", "--rm", "--init", "--network", args.network,
                            "--memory", args.memory, "--cpus", args.cpus,
                            "--pids-limit", str(max(16, args.pids_limit)),
                            "-v", f"{workdir}:/workspace", "-w", "/workspace",
                            args.image, *job["argv"],
                        ]
                    completed = subprocess.run(
                        command, cwd=str(workdir), capture_output=True, text=True,
                        timeout=max(1, min(int(job.get("timeout", 300)), 3600)), shell=False,
                    )
                    output = (completed.stdout or "") + (completed.stderr or "")
                    exit_code = completed.returncode
                except subprocess.TimeoutExpired as exc:
                    output = f"Remote command timed out\n{exc.stdout or ''}{exc.stderr or ''}"
                    exit_code = -1
                client.post(
                    f"/api/platform/remote/runner/jobs/{job['id']}/complete",
                    json={"output": output[-2_000_000:], "exit_code": exit_code},
                ).raise_for_status()
            except (httpx.HTTPError, OSError, KeyError, ValueError) as exc:
                print(f"runner retry: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(5)


if __name__ == "__main__":
    main()
