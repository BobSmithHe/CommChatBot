from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from ..infra.config import get_settings


@dataclass
class SandboxResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


class ProcessLimitHandle:
    def __init__(self, process: subprocess.Popen) -> None:
        self._handle = None
        if os.name == "nt":
            self._attach_windows(process)

    def _attach_windows(self, process: subprocess.Popen) -> None:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        settings = get_settings()
        limits = EXTENDED_LIMIT()
        # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS | PROCESS_MEMORY | PROCESS_TIME
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x0008 | 0x0100 | 0x0002
        limits.BasicLimitInformation.ActiveProcessLimit = max(1, settings.sandbox_max_processes)
        limits.BasicLimitInformation.PerProcessUserTimeLimit = max(1, settings.sandbox_cpu_seconds) * 10_000_000
        limits.ProcessMemoryLimit = max(64, settings.sandbox_memory_mb) * 1024 * 1024
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(job)
            return
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
            kernel32.CloseHandle(job)
            return
        self._handle = job

    def close(self) -> None:
        if self._handle and os.name == "nt":
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            self._handle = None


def sandbox_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(environment or os.environ)
    settings = get_settings()
    env["COMMCHAT_SANDBOX"] = settings.sandbox_mode
    if settings.sandbox_network.casefold() == "deny":
        # Covers package managers and standard HTTP clients. Agent command
        # allowlists remain the primary boundary for direct socket access.
        env.update({
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
        })
    return env


DOCKER_ENV_ALLOWLIST = {
    "LANG", "LC_ALL", "TERM", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
    "MPLBACKEND", "NODE_ENV", "CI", "NO_COLOR", "FORCE_COLOR",
    "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
}


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_container_name(prefix: str = "commchat-run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def docker_run_argv(
    *,
    workspace: str | os.PathLike,
    name: str,
    command: list[str],
    env: dict[str, str] | None = None,
    interactive: bool = False,
) -> list[str]:
    """Build a hardened Docker CLI invocation for one workspace.

    The Docker socket is never exposed to the container. Only the requested
    workspace is bind-mounted, while the image root remains read-only.
    """
    settings = get_settings()
    root = Path(workspace).resolve()
    network = "none" if settings.sandbox_network.casefold() == "deny" else "bridge"
    args = [
        shutil.which("docker") or "docker",
        "run", "--rm", "--name", name, "--hostname", "commchat-sandbox",
        "--network", network,
        "--memory", f"{max(64, settings.sandbox_memory_mb)}m",
        "--pids-limit", str(max(1, settings.sandbox_max_processes)),
        "--cpus", str(max(0.1, settings.sandbox_cpus)),
        "--ulimit", f"cpu={max(1, settings.sandbox_cpu_seconds)}",
        "--ulimit", "nofile=256:256",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size={max(32, settings.sandbox_tmpfs_mb)}m",
        "--mount", f"type=bind,source={root},target=/workspace",
        "--workdir", "/workspace",
        "--user", "65534:65534",
    ]
    if interactive:
        args.extend(["-i", "-t"])
    clean_env = {
        "HOME": "/tmp",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "COMMCHAT_SANDBOX": "docker",
        # Scientific libraries otherwise try to consume one PID per host CPU,
        # which conflicts with the deliberately small container PID budget.
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    clean_env.update({key: value for key, value in (env or {}).items() if key in DOCKER_ENV_ALLOWLIST})
    for key, value in clean_env.items():
        args.extend(["--env", f"{key}={value}"])
    args.extend([settings.sandbox_image, *command])
    return args


def remove_docker_container(name: str) -> None:
    subprocess.run(
        [shutil.which("docker") or "docker", "rm", "-f", name],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _docker_command(argv: list[str], cwd: Path) -> list[str]:
    translated: list[str] = []
    for index, value in enumerate(argv):
        candidate = Path(value)
        if index == 0:
            executable = candidate.name.casefold()
            if executable in {"python", "python.exe", "python3", "python3.exe"}:
                translated.append("python3")
            else:
                translated.append(candidate.name if candidate.is_absolute() else value)
            continue
        if candidate.is_absolute():
            try:
                relative = candidate.resolve().relative_to(cwd)
            except (OSError, ValueError):
                translated.append(value)
            else:
                translated.append((Path("/workspace") / relative).as_posix())
        else:
            translated.append(value)
    return translated


def _run_in_docker(
    argv: list[str], *, cwd: Path, timeout: int,
    env: dict[str, str] | None, input_data: bytes | None,
) -> SandboxResult:
    if not docker_available():
        return SandboxResult(-1, b"", b"Docker CLI is not available")
    name = docker_container_name()
    command = docker_run_argv(
        workspace=cwd,
        name=name,
        command=_docker_command(argv, cwd),
        env=env,
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            remove_docker_container(name)
            process.kill()
            stdout, stderr = process.communicate()
            timed_out = True
        cap = max(64_000, get_settings().sandbox_max_output_bytes)
        return SandboxResult(process.returncode if not timed_out else -1, stdout[-cap:], stderr[-cap:], timed_out)
    finally:
        if process.poll() is None:
            remove_docker_container(name)


def run_sandboxed(
    argv: list[str], *, cwd: str | os.PathLike, timeout: int,
    env: dict[str, str] | None = None, input_data: bytes | None = None,
    creationflags: int = 0,
) -> SandboxResult:
    resolved_cwd = Path(cwd).resolve()
    if get_settings().sandbox_mode.casefold() == "docker":
        return _run_in_docker(
            argv,
            cwd=resolved_cwd,
            timeout=timeout,
            env=env,
            input_data=input_data,
        )
    process = subprocess.Popen(
        argv, cwd=resolved_cwd, env=sandbox_environment(env), stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, creationflags=creationflags,
    )
    limits = ProcessLimitHandle(process)
    try:
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            timed_out = True
        cap = max(64_000, get_settings().sandbox_max_output_bytes)
        return SandboxResult(process.returncode if not timed_out else -1, stdout[-cap:], stderr[-cap:], timed_out)
    finally:
        limits.close()
