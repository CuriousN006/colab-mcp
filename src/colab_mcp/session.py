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
from typing import Any

from fastmcp import FastMCP, Client
from fastmcp.client.transports import ClientTransport
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
from fastmcp.server.proxy import FastMCPProxy
from fastmcp.tools.tool import Tool, ToolResult
from mcp.client.session import ClientSession
from mcp.types import TextContent
import webbrowser

from colab_mcp.websocket_server import ColabWebSocketServer, COLAB, SCRATCH_PATH

UI_CONNECTION_TIMEOUT = 60.0  # secs

FE_CONNECTED_KEY = "fe_connected"
PROXY_TOKEN_KEY = "proxy_token"
PROXY_PORT_KEY = "proxy_port"
INJECTED_TOOL_NAME = "open_colab_browser_connection"


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


def _tool_result_payload(result):
    data = getattr(result, "data", None)
    if data is not None:
        return data

    structured_content = getattr(result, "structured_content", None)
    if structured_content is not None:
        return structured_content

    content = getattr(result, "content", None)
    if content is None:
        return None

    serializable_content = []
    for item in content:
        if hasattr(item, "model_dump"):
            serializable_content.append(
                item.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
        elif hasattr(item, "text"):
            serializable_content.append({"type": "text", "text": item.text})
        else:
            serializable_content.append({"type": "text", "text": str(item)})
    return {"content": serializable_content}


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
        with contextlib.suppress(asyncio.TimeoutError):
            # wait for the connection to be live and for the proxy client to fully initialize
            connection_tasks = asyncio.gather(
                self.wss.connection_live.wait(), self._start_task
            )
            await asyncio.wait_for(
                connection_tasks,
                timeout=UI_CONNECTION_TIMEOUT,
            )

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
        return _tool_result_payload(result)

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
    description="Opens a connection to a Google Colab browser session and unlocks notebook editing tools. Returns a boolean representing whether the connection attempt succeeded",
)


def build_codex_proxy_tools(proxy_client: ColabProxyClient) -> list[Tool]:
    async def codex_add_code_cell(cellIndex: int, language: str, code: str) -> Any:
        """Insert a code cell into the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "add_code_cell",
            _colab_tool_args(cellIndex=cellIndex, language=language, code=code),
        )

    async def codex_add_text_cell(cellIndex: int, content: str) -> Any:
        """Insert a markdown/text cell into the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "add_text_cell",
            _colab_tool_args(cellIndex=cellIndex, content=content),
        )

    async def codex_update_cell(cellId: str, content: str) -> Any:
        """Overwrite the content of a cell in the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "update_cell",
            _colab_tool_args(cellId=cellId, content=content),
        )

    async def codex_move_cell(cellId: str, cellIndex: int) -> Any:
        """Move a cell to a new index in the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "move_cell",
            _colab_tool_args(cellId=cellId, cellIndex=cellIndex),
        )

    async def codex_delete_cell(cellId: str) -> Any:
        """Delete a cell from the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "delete_cell",
            _colab_tool_args(cellId=cellId),
        )

    async def codex_get_cells(
        start: int = 0,
        end: int | None = None,
        includeOutputs: bool = False,
    ) -> Any:
        """Return cells from the connected Colab notebook."""
        return await proxy_client.call_colab_tool(
            "get_cells",
            _colab_tool_args(
                start=start,
                end=end,
                includeOutputs=includeOutputs,
            ),
        )

    async def codex_run_code_cell(cellId: str) -> Any:
        """Run a code cell in the connected Colab notebook and return outputs."""
        return await proxy_client.call_colab_tool(
            "run_code_cell",
            _colab_tool_args(cellId=cellId),
        )

    return [
        Tool.from_function(
            fn=codex_add_code_cell,
            name="codex_add_code_cell",
            description="Codex-compatible wrapper for Colab add_code_cell(cellIndex, language, code). Use language values such as python, r, or julia.",
        ),
        Tool.from_function(
            fn=codex_add_text_cell,
            name="codex_add_text_cell",
            description="Codex-compatible wrapper for Colab add_text_cell(cellIndex, content).",
        ),
        Tool.from_function(
            fn=codex_update_cell,
            name="codex_update_cell",
            description="Codex-compatible wrapper for Colab update_cell(cellId, content).",
        ),
        Tool.from_function(
            fn=codex_move_cell,
            name="codex_move_cell",
            description="Codex-compatible wrapper for Colab move_cell(cellId, cellIndex).",
        ),
        Tool.from_function(
            fn=codex_delete_cell,
            name="codex_delete_cell",
            description="Codex-compatible wrapper for Colab delete_cell(cellId).",
        ),
        Tool.from_function(
            fn=codex_get_cells,
            name="codex_get_cells",
            description="Codex-compatible wrapper for Colab get_cells(start, end, includeOutputs).",
        ),
        Tool.from_function(
            fn=codex_run_code_cell,
            name="codex_run_code_cell",
            description="Codex-compatible wrapper for Colab run_code_cell(cellId).",
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
        self.middleware.append(
            ToolInjectionMiddleware(
                tools=[check_session_proxy_tool, *build_codex_proxy_tools(proxy_client)]
            )
        )

    async def cleanup(self):
        await self._exit_stack.aclose()
