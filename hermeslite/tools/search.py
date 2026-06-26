"""Built-in tool placeholder for the search category.

This module is intentionally tiny: web_search requires a third-party
search API (Bing/Google/Brave/DDG) and we don't ship a default. Users
add a custom tool by writing their own module that calls
``registry.register(MySearchTool())`` and adding the file to
``hermeslite/tools/_user/`` (a directory we will create on first run).

For now this file documents the gap so the agent can tell the user what
to do.
"""
from __future__ import annotations

from .registry import Tool, ToolResult, registry


class WebSearchStubTool(Tool):
    name = "web_search"
    description = (
        "Stub: web search is not enabled by default. To enable it, drop "
        "a custom tool into ~/.hermes-lite/tools_user/ that calls your "
        "preferred search API and registers it under the name 'web_search'."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def run(self, query: str, **_) -> ToolResult:
        return ToolResult.failure(
            "web_search is not configured. See the tool description for setup."
        )


registry.register(WebSearchStubTool())
