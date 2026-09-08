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
import tempfile
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
                    container_name = ""
                    if args.mode == "docker":
                        container_name = f"commchat-runner-{job['id']}"
                        command = [
                            "docker", "run", "--rm", "--init", "--name", container_name,
                            "--network", args.network,
                            "--memory", args.memory, "--cpus", args.cpus,
                            "--pids-limit", str(max(16, args.pids_limit)),
                            "-v", f"{workdir}:/workspace", "-w", "/workspace",
                            args.image, *job["argv"],
                        ]
                    timeout = max(1, min(int(job.get("timeout", 300)), 3600))
                    deadline = time.monotonic() + timeout
                    next_heartbeat = time.monotonic() + 10
                    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as output_file:
                        process = subprocess.Popen(
                            command, cwd=str(workdir), stdout=output_file, stderr=subprocess.STDOUT,
                            text=True, shell=False,
                        )
                        cancelled = False
                        timed_out = False
                        while process.poll() is None:
                            now = time.monotonic()
                            if now >= deadline:
                                timed_out = True
                                break
                            if now >= next_heartbeat:
                                lease = client.post(
                                    f"/api/platform/remote/runner/jobs/{job['id']}/heartbeat",
                                    json={"runner_id": runner_id, "lease_token": job["lease_token"]},
                                )
                                lease.raise_for_status()
                                if lease.json().get("action") == "cancel":
                                    cancelled = True
                                    break
                                next_heartbeat = now + 10
                            time.sleep(0.25)
                        if process.poll() is None:
                            if container_name:
                                subprocess.run(
                                    ["docker", "stop", "-t", "2", container_name],
                                    capture_output=True, timeout=10, check=False,
                                )
                            else:
                                process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                        output_file.seek(0)
                        output = output_file.read()
                        exit_code = process.returncode if not (cancelled or timed_out) else -1
                        if cancelled:
                            output = "Remote command cancelled\n" + output
                        elif timed_out:
                            output = "Remote command timed out\n" + output
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    output = f"Remote runner failed to start or monitor command: {type(exc).__name__}: {exc}"
                    exit_code = -1
                client.post(
                    f"/api/platform/remote/runner/jobs/{job['id']}/complete",
                    json={
                        "runner_id": runner_id, "lease_token": job["lease_token"],
                        "output": output[-2_000_000:], "exit_code": exit_code,
                    },
                ).raise_for_status()
            except (httpx.HTTPError, OSError, KeyError, ValueError) as exc:
                print(f"runner retry: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(5)


if __name__ == "__main__":
    main()
