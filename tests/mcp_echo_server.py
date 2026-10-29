"""A tiny MCP server for the supervisor tests, over stdio."""
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

server = MCPServer("echo")
T0 = 1_700_000_000_000


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                                         openWorldHint=False))
def echo(text: str) -> str:
    """Echo the text back."""
    return text


@server.tool()
def env(name: str) -> str:
    """One variable of its environment, "" when unset."""
    return os.environ.get(name, "")


@server.tool()
def crash() -> str:
    """Exit the server process at once."""
    os._exit(1)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                                         openWorldHint=True))
def read_messages(chat: str, before: str = "") -> CallToolResult:
    """Two messages of a chat, as canonical feed records."""
    peer = {"key": "u1", "display_name": "Ana"}
    recs = [{"seq": 7, "kind": "message",
             "chat": {"key": chat, "kind": "dm", "peer": peer},
             "message": {"key": f"{chat}-m1", "ts": T0, "text": "first", "sender": peer}},
            {"seq": 8, "kind": "message",
             "chat": {"key": chat, "kind": "dm", "peer": peer},
             "message": {"key": f"{chat}-m2", "ts": T0 + 60_000, "text": "second",
                         "from_me": True}}]
    return CallToolResult(
        content=[TextContent(type="text", text="raw text the adapter replaces")],
        structured_content={"records": recs, "where": f"DM {chat}",
                            "note": f"Read 2 messages (before={before or 'none'}).",
                            "earlier": {"chat": chat, "before": f"{chat}-m1"}})


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=False, openWorldHint=True))
def send(text: str) -> str:
    """The one write."""
    return f"sent: {text}"


if __name__ == "__main__":
    server.run("stdio")
