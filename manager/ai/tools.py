"""Explicit future tool-call boundary. No tools are enabled by default."""
from dataclasses import dataclass
from typing import Callable
from pydantic import BaseModel


@dataclass(frozen=True)
class Tool:
    parameters: type[BaseModel]
    handler: Callable
    permission: Callable[[int], bool]
    requires_confirmation: bool = True


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, name, tool: Tool):
        if name in self.tools:
            raise ValueError("Tool already registered")
        self.tools[name] = tool

    async def invoke(self, name, arguments, user_id, confirmed=False):
        tool = self.tools.get(name)
        if tool is None or not tool.permission(user_id):
            raise PermissionError("Tool unavailable or forbidden")
        if tool.requires_confirmation and not confirmed:
            raise PermissionError("Explicit confirmation required")
        params = tool.parameters.model_validate(arguments)
        return await tool.handler(params)
