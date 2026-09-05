from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from app.infra.config import get_settings
from ..sandbox import run_sandboxed


IMAGE_CAPTURE_SUFFIX = r"""
try:
    import base64 as _b64, io as _io
    import matplotlib.pyplot as _plt
    _imgs = []
    for _num in _plt.get_fignums():
        _fig = _plt.figure(_num)
        _buf = _io.BytesIO()
        _fig.savefig(_buf, format="png", dpi=90, bbox_inches="tight")
        _buf.seek(0)
        _imgs.append(_b64.b64encode(_buf.read()).decode("ascii"))
        _plt.close(_fig)
    if _imgs:
        print("__IMAGES_B64__:" + "||".join(_imgs))
except Exception:
    pass
"""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class CodeExecutor:
    def __init__(self, timeout: int | None = None) -> None:
        self.timeout = timeout or get_settings().code_exec_timeout

    async def execute(
        self,
        code: str,
        language: str = "python",
        workspace_dir: str | os.PathLike | None = None,
    ) -> dict:
        if language.lower() not in {"python", "py"}:
            return {"stdout": "", "stderr": f"Unsupported language: {language}", "exit_code": -1, "images": []}

        temporary_root = None
        if workspace_dir is None:
            execution_root = Path(tempfile.mkdtemp(prefix="commchat-code-")).resolve()
            temporary_root = execution_root
        else:
            execution_root = Path(workspace_dir).resolve()
            if not execution_root.is_dir():
                raise ValueError("Code execution workspace does not exist")
        descriptor, script_name = tempfile.mkstemp(
            prefix=".commchat-exec-",
            suffix=".py",
            dir=execution_root,
        )
        os.close(descriptor)
        script = Path(script_name)
        with script.open("w", encoding="utf-8") as handle:
            handle.write(self._wrap(code))

        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["MPLBACKEND"] = "Agg"

            def _run():
                return run_sandboxed(
                    [sys.executable, str(script)],
                    cwd=execution_root,
                    env=env,
                    timeout=self.timeout,
                )

            proc = await asyncio.to_thread(_run)
            if proc.timed_out:
                return {"stdout": "", "stderr": f"Execution timed out after {self.timeout}s", "exit_code": -1, "images": []}
            return self._parse(proc.stdout, proc.stderr, proc.returncode)
        finally:
            script.unlink(missing_ok=True)
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)

    def _wrap(self, code: str) -> str:
        return (
            "import os, sys, warnings\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "sys.stderr.reconfigure(encoding='utf-8')\n"
            "warnings.filterwarnings('ignore')\n"
            "os.environ.setdefault('MPLBACKEND', 'Agg')\n"
            "try:\n"
            "    import matplotlib\n"
            "    matplotlib.use('Agg')\n"
            "except Exception:\n"
            "    pass\n\n"
            + code
            + "\n"
            + IMAGE_CAPTURE_SUFFIX
        )

    @staticmethod
    def _parse(stdout: bytes, stderr: bytes, exit_code: int) -> dict:
        stdout_text = _decode(stdout)
        stderr_text = _decode(stderr)[:10000]
        images: list[str] = []
        match = re.search(r"__IMAGES_B64__:([A-Za-z0-9+/=|]+)", stdout_text)
        if match:
            images = [item for item in match.group(1).split("||") if item]
            stdout_text = stdout_text[: match.start()] + stdout_text[match.end():]
        return {
            "stdout": stdout_text.strip()[:20000],
            "stderr": stderr_text.strip(),
            "exit_code": int(exit_code),
            "images": images,
        }
