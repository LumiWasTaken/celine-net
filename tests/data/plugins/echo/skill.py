from celine.settings import form
from celine.skills import ToolContext, ToolResult, described, guide, tool


class Echo:
    name = "echo"
    guide = guide("echo", file=__file__, rel="guide.md")
    description = described()

    def __init__(self, cfg, plugin):
        self.cfg, self.plugin = cfg, plugin

    @tool(read_only=False)
    async def say(self, ctx: ToolContext, text: str) -> ToolResult:
        """Says `text` back with the prefix of its settings, and keeps it."""
        await self.plugin.db.write(
            lambda c: c.execute("INSERT INTO heard (text) VALUES (?)", (text,)))
        prefix = form.values(self.plugin.form["fields"],
                             self.cfg.settings.plugins.get("echo", {}))["prefix"]
        return ToolResult(prefix + text)
