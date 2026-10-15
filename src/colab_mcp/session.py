# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
from collections.abc import AsyncIterator
import contextlib
from contextlib import AsyncExitStack
import copy
import json
import re
from typing import Any

from fastmcp import FastMCP, Client
from fastmcp.client.transports import ClientTransport
from fastmcp.dependencies import CurrentContext
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
from fastmcp.server.proxy import FastMCPProxy
from fastmcp.tools.tool import Tool, ToolResult
from mcp.client.session import ClientSession
from mcp.types import ImageContent, TextContent
import webbrowser

from colab_mcp.websocket_server import ColabWebSocketServer, COLAB, SCRATCH_PATH

UI_CONNECTION_TIMEOUT = 60.0  # secs

FE_CONNECTED_KEY = "fe_connected"
PROXY_TOKEN_KEY = "proxy_token"
PROXY_PORT_KEY = "proxy_port"
INJECTED_TOOL_NAME = "open_colab_browser_connection"

# tools whose results carry notebook cell outputs
OUTPUT_TOOL_NAMES = {
    "run_code_cell",
    "get_cells",
    "colab_run_code_cell",
    "colab_get_cells",
}
IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

SERVER_INSTRUCTIONS = """\
Works with a Google Colab notebook open in the user's browser.

Connecting:
1. Only open_colab_browser_connection is available until a notebook is connected.
2. Before calling it, tell the user: a Colab tab will open in their browser and they must
   allow the connection there within 60 seconds.
3. If it returns false, ask the user whether they saw the prompt, then call it again;
   the server keeps waiting for the browser between attempts.
4. Once it returns true, the notebook tools appear (add_code_cell, add_text_cell,
   update_cell, move_cell, delete_cell, get_cells, run_code_cell). Clients that ignore
   tool list changes can use the always-available colab_* wrappers instead.

Using the notebook:
- Code runs on the Colab runtime, not on the user's machine; variables persist between cells.
- Image outputs (plots) are returned as image content; text placeholders mark where they were.
- colab_notebook_outline gives a cheap one-line-per-cell overview; colab_search_cells finds
  text across cells; colab_edit_cell replaces one exact snippet in a cell instead of
  rewriting the whole cell with update_cell.
"""


class ColabTransport(ClientTransport):
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss

    @contextlib.asynccontextmanager
    async def connect_session(self, **session_kwargs) -> AsyncIterator[ClientSession]:
        async with ClientSession(
            self.wss.read_stream, self.wss.write_stream, **session_kwargs
        ) as session:
            yield session

    def __repr__(self) -> str:
        return "<ColabSessionProxyTransport>"


def _colab_tool_args(**kwargs) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


def _notebook_payload(result: ToolResult) -> dict[str, Any] | None:
    """Returns a copy of a notebook tool result payload ({"outputs": ...} or {"cells": ...})."""
    candidates = []
    if isinstance(result.structured_content, dict):
        candidates.append(result.structured_content)
    for item in result.content:
        if isinstance(item, TextContent):
            with contextlib.suppress(ValueError):
                candidates.append(json.loads(item.text))
    for candidate in candidates:
        if isinstance(candidate, dict) and (
            "outputs" in candidate or "cells" in candidate
        ):
            return copy.deepcopy(candidate)
    return None


def _strip_ansi(value):
    if isinstance(value, str):
        return ANSI_ESCAPE_RE.sub("", value)
    if isinstance(value, list):
        return [_strip_ansi(item) for item in value]
    return value


def _clean_output(output: dict[str, Any], images: list[ImageContent]):
    """Strips ANSI codes and moves base64 images into MCP image content."""
    for key in ("traceback", "text", "evalue"):
        if key in output:
            output[key] = _strip_ansi(output[key])
    data = output.get("data")
    if not isinstance(data, dict):
        return
    for mime_type in IMAGE_MIME_TYPES:
        encoded = data.get(mime_type)
        if encoded is None:
            continue
        if isinstance(encoded, list):
            encoded = "".join(encoded)
        images.append(
            ImageContent(
                type="image", data="".join(encoded.split()), mimeType=mime_type
            )
        )
        data[mime_type] = f"<{mime_type} attached as image #{len(images)}>"


def clean_notebook_result(result: ToolResult) -> ToolResult:
    """
    Makes cell outputs readable for agents: images become MCP image content instead of
    base64 text, and ANSI color codes are removed from tracebacks and streams.
    """
    payload = _notebook_payload(result)
    if payload is None:
        return result
    outputs = list(payload.get("outputs") or [])
    for cell in payload.get("cells") or []:
        if isinstance(cell, dict):
            outputs.extend(cell.get("outputs") or [])
    images: list[ImageContent] = []
    for output in outputs:
        if isinstance(output, dict):
            _clean_output(output, images)
    return ToolResult(
        content=[
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)),
            *images,
        ],
        structured_content=payload if result.structured_content is not None else None,
        meta=result.meta,
    )


class ColabProxyClient:
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss
        self.stubbed_mcp_client = Client(FastMCP())
        self.proxy_mcp_client: Client | None = None
        self._exit_stack = AsyncExitStack()
        self._start_task = None

    def is_connected(self):
        return self.wss.connection_live.is_set() and self.proxy_mcp_client is not None

    async def await_proxy_connection(self):
        # wait for the connection to be live and for the proxy client to fully initialize.
        # asyncio.wait does not cancel on timeout, so the start task survives for a later retry.
        if self._start_task.done() and self.proxy_mcp_client is None:
            # a previous start attempt failed; start a fresh one
            self._start_task = asyncio.create_task(self._start_proxy_client())
        live_waiter = asyncio.ensure_future(self.wss.connection_live.wait())
        try:
            await asyncio.wait(
                [live_waiter, self._start_task],
                timeout=UI_CONNECTION_TIMEOUT,
            )
        finally:
            live_waiter.cancel()

    def client_factory(self):
        if self.is_connected():
            return self.proxy_mcp_client
        # return a client mapped to a stubbed mcp server if there is no session proxy
        return self.stubbed_mcp_client

    async def call_colab_tool(self, tool_name: str, arguments: dict[str, Any]):
        if not self.is_connected() or self.proxy_mcp_client is None:
            raise RuntimeError(
                "Colab UI is not connected. Call open_colab_browser_connection first, "
                "then retry this tool after the browser connection succeeds."
            )
        result = await self.proxy_mcp_client.call_tool(tool_name, arguments)
        # pass the result through as-is so image and other non-text content survive
        return ToolResult(
            content=result.content,
            structured_content=result.structured_content,
            meta=result.meta,
        )

    async def _start_proxy_client(self):
        # blocks until a websocket connection is made successfully
        self.proxy_mcp_client = await self._exit_stack.enter_async_context(
            Client(ColabTransport(self.wss))
        )

    async def __aenter__(self):
        self._start_task = asyncio.create_task(self._start_proxy_client())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._start_task:
            self._start_task.cancel()
        await self._exit_stack.aclose()


class ColabProxyMiddleware(Middleware):
    def __init__(self, proxy_client: ColabProxyClient):
        self.proxy_client = proxy_client
        self.last_message_connected = self.proxy_client.is_connected()

    async def on_message(self, context: MiddlewareContext, call_next):
        """
        Check for a change to Colab session connectivity on any communication with this MCP server and
        notify the client when the connectivity status has changed.
        """
        context.fastmcp_context.set_state(
            FE_CONNECTED_KEY, self.proxy_client.is_connected()
        )
        context.fastmcp_context.set_state(PROXY_TOKEN_KEY, self.proxy_client.wss.token)
        context.fastmcp_context.set_state(PROXY_PORT_KEY, self.proxy_client.wss.port)

        result = await call_next(context)

        connected = self.proxy_client.is_connected()
        connection_state_changed = connected != self.last_message_connected
        self.last_message_connected = connected
        if connection_state_changed:
            await context.fastmcp_context.send_tool_list_changed()

        return result

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if context.message.name != INJECTED_TOOL_NAME:
            return result
        if self.proxy_client.is_connected():
            return result
        # if the tool call was for open_colab_browser_connection and there is no existing connection, try to await full connection
        await context.fastmcp_context.report_progress(
            progress=1, total=3, message="The user is not connected to the Colab UI"
        )
        await context.fastmcp_context.report_progress(
            progress=2,
            total=3,
            message="Waiting for user to connect in Colab - will wait for 60s",
        )
        await self.proxy_client.await_proxy_connection()
        if self.proxy_client.is_connected():
            await context.fastmcp_context.report_progress(
                progress=3, total=3, message="The Colab UI is successfully connected!"
            )
            return ToolResult(
                content=[TextContent(type="text", text="true")],
                structured_content={"result": True},
            )
        else:
            await context.fastmcp_context.report_progress(
                progress=3,
                total=3,
                message="Timeout while waiting for the user to connect.",
            )
            return ToolResult(
                content=[TextContent(type="text", text="false")],
                structured_content={"result": False},
            )


async def check_session_proxy_tool_fn(ctx: Context = CurrentContext()) -> bool:
    fe_connected = ctx.get_state(FE_CONNECTED_KEY)
    token = ctx.get_state(PROXY_TOKEN_KEY)
    port = ctx.get_state(PROXY_PORT_KEY)
    if fe_connected:
        return True
    webbrowser.open_new(
        f"{COLAB}{SCRATCH_PATH}#mcpProxyToken={token}&mcpProxyPort={port}"
    )
    return False


check_session_proxy_tool = Tool.from_function(
    fn=check_session_proxy_tool_fn,
    name=INJECTED_TOOL_NAME,
    description=(
        "Opens a connection to a Google Colab browser session and unlocks notebook editing tools. "
        "Returns a boolean representing whether the connection attempt succeeded. "
        "Before calling, tell the user that a Colab tab will open and that they must allow the "
        "connection there within 60 seconds. If it returns false, confirm with the user and call it again."
    ),
)


class ColabOutputMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if context.message.name in OUTPUT_TOOL_NAMES:
            return clean_notebook_result(result)
        return result


def _source_text(cell: dict[str, Any]) -> str:
    source = cell.get("source") or ""
    return "".join(source) if isinstance(source, list) else source


async def _get_all_cells(proxy_client: ColabProxyClient) -> list[dict[str, Any]]:
    result = await proxy_client.call_colab_tool("get_cells", {})
    payload = _notebook_payload(result)
    if payload is None:
        raise ToolError("Unexpected get_cells response from Colab.")
    return [cell for cell in payload.get("cells") or [] if isinstance(cell, dict)]


def _one_line(text: str, limit: int | None = None) -> str:
    flat = text.replace("\n", "↵")
    if limit is not None and len(flat) > limit:
        return flat[:limit] + "..."
    return flat


def build_static_proxy_tools(proxy_client: ColabProxyClient) -> list[Tool]:
    async def colab_add_code_cell(cellIndex: int, language: str, code: str) -> Any:
        """Insert a code cell into the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "add_code_cell",
            _colab_tool_args(cellIndex=cellIndex, language=language, code=code),
        )

    async def colab_add_text_cell(cellIndex: int, content: str) -> Any:
        """Insert a markdown/text cell into the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "add_text_cell",
            _colab_tool_args(cellIndex=cellIndex, content=content),
        )

    async def colab_update_cell(cellId: str, content: str) -> Any:
        """Overwrite the content of a cell in the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "update_cell",
            _colab_tool_args(cellId=cellId, content=content),
        )

    async def colab_move_cell(cellId: str, cellIndex: int) -> Any:
        """Move a cell to a new index in the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "move_cell",
            _colab_tool_args(cellId=cellId, cellIndex=cellIndex),
        )

    async def colab_delete_cell(cellId: str) -> Any:
        """Delete a cell from the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "delete_cell",
            _colab_tool_args(cellId=cellId),
        )

    async def colab_get_cells(
        cellIndexStart: int | None = None,
        cellIndexEnd: int | None = None,
        includeOutputs: bool = False,
    ) -> Any:
        """Return cells from the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "get_cells",
            _colab_tool_args(
                cellIndexStart=cellIndexStart,
                cellIndexEnd=cellIndexEnd,
                includeOutputs=includeOutputs,
            ),
        )

    async def colab_run_code_cell(cellId: str) -> Any:
        """Run a code cell in the connected Colab notebook and return outputs."""
        return await proxy_client.call_colab_tool(
            "run_code_cell",
            _colab_tool_args(cellId=cellId),
        )

    async def colab_edit_cell(cellId: str, old_text: str, new_text: str) -> Any:
        """Replace one exact occurrence of old_text in a cell, leaving the rest as is."""
        if not old_text:
            raise ToolError("old_text must not be empty.")
        cells = await _get_all_cells(proxy_client)
        for index, cell in enumerate(cells):
            if cell.get("id") == cellId:
                break
        else:
            raise ToolError(f"Cell {cellId} was not found in the notebook.")
        source = _source_text(cell)
        count = source.count(old_text)
        if count == 0:
            raise ToolError(f"old_text was not found in cell {cellId}.")
        if count > 1:
            raise ToolError(
                f"old_text appears {count} times in cell {cellId}; "
                "include more surrounding text so it matches exactly once."
            )
        await proxy_client.call_colab_tool(
            "update_cell",
            {"cellId": cellId, "content": source.replace(old_text, new_text, 1)},
        )
        return {"cellId": cellId, "cellIndex": index, "replaced": 1}

    async def colab_notebook_outline() -> Any:
        """List every cell as one line: index, type, id and a short source preview."""
        cells = await _get_all_cells(proxy_client)
        lines = [f"{len(cells)} cells"]
        for index, cell in enumerate(cells):
            preview = _one_line(_source_text(cell), limit=60) or "(empty)"
            lines.append(
                f"[{index}] {cell.get('cell_type', '?')} {cell.get('id', '?')}: {preview}"
            )
        return "\n".join(lines)

    async def colab_search_cells(
        pattern: str,
        use_regex: bool = False,
        case_sensitive: bool = True,
        max_results: int = 50,
    ) -> Any:
        """Find text (or a regex) across all cell sources."""
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern if use_regex else re.escape(pattern), flags)
        except re.error as e:
            raise ToolError(f"Invalid regex: {e}") from e
        cells = await _get_all_cells(proxy_client)
        matches = []
        total = 0
        for index, cell in enumerate(cells):
            source = _source_text(cell)
            for match in regex.finditer(source):
                total += 1
                if len(matches) >= max_results:
                    continue
                start = max(0, match.start() - 20)
                end = min(len(source), match.end() + 20)
                matches.append(
                    {
                        "cellIndex": index,
                        "cellId": cell.get("id"),
                        "cellType": cell.get("cell_type"),
                        "match": match.group(),
                        "context": _one_line(source[start:end]),
                    }
                )
        return {
            "matchCount": total,
            "truncated": total > len(matches),
            "matches": matches,
        }

    return [
        Tool.from_function(
            fn=colab_add_code_cell,
            name="colab_add_code_cell",
            description="Always-available wrapper for Colab add_code_cell(cellIndex, language, code). Use language values such as python, r, or julia.",
        ),
        Tool.from_function(
            fn=colab_add_text_cell,
            name="colab_add_text_cell",
            description="Always-available wrapper for Colab add_text_cell(cellIndex, content).",
        ),
        Tool.from_function(
            fn=colab_update_cell,
            name="colab_update_cell",
            description="Always-available wrapper for Colab update_cell(cellId, content).",
        ),
        Tool.from_function(
            fn=colab_move_cell,
            name="colab_move_cell",
            description="Always-available wrapper for Colab move_cell(cellId, cellIndex).",
        ),
        Tool.from_function(
            fn=colab_delete_cell,
            name="colab_delete_cell",
            description="Always-available wrapper for Colab delete_cell(cellId).",
        ),
        Tool.from_function(
            fn=colab_get_cells,
            name="colab_get_cells",
            description="Always-available wrapper for Colab get_cells(cellIndexStart, cellIndexEnd, includeOutputs).",
        ),
        Tool.from_function(
            fn=colab_run_code_cell,
            name="colab_run_code_cell",
            description="Always-available wrapper for Colab run_code_cell(cellId).",
        ),
        Tool.from_function(
            fn=colab_edit_cell,
            name="colab_edit_cell",
            description=(
                "Edit part of a cell: replaces old_text with new_text in the cell with the given id. "
                "old_text must appear exactly once in that cell, otherwise nothing changes and an "
                "error says how many matches there were. Prefer this over update_cell for small edits."
            ),
        ),
        Tool.from_function(
            fn=colab_notebook_outline,
            name="colab_notebook_outline",
            description=(
                "Cheap overview of the notebook: one line per cell with its index, type, id and the "
                "first 60 characters of source (newlines shown as ↵). No outputs. Use it before "
                "get_cells on large notebooks."
            ),
        ),
        Tool.from_function(
            fn=colab_search_cells,
            name="colab_search_cells",
            description=(
                "Search all cell sources for text, or a Python regex when use_regex is true. Returns "
                "each match's cell index, id, type and about 20 characters of context on each side."
            ),
        ),
    ]


class ColabSessionProxy:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.proxy_server: FastMCPProxy | None = None
        # list order matters, see: https://gofastmcp.com/servers/middleware#multiple-middleware
        self.middleware: list[Middleware] = []
        self.wss: ColabWebSocketServer | None = None

    async def start_proxy_server(self):
        self.wss = await self._exit_stack.enter_async_context(ColabWebSocketServer())
        proxy_client = await self._exit_stack.enter_async_context(
            ColabProxyClient(self.wss)
        )
        self.proxy_server = FastMCPProxy(
            client_factory=proxy_client.client_factory,
            instructions="Connects to a user's Google Colab session in a browser and allows for interactions with their Google Colab notebook",
        )
        # ColabProxyMiddleware must be first because it sets the fe_connected state
        self.middleware.append(ColabProxyMiddleware(proxy_client))
        # outside ToolInjectionMiddleware so the colab_* wrapper results are cleaned too
        self.middleware.append(ColabOutputMiddleware())
        self.middleware.append(
            ToolInjectionMiddleware(
                tools=[
                    check_session_proxy_tool,
                    *build_static_proxy_tools(proxy_client),
                ]
            )
        )

    async def cleanup(self):
        await self._exit_stack.aclose()
