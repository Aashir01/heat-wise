# Connecting the FortyGuard MCP to coding agents

From FortyGuard's MCP Client Setup page (my local copy, not included in this repo). Endpoint `https://tos-mcp.app.fortyguard.com/mcp`, Streamable HTTP, header `Authorization: Bearer <key>`. Note the REST API uses a different header (`api-key`).

In this repo the key is stored as `TEMP_API_KEY` in `.env`. The client snippets below use FortyGuard's documented variable name `FORTYGUARD_API_KEY`; set it from your key in your shell, never in a committed file.

```powershell
# PowerShell
$env:FORTYGUARD_API_KEY = "<your key>"
```

```bash
# bash / zsh
export FORTYGUARD_API_KEY='<your key>'
```

## Claude Code

```bash
claude mcp add --transport http fortyguard \
  https://tos-mcp.app.fortyguard.com/mcp \
  --header "Authorization: Bearer $FORTYGUARD_API_KEY"
claude mcp list
```

Inside Claude Code, `/mcp` should show `fortyguard` as connected. Tool names may appear with a server prefix.

## Cursor (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "fortyguard": {
      "url": "https://tos-mcp.app.fortyguard.com/mcp",
      "headers": { "Authorization": "Bearer ${env:FORTYGUARD_API_KEY}" }
    }
  }
}
```

Launch Cursor from a shell where the variable is set so it inherits it.

## OpenAI Codex CLI (`~/.codex/config.toml`)

```toml
[mcp_servers.fortyguard]
url = "https://tos-mcp.app.fortyguard.com/mcp"
enabled = true
env_http_headers = { Authorization = "FORTYGUARD_AUTH_HEADER" }
startup_timeout_sec = 30
tool_timeout_sec = 120
```

with `export FORTYGUARD_AUTH_HEADER="Bearer $FORTYGUARD_API_KEY"`. Check with `codex mcp list`.

## Test prompt (from FortyGuard's docs)

> Get environmental conditions for latitude 33.4484, longitude -112.074, for September 20, 2025 using a reference temperature of 35 °C.

This is a paid call (2,900 credits on the key used here). In my testing the server listed 11 tools, not the 9 in the docs (`credit_balance` and `monitoring` are extra); `credit_balance` is free and a good first connection test. These client snippets were copied from the docs, not tested in each client; only the Python SDK connection was tested here.
