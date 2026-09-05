from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse


class LspUnavailable(RuntimeError):
    pass


class WorkspaceLspSession:
    """Persistent JSON-RPC/LSP client for one workspace."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._process: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._documents: dict[str, tuple[int, str]] = {}
        self._diagnostics: dict[str, list[dict]] = {}
        self._diagnostic_events: dict[str, threading.Event] = {}
        self._start()

    @property
    def alive(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def _start(self) -> None:
        node = shutil.which("node.exe") or shutil.which("node")
        pyright = Path(__file__).resolve().parents[4] / "frontend" / "node_modules" / "pyright" / "langserver.index.js"
        if not node or not pyright.is_file():
            raise LspUnavailable("Pyright language server is not installed")
        self._process = subprocess.Popen(
            [node, str(pyright), "--stdio"],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read_loop, daemon=True, name=f"pyright-{self.root.name}").start()
        root_uri = self.root.as_uri()
        self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "capabilities": {
                "textDocument": {
                    "completion": {"completionItem": {"snippetSupport": False}},
                    "definition": {}, "hover": {}, "references": {}, "rename": {},
                    "publishDiagnostics": {"relatedInformation": True},
                },
                "workspace": {"workspaceFolders": True},
            },
        }, timeout=15)
        self.notify("initialized", {})

    def sync(self, path: str, content: str) -> tuple[str, int]:
        target = self._resolve(path)
        uri = target.as_uri()
        with self._state_lock:
            existing = self._documents.get(uri)
            if existing and existing[1] == content:
                return uri, existing[0]
            version = (existing[0] + 1) if existing else 1
            self._documents[uri] = (version, content)
            event = self._diagnostic_events.setdefault(uri, threading.Event())
            event.clear()
        if existing:
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": content}],
            })
        else:
            self.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri, "languageId": self._language_id(target),
                    "version": version, "text": content,
                }
            })
        return uri, version

    def completion(self, path: str, content: str, line: int, column: int) -> list[dict]:
        uri, _ = self.sync(path, content)
        result = self.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": self._position(line, column),
        }) or []
        items = result.get("items", []) if isinstance(result, dict) else result
        return [{
            "label": item.get("label", ""),
            "insert_text": item.get("insertText") or item.get("label", ""),
            "kind": item.get("kind"),
            "detail": item.get("detail") or "",
            "documentation": self._markup(item.get("documentation")),
        } for item in items[:150]]

    def diagnostics(self, path: str, content: str) -> list[dict]:
        uri, _ = self.sync(path, content)
        event = self._diagnostic_events.setdefault(uri, threading.Event())
        event.wait(1.5)
        with self._state_lock:
            items = list(self._diagnostics.get(uri, []))
        return [self._diagnostic(item) for item in items]

    def definition(self, path: str, content: str, line: int, column: int) -> list[dict]:
        return self._locations("textDocument/definition", path, content, line, column)

    def references(self, path: str, content: str, line: int, column: int) -> list[dict]:
        return self._locations(
            "textDocument/references", path, content, line, column,
            extra={"context": {"includeDeclaration": True}},
        )

    def hover(self, path: str, content: str, line: int, column: int) -> dict | None:
        uri, _ = self.sync(path, content)
        result = self.request("textDocument/hover", {
            "textDocument": {"uri": uri}, "position": self._position(line, column),
        })
        if not result:
            return None
        return {"contents": self._markup(result.get("contents")), "range": result.get("range")}

    def rename(self, path: str, content: str, line: int, column: int, new_name: str) -> list[dict]:
        uri, _ = self.sync(path, content)
        result = self.request("textDocument/rename", {
            "textDocument": {"uri": uri}, "position": self._position(line, column), "newName": new_name,
        }) or {}
        edits = []
        for changed_uri, changes in (result.get("changes") or {}).items():
            relative = self._relative_uri(changed_uri)
            if relative is not None:
                edits.append({"path": relative, "edits": changes, "content": self._read_relative(relative, path, content)})
        for change in result.get("documentChanges") or []:
            relative = self._relative_uri((change.get("textDocument") or {}).get("uri", ""))
            if relative is not None:
                edits.append({"path": relative, "edits": change.get("edits") or [], "content": self._read_relative(relative, path, content)})
        return edits

    def _read_relative(self, relative: str, active_path: str, active_content: str) -> str:
        if relative == active_path.replace("\\", "/"):
            return active_content
        try:
            return self._resolve(relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def request(self, method: str, params: dict, timeout: float = 8) -> object:
        if not self.alive:
            raise LspUnavailable("Language server exited")
        with self._state_lock:
            self._next_id += 1
            request_id = self._next_id
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Language server request timed out: {method}") from exc
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        if response.get("error"):
            raise ValueError(response["error"].get("message") or "Language server error")
        return response.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        process = self._process
        self._process = None
        if not process:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _locations(self, method: str, path: str, content: str, line: int, column: int, extra: dict | None = None) -> list[dict]:
        uri, _ = self.sync(path, content)
        params = {"textDocument": {"uri": uri}, "position": self._position(line, column), **(extra or {})}
        result = self.request(method, params) or []
        if isinstance(result, dict):
            result = [result]
        locations = []
        for item in result:
            target_uri = item.get("uri") or (item.get("targetUri") if isinstance(item, dict) else None)
            location_range = item.get("range") or item.get("targetSelectionRange") or {}
            relative = self._relative_uri(target_uri or "")
            if relative is None:
                continue
            target = self._resolve(relative)
            try:
                target_content = content if target_uri == uri else target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                target_content = ""
            start = location_range.get("start") or {}
            locations.append({
                "path": relative, "line": int(start.get("line", 0)) + 1,
                "column": int(start.get("character", 0)) + 1, "content": target_content,
            })
        return locations[:200]

    def _read_loop(self) -> None:
        stream = self._process.stdout if self._process else None
        if stream is None:
            return
        while self.alive:
            headers = {}
            while True:
                line = stream.readline()
                if not line:
                    return
                if line in {b"\r\n", b"\n"}:
                    break
                key, _, value = line.decode("ascii", errors="replace").partition(":")
                headers[key.lower()] = value.strip()
            length = int(headers.get("content-length", "0") or 0)
            if not length:
                continue
            try:
                message = json.loads(stream.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if "id" in message and "method" not in message:
                with self._state_lock:
                    pending = self._pending.get(message.get("id"))
                if pending:
                    pending.put(message)
            elif message.get("method") == "textDocument/publishDiagnostics":
                params = message.get("params") or {}
                uri = params.get("uri", "")
                with self._state_lock:
                    self._diagnostics[uri] = params.get("diagnostics") or []
                    event = self._diagnostic_events.setdefault(uri, threading.Event())
                    event.set()

    def _send(self, message: dict) -> None:
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        process = self._process
        if not process or process.poll() is not None or process.stdin is None:
            raise LspUnavailable("Language server exited")
        with self._write_lock:
            process.stdin.write(frame)
            process.stdin.flush()

    def _resolve(self, path: str) -> Path:
        candidate = (self.root / path.replace("\\", "/")).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path escapes the workspace") from exc
        return candidate

    def _relative_uri(self, uri: str) -> str | None:
        try:
            parsed = urlparse(uri)
            path = Path(unquote(parsed.path.lstrip("/") if os.name == "nt" else parsed.path)).resolve()
            return path.relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None

    @staticmethod
    def _position(line: int, column: int) -> dict:
        return {"line": max(0, line - 1), "character": max(0, column - 1)}

    @staticmethod
    def _language_id(path: Path) -> str:
        return {".py": "python", ".js": "javascript", ".jsx": "javascriptreact", ".ts": "typescript", ".tsx": "typescriptreact"}.get(path.suffix.lower(), "plaintext")

    @staticmethod
    def _markup(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("value") or value.get("language") or "")
        if isinstance(value, list):
            return "\n\n".join(WorkspaceLspSession._markup(item) for item in value)
        return ""

    @staticmethod
    def _diagnostic(item: dict) -> dict:
        location = item.get("range") or {}
        start, end = location.get("start") or {}, location.get("end") or {}
        severity = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(item.get("severity"), "warning")
        return {
            "message": item.get("message") or "Language diagnostic", "code": item.get("code"), "severity": severity,
            "start_line": int(start.get("line", 0)) + 1, "start_column": int(start.get("character", 0)) + 1,
            "end_line": int(end.get("line", 0)) + 1, "end_column": int(end.get("character", 0)) + 1,
        }


class WorkspaceLspManager:
    def __init__(self) -> None:
        self._sessions: dict[str, WorkspaceLspSession] = {}
        self._lock = threading.Lock()

    def session(self, root: Path) -> WorkspaceLspSession:
        key = str(root.resolve()).casefold()
        with self._lock:
            session = self._sessions.get(key)
            if session and session.alive:
                return session
            if session:
                session.close()
            session = WorkspaceLspSession(root)
            self._sessions[key] = session
            return session

    def close_all(self) -> None:
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            session.close()


lsp_manager = WorkspaceLspManager()
