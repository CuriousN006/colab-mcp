# Colab-mcp

An MCP server for bridging your local agent to a Colab session in the browser.

# Fork notes

This fork exposes the Colab notebook operations only as static `colab_*` tools. Upstream mirrors Colab's own tools (`add_code_cell`, `run_code_cell`, ...) once the browser connects, which changes the tool list mid-session and leaves two names for each operation. Here the tool list is the same 11 tools before and after connecting, so it works the same in every client, including ones that do not refresh `notifications/tools/list_changed`.

To use this fork:

```
...
  "mcpServers": {
    "colab-mcp": {
      "command": "uvx",
      "args": ["git+https://github.com/CuriousN006/colab-mcp"],
      "timeout": 30000
    }
  }
...
```

The static wrapper tools are:

- `colab_add_code_cell(cellIndex, language, code)`
- `colab_add_text_cell(cellIndex, content)`
- `colab_update_cell(cellId, content)`
- `colab_move_cell(cellId, cellIndex)` (the cell ends up at `cellIndex`; see below)
- `colab_delete_cell(cellId)`
- `colab_get_cells(cellIndexStart, cellIndexEnd, includeOutputs)`
- `colab_run_code_cell(cellId)`

Call `open_colab_browser_connection` first. After the browser connection succeeds, the `colab_*` tools forward to the live Colab notebook tools; before that they return an error asking you to connect.

## Agent-friendly changes

- **Server instructions:** the top-level server sends instructions describing the connection flow (tell the user a Colab tab will open and must be allowed within 60 seconds, retry on `false`). Upstream sets instructions only on the mounted proxy server, so they never reach the client.
- **Retry after timeout:** upstream cancels the proxy start task when the 60 second wait times out, so the server cannot connect again until it is restarted. The wait no longer cancels it, and a failed start is retried.
- **Image outputs:** `image/png`, `image/jpeg`, `image/gif` and `image/webp` outputs from `colab_run_code_cell` and `colab_get_cells` are returned as MCP image content instead of base64 text, with a placeholder left in the JSON.
- **Cell moves land where asked:** Colab's `move_cell` puts the cell right after the one at `cellIndex`, so a cell moved up lands one below the requested index, and moving a cell up by one leaves the notebook unchanged but shifts the list `get_cells` reads, after which moves and deletes hit the wrong cells. `colab_move_cell` reads the cell's current index and sends the index that puts it at `cellIndex`.
- **Readable errors:** ANSI color codes are stripped from tracebacks and stream output.
- **Editing helpers** built on `get_cells` and `update_cell`:
  - `colab_edit_cell(cellId, old_text, new_text)` replaces one exact snippet in a cell and refuses when it matches zero or several times, so small edits don't have to resend the whole cell.
  - `colab_notebook_outline()` lists one line per cell (index, type, id, 60-character preview) without outputs.
  - `colab_search_cells(pattern, use_regex, case_sensitive, max_results)` finds text or a regex across all cells with surrounding context.

# Supported Clients
This MCP server requires the client to be running locally on your device. Because the tool list never changes, it does not need `notifications/tools/list_changed` support.

Clients known to work include:
- Claude Code
- Codex

# Setup

- Install `uv` (`pip install uv`)
- Configure for usage (eg for mcp.json style services):

```
...
  "mcpServers": {
    "colab-mcp": {
      "command": "uvx",
      "args": ["git+https://github.com/CuriousN006/colab-mcp"],
      "timeout": 30000
    }
  }
...
```

(If you have a non-standard default package index (**Googlers**), you may also need to add `--index https://pypi.org/simple`)

The two sections below are the upstream project's ([googlecolab/colab-mcp](https://github.com/googlecolab/colab-mcp)) policy.

# Issues & Discussions

We are using GitHub [discussions](https://github.com/googlecolab/colab-mcp/discussions) as the
place for issue discussion and feature requests. As discussions mature into action items, we
will add those items as issues. This helps us ensure that issues in the issue tracker are
well-understood, deduplicated, and actionable. For these reasons, **please do <u>NOT</u> open
issues directly.**

# Contributing
We unfortunately don't have the bandwidth to support review of external contributions, and we
don't want user PRs to languish, so we aren't accepting any external contributions right now.

If you have a great idea or pain point, we would love to hear about it on our
[discussions](https://github.com/googlecolab/colab-mcp/discussions) page - the preferred place
for issue discussion and feature requests.

# Internal - For Colab Developers

### Prerequisites

- `uv` is required (`pip install uv`)
- Configure git hooks to run repo presubmits

```shell
git config core.hooksPath .githooks
```

### Gemini CLI setup

```
...
  "mcpServers": {
    "colab-mcp": {
      "command": "uv",
      "args": ["run", "colab-mcp"],
      "cwd": "/path/to/github/colab-mcp",
      "timeout": 30000
    }
  }
...
```
