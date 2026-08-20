# Client configurations

Copy-pasteable MCP client configurations for mailmind. All of them spawn
`mailmindctl mcp`, which speaks MCP over the pipe and tells the model where the review UI
is — see [Connecting an agent](../docs/agents.md) for what the agent
gets and what it does not.

| File | For |
|---|---|
| [`opencode.json`](opencode.json) | opencode, current config schema (`mcp.servers`) |
| [`mcp-servers.json`](mcp-servers.json) | the `mcpServers` shape: Claude Desktop, Claude Code's `.mcp.json`, VS Code, and most others |

## Where they go

- **opencode** — `~/.config/opencode/opencode.json` for every project, or `opencode.json`
  in a project root, which wins. Older opencode nests servers directly under `mcp` rather
  than under `mcp.servers`, and spells the switch `enabled: true` instead of
  `disabled: false`; the fields are otherwise the same.
- **Claude Desktop** — merge the `mcpServers` block into `claude_desktop_config.json`.
- **Claude Code** — `.mcp.json` in the project root takes the file as it stands.

## The two decisions in them

**`MAILMIND_CONFIG` is set explicitly.** A spawned process inherits a working directory
nobody chose, so the `./mailmind.toml` fallback is not something to rely on. mailmind
expands the `~` itself, and refuses to start if the path it was given is not there — a
named configuration that is missing is a mistake, not a fresh install, and starting anyway
would mean an empty configuration and a database in some arbitrary directory.

**`--serve --port 0`** brings the review UI up for the life of the session, on a free port,
and tells the model the address so the agent can pass it on. That is the self-contained
shape: start the agent, get told where to review, review it while the agent is there.

The review UI has a login, and the link that opens it is deliberately *not* in the MCP
log — these clients collect the stderr of what they spawn, and some show it to the model.
The link goes to a file only you can read; the log gets its path. `mailmindctl review
--open` follows it for you. If a page says "Not open", that is what to run.

Drop `--serve --port 0` if you would rather run `mailmindctl serve` yourself and have a
queue that outlives any one session. The agent is then told the address from your
configuration instead, and everything else is the same.

## Before the first connection

```
mailmindctl bootstrap && mailmindctl probe && mailmindctl sync
```

The agent can call `request_sync` afterwards, but the first one is worth doing by hand
where you can watch it.

## Narrowing what the agent gets

`--producer NAME` reuses that producer's grant if it has one, so mint a narrow one first
and the pipe gets the narrow one:

```
mailmindctl grant --producer opencode --capability observe --capability suggest
```

Without that, a producer with no grant gets one covering every account, on the reasoning
that whatever spawned the process could read the database anyway. `--account NAME` narrows
it further. There is no capability that applies anything, whatever the grant says.
