from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile

from ...infra.config import get_settings


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

    async def execute(self, code: str, language: str = "python") -> dict:
        if language.lower() not in {"python", "py"}:
            return {"stdout": "", "stderr": f"Unsupported language: {language}", "exit_code": -1, "images": []}

        tmpdir = tempfile.mkdtemp(prefix="commchat-code-")
        script = os.path.join(tmpdir, "main.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(self._wrap(code))

        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["MPLBACKEND"] = "Agg"

            def _run():
                return subprocess.run(
                    [sys.executable, script],
                    cwd=tmpdir,
                    env=env,
                    capture_output=True,
                    timeout=self.timeout,
                )

            proc = await asyncio.to_thread(_run)
            return self._parse(proc.stdout, proc.stderr, proc.returncode)
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"Execution timed out after {self.timeout}s", "exit_code": -1, "images": []}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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

