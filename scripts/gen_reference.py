"""Render the MCP surface from the server itself.

The tools, resources and prompts are declared once, in ``mailmind.mcp.server``, and the
server publishes them to whoever connects.  A second copy typed into markdown would be a
copy that drifts, so this asks the server at build time instead.
"""

from __future__ import annotations

import asyncio
import inspect

import mkdocs_gen_files

from mailmind.config import Config
from mailmind.mcp.server import build_server
from mailmind.service import Service

LEAD = """# The MCP surface

Rendered from the server itself, which is the only place it is declared.

Both transports carry the same surface: a pipe (`mailmindctl mcp`) or
`http://127.0.0.1:8765/mcp/` with a bearer token. Every tool is scoped by the grant behind
the connection — see [Connecting a client](../connecting.md).

There is no tool that applies anything. Not a permission an agent lacks: `apply` is not a
value the capability enum can hold, and the module that writes to a mailbox is not imported
by anything on this side.
"""


def resolved(value):
    """The mcp package hands some of these back as coroutines and some as lists."""
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def sentence(text: str | None) -> str:
    """The first line of a docstring, which is what a table cell can hold."""
    if not text:
        return ""
    return text.strip().split("\n\n")[0].replace("\n", " ").strip()


def arguments(schema: dict) -> str:
    required = set(schema.get("required", ()))
    names = [
        name if name in required else f"{name}?"
        for name in schema.get("properties", {})
    ]
    return ", ".join(names)


def main() -> None:
    server = build_server(Service(Config()))
    tools = resolved(server.list_tools())
    resources = resolved(server.list_resources())
    templates = resolved(server.list_resource_templates())
    prompts = resolved(server.list_prompts())

    out = [LEAD, f"\n## Tools ({len(tools)})\n"]
    for tool in sorted(tools, key=lambda t: t.name):
        out.append(f"### `{tool.name}({arguments(tool.input_schema)})`\n")
        out.append((tool.description or "").strip() + "\n")

    out.append("## Resources\n")
    out.append("| URI | |\n|---|---|")
    for resource in resources:
        out.append(f"| `{resource.uri}` | {sentence(resource.description)} |")
    for template in templates:
        out.append(f"| `{template.uri_template}` | {sentence(template.description)} |")
    out.append("")

    out.append("## Prompts\n")
    out.append("| Prompt | |\n|---|---|")
    for prompt in prompts:
        out.append(f"| `{prompt.name}` | {sentence(prompt.description)} |")
    out.append("")

    with mkdocs_gen_files.open("reference/mcp.md", "w") as page:
        page.write("\n".join(out))


main()
