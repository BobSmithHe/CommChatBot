"""Backward-compatible imports for the split built-in coding extensions."""

from .builtin.github import GitHubExtension as GitHubToolsExtension
from .builtin.mcp import MCPExtension as MCPToolsExtension
from .builtin.memory import ProjectMemoryExtension
from .builtin.plan import PlanExtension
from .builtin.project_context import ProjectContextExtension
from .builtin.remote_execution import RemoteExecutionExtension
from .builtin.subagents import SubagentExtension
from .builtin.workspace import WorkspaceExtension as CodingToolsExtension

__all__ = [
    "CodingToolsExtension", "GitHubToolsExtension", "MCPToolsExtension", "PlanExtension",
    "ProjectContextExtension", "ProjectMemoryExtension", "RemoteExecutionExtension",
    "SubagentExtension",
]
