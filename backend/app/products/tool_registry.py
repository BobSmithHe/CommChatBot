from __future__ import annotations

import json

import httpx

from ..core.code import CodeExecutor
from ..core.rag import LocalRagStore
from ..core.workspace import WorkspaceEditor
from ..infra.config import get_settings
from ..packages.agent import Tool


class ProductToolRegistry:
    def __init__(self, rag: LocalRagStore, code_executor: CodeExecutor, workspace: WorkspaceEditor) -> None:
        self.rag = rag
        self.code_executor = code_executor
        self.workspace = workspace
        self.settings = get_settings()

    def chatbot_tools(self, *, use_rag: bool, use_web: bool) -> list[Tool]:
        tools: list[Tool] = []
        if use_rag:
            tools.append(self.search_knowledge_tool())
        if use_web:
            tools.append(self.search_web_tool())
        return tools

    def coding_agent_tools(self, workspace: WorkspaceEditor | None = None) -> list[Tool]:
        editor = workspace or self.workspace
        return [
            self.list_files_tool(editor),
            self.read_file_tool(editor),
            self.search_files_tool(editor),
            self.write_file_tool(editor),
            self.replace_in_file_tool(editor),
            self.run_command_tool(editor),
            self.execute_python_tool(),
        ]

    def list_files_tool(self, workspace: WorkspaceEditor | None = None) -> Tool:
        editor = workspace or self.workspace
        return Tool(
            name="list_files",
            description="List files below a workspace-relative directory. Excludes generated and dependency folders.",
            input_schema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            handler=editor.list_files,
        )

    def read_file_tool(self, workspace: WorkspaceEditor | None = None) -> Tool:
        editor = workspace or self.workspace
        return Tool(
            name="read_file",
            description="Read up to 500 numbered lines from a UTF-8 text file inside the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
            handler=editor.read_file,
        )

    def search_files_tool(self, workspace: WorkspaceEditor | None = None) -> Tool:
        editor = workspace or self.workspace
        return Tool(
            name="search_files",
            description="Search text files in the workspace and return path, line number, and matching line.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "directory": {"type": "string"},
                    "pattern": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=editor.search_files,
            execution_mode="parallel",
        )

    def write_file_tool(self, workspace: WorkspaceEditor | None = None) -> Tool:
        editor = workspace or self.workspace
        return Tool(
            name="write_file",
            description="Create a UTF-8 text file in the workspace. Existing files require explicit overwrite=true.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            handler=editor.write_file,
            capability="write",
        )

    def replace_in_file_tool(self, workspace: WorkspaceEditor | None = None) -> Tool:
        editor = workspace or self.workspace
        return Tool(
            name="replace_in_file",
            description="Edit a workspace file by exact text replacement. Ambiguous matches are rejected by default.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            handler=editor.replace_in_file,
            capability="write",
        )

    def run_command_tool(self, workspace: WorkspaceEditor | None = None) -> Tool:
        editor = workspace or self.workspace
        return Tool(
            name="run_command",
            description=(
                "Run a bounded project verification command without shell parsing. "
                "Allowed commands: pytest, npm run/test, read-only git status/diff/log/show, and rg."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "directory": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["argv"],
            },
            handler=editor.run_command,
            capability="execute",
        )

    def search_knowledge_tool(self) -> Tool:
        return Tool(
            name="search_knowledge",
            description="Search the local wireless communications knowledge base.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                "required": ["query"],
            },
            handler=self.search_knowledge,
            execution_mode="parallel",
        )

    def search_web_tool(self) -> Tool:
        return Tool(
            name="search_web",
            description="Search recent external web information.",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            handler=self.search_web,
            execution_mode="parallel",
            capability="network",
        )

    def execute_python_tool(self) -> Tool:
        return Tool(
            name="execute_python",
            description="Execute Python code and return stdout, stderr, exit code, and image count.",
            input_schema={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
            handler=self.execute_python,
            capability="execute",
        )

    async def search_knowledge(self, query: str, top_k: int = 5) -> str:
        docs = await self.rag.search(query, top_k=top_k)
        if not docs:
            return "No local knowledge results found."
        return "\n\n".join(f"[{doc.source}] score={doc.score}\n{doc.content[:1600]}" for doc in docs)

    async def execute_python(self, code: str) -> str:
        result = await self.code_executor.execute(code, "python")
        payload = {
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": result.get("exit_code", -1),
            "image_count": len(result.get("images") or []),
        }
        return json.dumps(payload, ensure_ascii=False)

    async def search_web(self, query: str) -> str:
        if not self.settings.tavily_api_key:
            return "Web search disabled: TAVILY_API_KEY is not configured."
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": self.settings.tavily_api_key, "query": query, "max_results": 5},
            )
            response.raise_for_status()
        results = response.json().get("results", [])
        if not results:
            return "No web results found."
        return "\n\n".join(
            f"[Web] {item.get('title', '')}\n{item.get('url', '')}\n{str(item.get('content', ''))[:800]}"
            for item in results
        )
