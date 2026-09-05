from __future__ import annotations

import json
import re
from urllib.parse import quote

import httpx

from app.infra.config import get_settings
from app.agent_runtime import Tool
from app.extensions.builtin.workspace import WorkspaceEditor


class GitHubIntegration:
    """Small GitHub REST integration exposed as permission-gated Agent tools."""

    def tools(self, workspace: WorkspaceEditor) -> list[Tool]:
        async def pull_requests(state: str = "open", limit: int = 20) -> str:
            owner, repo = await self._repository(workspace)
            return await self._request("GET", f"/repos/{owner}/{repo}/pulls", params={
                "state": state, "per_page": max(1, min(limit, 50)),
            })

        async def checks(ref: str = "HEAD") -> str:
            owner, repo = await self._repository(workspace)
            sha = (await self._git(workspace, ["rev-parse", ref])).strip()
            return await self._request("GET", f"/repos/{owner}/{repo}/commits/{quote(sha)}/check-runs")

        async def create_pull_request(title: str, body: str = "", head: str = "", base: str = "") -> str:
            owner, repo = await self._repository(workspace)
            branch = head.strip() or (await self._git(workspace, ["branch", "--show-current"])).strip()
            if not branch:
                raise ValueError("Cannot create a PR from detached HEAD")
            return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json_body={
                "title": title, "body": body, "head": branch,
                "base": base.strip() or get_settings().github_default_base,
            })

        async def review_pull_request(
            number: int, body: str = "", event: str = "COMMENT",
            commit_id: str = "", comments: list[dict] | None = None,
        ) -> str:
            owner, repo = await self._repository(workspace)
            return await self.submit_review(
                f"{owner}/{repo}", number=number, body=body, event=event,
                commit_id=commit_id or None, comments=comments or [],
            )

        return [
            Tool("github_list_prs", "List pull requests for the current GitHub repository.", {
                "type": "object", "properties": {
                    "state": {"type": "string", "enum": ["open", "closed", "all"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            }, pull_requests, capability="network"),
            Tool("github_check_runs", "Get GitHub Actions/check-run status for a git ref.", {
                "type": "object", "properties": {"ref": {"type": "string"}},
            }, checks, capability="network"),
            Tool("github_create_pr", "Create a GitHub pull request from the current or specified branch.", {
                "type": "object", "properties": {
                    "title": {"type": "string"}, "body": {"type": "string"},
                    "head": {"type": "string"}, "base": {"type": "string"},
                }, "required": ["title"],
            }, create_pull_request, capability="network"),
            Tool("github_review_pr", "Submit a pull-request review, optionally with line-level comments.", {
                "type": "object", "properties": {
                    "number": {"type": "integer", "minimum": 1}, "body": {"type": "string"},
                    "event": {"type": "string", "enum": ["COMMENT", "APPROVE", "REQUEST_CHANGES"]},
                    "commit_id": {"type": "string"},
                    "comments": {"type": "array", "maxItems": 100, "items": {
                        "type": "object", "properties": {
                            "path": {"type": "string"}, "line": {"type": "integer", "minimum": 1},
                            "side": {"type": "string", "enum": ["LEFT", "RIGHT"]},
                            "body": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                            "start_side": {"type": "string", "enum": ["LEFT", "RIGHT"]},
                        }, "required": ["path", "line", "side", "body"], "additionalProperties": False,
                    }},
                }, "required": ["number"],
            }, review_pull_request, capability="network"),
        ]

    async def submit_review(
        self, repository: str, *, number: int, body: str, event: str,
        commit_id: str | None = None, comments: list[dict] | None = None,
    ) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        allowed = {"COMMENT", "APPROVE", "REQUEST_CHANGES"}
        if event not in allowed:
            raise ValueError("Invalid review event")
        normalized = []
        for comment in comments or []:
            if not isinstance(comment, dict) or not all(comment.get(key) for key in ("path", "line", "side", "body")):
                raise ValueError("Each inline comment requires path, line, side and body")
            normalized.append({key: comment[key] for key in (
                "path", "line", "side", "body", "start_line", "start_side",
            ) if key in comment and comment[key] is not None})
        payload: dict = {"body": body, "event": event}
        if commit_id:
            payload["commit_id"] = commit_id
        if normalized:
            payload["comments"] = normalized
        return await self._request(
            "POST", f"/repos/{repository}/pulls/{number}/reviews", json_body=payload,
        )

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
    ) -> str:
        token = get_settings().github_token.strip()
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=30) as client:
            response = await client.request(method, path, params=params, json=json_body)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API {response.status_code}: {response.text[:1000]}")
        return json.dumps(response.json(), ensure_ascii=False, default=str)

    async def _repository(self, workspace: WorkspaceEditor) -> tuple[str, str]:
        remote = (await self._git(workspace, ["remote", "get-url", "origin"])).strip()
        match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
        if not match:
            raise ValueError("The workspace origin is not a GitHub repository")
        return match.group(1), match.group(2)

    @staticmethod
    async def _git(workspace: WorkspaceEditor, args: list[str]) -> str:
        result = await workspace.run_command(argv=["git", *args], directory="", timeout=20)
        if int(result.get("exit_code", 1)) != 0:
            raise RuntimeError(str(result.get("stderr") or result.get("command_output") or "git failed"))
        return str(result.get("stdout") or result.get("command_output") or "")


github_integration = GitHubIntegration()
