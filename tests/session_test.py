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
from colab_mcp import session
from fastmcp.server.middleware import MiddlewareContext
import pytest
from unittest.mock import patch, AsyncMock, Mock


@pytest.fixture(autouse=True)
def mock_webbrowser(monkeypatch):
    import webbrowser

    mock_open = Mock()
    monkeypatch.setattr(webbrowser, "open_new", mock_open)
    return mock_open


@pytest.fixture
def mock_wss():
    """Provides a mock ColabWebSocketServer instance."""
    return MockColabWebSocketServer()


class MockColabWebSocketServer:
    def __init__(self):
        self.connection_live = asyncio.Event()
        self.read_stream = AsyncMock()
        self.write_stream = AsyncMock()
        self.token = "test-token"
        self.port = 1234

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_proxy_client(mock_wss):
    client = Mock(spec=session.ColabProxyClient)
    client.wss = mock_wss
    client.is_connected.return_value = False
    return client


class TestColabProxyMiddleware:
    @pytest.mark.asyncio
    async def test_connection_live(self, mock_proxy_client):
        """Tests connection state change from disconnected to connected."""
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = True
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", True)
        context.fastmcp_context.set_state.assert_any_call("proxy_token", "test-token")
        context.fastmcp_context.set_state.assert_any_call("proxy_port", 1234)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_not_live(self, mock_proxy_client):
        """Tests connection state change from connected to disconnected."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        mock_proxy_client.is_connected.return_value = False
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", False)
        assert middleware.last_message_connected is False
        context.fastmcp_context.send_tool_list_changed.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_connection_change(self, mock_proxy_client):
        """Tests no connection state change."""
        mock_proxy_client.is_connected.return_value = True
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock(spec=MiddlewareContext)
        context.fastmcp_context.set_state = Mock()
        context.fastmcp_context.send_tool_list_changed = AsyncMock()
        call_next = AsyncMock()

        await middleware.on_message(context, call_next)

        call_next.assert_called_once_with(context)
        context.fastmcp_context.set_state.assert_any_call("fe_connected", True)
        assert middleware.last_message_connected is True
        context.fastmcp_context.send_tool_list_changed.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_call_tool_await_connection(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.side_effect = [False, True]
        mock_proxy_client.await_proxy_connection = AsyncMock()
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        context.fastmcp_context.report_progress.assert_called()
        assert result.structured_content == {"result": True}

    @pytest.mark.asyncio
    async def test_on_call_tool_timeout(self, mock_proxy_client):
        middleware = session.ColabProxyMiddleware(mock_proxy_client)
        context = Mock()
        context.fastmcp_context.report_progress = AsyncMock()
        context.message.name = session.INJECTED_TOOL_NAME
        mock_proxy_client.is_connected.return_value = False
        mock_proxy_client.await_proxy_connection = AsyncMock()
        call_next = AsyncMock()

        result = await middleware.on_call_tool(context, call_next)

        mock_proxy_client.await_proxy_connection.assert_called_once()
        assert result.structured_content == {"result": False}


class TestCheckSessionProxyToolFn:
    @pytest.mark.asyncio
    async def test_connected(self):
        ctx = Mock()
        ctx.get_state.side_effect = (
            lambda k: True if k == session.FE_CONNECTED_KEY else None
        )
        assert await session.check_session_proxy_tool_fn(ctx) is True

    @pytest.mark.asyncio
    async def test_disconnected(self, mock_webbrowser):
        ctx = Mock()

        def get_state(k):
            if k == session.FE_CONNECTED_KEY:
                return False
            if k == session.PROXY_TOKEN_KEY:
                return "test-token"
            if k == session.PROXY_PORT_KEY:
                return 1234
            return None

        ctx.get_state.side_effect = get_state
        assert await session.check_session_proxy_tool_fn(ctx) is False
        mock_webbrowser.assert_called_once()
        args, _ = mock_webbrowser.call_args
        assert "mcpProxyToken=test-token" in args[0]
        assert "mcpProxyPort=1234" in args[0]


class TestColabProxyClient:
    def test_is_connected(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.is_connected() is False
        mock_wss.connection_live.set()
        assert client.is_connected() is False
        client.proxy_mcp_client = Mock()
        assert client.is_connected() is True

    def test_client_factory_connection_live(self, mock_wss):
        mock_wss.connection_live.set()
        client = session.ColabProxyClient(mock_wss)
        client.proxy_mcp_client = Mock()

        assert client.client_factory() is client.proxy_mcp_client

    def test_client_factory_connection_not_live(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        assert client.client_factory() is client.stubbed_mcp_client

    @pytest.mark.asyncio
    async def test_await_proxy_connection(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        client._start_task = asyncio.create_task(asyncio.sleep(0.01))
        mock_wss.connection_live.set()
        with patch("colab_mcp.session.UI_CONNECTION_TIMEOUT", 0.1):
            await client.await_proxy_connection()
        await client._start_task

    @pytest.mark.asyncio
    async def test_await_proxy_connection_timeout_keeps_start_task(self, mock_wss):
        """A timed out wait must not cancel the proxy start task, so a later retry can connect."""
        client = session.ColabProxyClient(mock_wss)
        started = asyncio.Event()

        async def start():
            await started.wait()

        client._start_task = asyncio.create_task(start())
        with patch("colab_mcp.session.UI_CONNECTION_TIMEOUT", 0.05):
            await client.await_proxy_connection()
        assert not client._start_task.cancelled()

        mock_wss.connection_live.set()
        started.set()
        with patch("colab_mcp.session.UI_CONNECTION_TIMEOUT", 1):
            await client.await_proxy_connection()
        assert client._start_task.done() and not client._start_task.cancelled()

    @pytest.mark.asyncio
    @patch("colab_mcp.session.Client")
    @patch("colab_mcp.session.ColabTransport", spec=session.ColabTransport)
    async def test_start_proxy_client(
        self, mock_colab_transport, mock_client, mock_wss
    ):
        mock_client.return_value.__aenter__ = AsyncMock()
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        async with client:
            await client._start_task

        mock_colab_transport.assert_called_once_with(mock_wss)
        mock_client.assert_called_with(mock_colab_transport.return_value)


class TestColabTransport:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ClientSession")
    async def test_connect_session(self, mock_client_session, mock_wss):
        transport = session.ColabTransport(mock_wss)
        mock_client_session.return_value.__aenter__ = AsyncMock()
        async with transport.connect_session(foo="bar") as client_session:
            assert (
                client_session
                == mock_client_session.return_value.__aenter__.return_value
            )

        mock_client_session.assert_called_once_with(
            mock_wss.read_stream, mock_wss.write_stream, foo="bar"
        )


class TestColabSessionProxy:
    @pytest.mark.asyncio
    @patch("colab_mcp.session.ToolInjectionMiddleware")
    @patch("colab_mcp.session.ColabWebSocketServer")
    @patch("colab_mcp.session.ColabProxyClient")
    @patch("colab_mcp.session.ColabProxyMiddleware")
    async def test_start_proxy_server(
        self,
        mock_colab_proxy_middleware,
        mock_colab_proxy_client,
        mock_colab_web_socket_server,
        mock_tool_injection_middleware,
    ):
        mock_colab_web_socket_server.return_value.__aenter__ = AsyncMock()
        mock_colab_proxy_client.return_value.__aenter__ = AsyncMock()
        proxy = session.ColabSessionProxy()
        await proxy.start_proxy_server()
        mock_colab_proxy_client.assert_called_once()
        assert proxy.proxy_server is not None
        mock_colab_proxy_middleware.assert_called_once()
        mock_tool_injection_middleware.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup(self):
        proxy = session.ColabSessionProxy()
        proxy._exit_stack = AsyncMock()
        await proxy.cleanup()
        proxy._exit_stack.aclose.assert_called_once()


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="


def _run_result(outputs, structured=False):
    payload = {"outputs": outputs}
    return session.ToolResult(
        content=[session.TextContent(type="text", text=session.json.dumps(payload))],
        structured_content=payload if structured else None,
    )


class TestCleanNotebookResult:
    def test_image_becomes_image_content(self):
        result = _run_result(
            [
                {
                    "output_type": "display_data",
                    "data": {
                        "text/plain": ["<Figure size 300x200 with 1 Axes>"],
                        "image/png": PNG_B64 + "\n",
                    },
                }
            ]
        )
        cleaned = session.clean_notebook_result(result)
        text, image = cleaned.content
        assert image.type == "image"
        assert image.mimeType == "image/png"
        assert image.data == PNG_B64
        assert PNG_B64 not in text.text
        assert "attached as image #1" in text.text

    def test_ansi_removed_from_traceback(self):
        result = _run_result(
            [
                {
                    "output_type": "error",
                    "ename": "ZeroDivisionError",
                    "evalue": "division by zero",
                    "traceback": [
                        "\u001b[0;31mZeroDivisionError\u001b[0m: division by zero"
                    ],
                }
            ],
            structured=True,
        )
        cleaned = session.clean_notebook_result(result)
        assert cleaned.structured_content["outputs"][0]["traceback"] == [
            "ZeroDivisionError: division by zero"
        ]
        assert "\u001b" not in cleaned.content[0].text
        # the original result is not mutated
        assert "\u001b" in result.structured_content["outputs"][0]["traceback"][0]

    def test_get_cells_outputs_cleaned(self):
        payload = {
            "cells": [
                {"cell_type": "markdown", "id": "a", "source": ["# hi"]},
                {
                    "cell_type": "code",
                    "id": "b",
                    "outputs": [
                        {
                            "output_type": "display_data",
                            "data": {"image/png": [PNG_B64]},
                        }
                    ],
                },
            ]
        }
        result = session.ToolResult(
            content=[session.TextContent(type="text", text=session.json.dumps(payload))]
        )
        cleaned = session.clean_notebook_result(result)
        assert [c.type for c in cleaned.content] == ["text", "image"]
        assert cleaned.structured_content is None

    def test_non_notebook_result_untouched(self):
        result = session.ToolResult(
            content=[session.TextContent(type="text", text='{"newCellId": "x"}')]
        )
        assert session.clean_notebook_result(result) is result


class TestColabOutputMiddleware:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["run_code_cell", "colab_get_cells"])
    async def test_cleans_output_tools(self, name):
        context = Mock()
        context.message.name = name
        call_next = AsyncMock(
            return_value=_run_result(
                [{"output_type": "display_data", "data": {"image/png": PNG_B64}}]
            )
        )
        result = await session.ColabOutputMiddleware().on_call_tool(context, call_next)
        assert result.content[-1].type == "image"

    @pytest.mark.asyncio
    async def test_skips_other_tools(self):
        context = Mock()
        context.message.name = "add_code_cell"
        original = _run_result([])
        call_next = AsyncMock(return_value=original)
        assert (
            await session.ColabOutputMiddleware().on_call_tool(context, call_next)
            is original
        )


class TestStaticProxyTools:
    @pytest.mark.asyncio
    async def test_get_cells_uses_colab_argument_names(self):
        proxy_client = Mock()
        proxy_client.call_colab_tool = AsyncMock()
        tools = {t.name: t for t in session.build_static_proxy_tools(proxy_client)}
        await tools["colab_get_cells"].fn(cellIndexStart=1, cellIndexEnd=2)
        proxy_client.call_colab_tool.assert_called_once_with(
            "get_cells",
            {"cellIndexStart": 1, "cellIndexEnd": 2, "includeOutputs": False},
        )

    @pytest.mark.asyncio
    async def test_call_colab_tool_passes_content_through(self, mock_wss):
        client = session.ColabProxyClient(mock_wss)
        mock_wss.connection_live.set()
        image = session.ImageContent(type="image", data=PNG_B64, mimeType="image/png")
        client.proxy_mcp_client = Mock()
        client.proxy_mcp_client.call_tool = AsyncMock(
            return_value=Mock(content=[image], structured_content=None, meta=None)
        )
        result = await client.call_colab_tool("run_code_cell", {"cellId": "x"})
        assert result.content == [image]


def _cells_proxy_client(cells):
    proxy_client = Mock()
    payload = {"cells": cells}
    get_cells_result = session.ToolResult(
        content=[session.TextContent(type="text", text=session.json.dumps(payload))]
    )
    proxy_client.call_colab_tool = AsyncMock(return_value=get_cells_result)
    tools = {t.name: t for t in session.build_static_proxy_tools(proxy_client)}
    return proxy_client, tools


NOTEBOOK_CELLS = [
    {"cell_type": "text", "id": "md1", "source": ["# Title\n", "intro"]},
    {"cell_type": "code", "id": "c1", "source": ["x = 1\n", "print(x)"]},
    {"cell_type": "code", "id": "c2", "source": "y = x + x"},
    {"cell_type": "code", "id": "c3", "source": []},
]


class TestEditCell:
    @pytest.mark.asyncio
    async def test_replaces_unique_text(self):
        proxy_client, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        result = await tools["colab_edit_cell"].fn(
            cellId="c1", old_text="x = 1", new_text="x = 2"
        )
        assert result == {"cellId": "c1", "cellIndex": 1, "replaced": 1}
        proxy_client.call_colab_tool.assert_called_with(
            "update_cell", {"cellId": "c1", "content": "x = 2\nprint(x)"}
        )

    @pytest.mark.asyncio
    async def test_ambiguous_text_is_rejected(self):
        proxy_client, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        with pytest.raises(session.ToolError, match="appears 2 times"):
            await tools["colab_edit_cell"].fn(cellId="c2", old_text="x", new_text="z")
        assert proxy_client.call_colab_tool.call_count == 1  # only get_cells

    @pytest.mark.asyncio
    async def test_missing_text_is_rejected(self):
        _, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        with pytest.raises(session.ToolError, match="not found in cell c1"):
            await tools["colab_edit_cell"].fn(cellId="c1", old_text="nope", new_text="")

    @pytest.mark.asyncio
    async def test_unknown_cell_is_rejected(self):
        _, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        with pytest.raises(session.ToolError, match="zzz was not found"):
            await tools["colab_edit_cell"].fn(cellId="zzz", old_text="x", new_text="y")

    @pytest.mark.asyncio
    async def test_empty_old_text_is_rejected(self):
        proxy_client, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        with pytest.raises(session.ToolError, match="must not be empty"):
            await tools["colab_edit_cell"].fn(cellId="c1", old_text="", new_text="y")
        proxy_client.call_colab_tool.assert_not_called()


class TestNotebookOutline:
    @pytest.mark.asyncio
    async def test_one_line_per_cell(self):
        proxy_client, tools = _cells_proxy_client(
            NOTEBOOK_CELLS + [{"cell_type": "code", "id": "long", "source": "a" * 100}]
        )
        outline = await tools["colab_notebook_outline"].fn()
        proxy_client.call_colab_tool.assert_called_once_with("get_cells", {})
        assert outline.splitlines() == [
            "5 cells",
            "[0] text md1: # Title↵intro",
            "[1] code c1: x = 1↵print(x)",
            "[2] code c2: y = x + x",
            "[3] code c3: (empty)",
            "[4] code long: " + "a" * 60 + "...",
        ]


class TestSearchCells:
    @pytest.mark.asyncio
    async def test_plain_text_search(self):
        _, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        result = await tools["colab_search_cells"].fn(pattern="x")
        assert result["matchCount"] == 4
        assert result["truncated"] is False
        assert [(m["cellIndex"], m["cellId"]) for m in result["matches"]] == [
            (1, "c1"),
            (1, "c1"),
            (2, "c2"),
            (2, "c2"),
        ]
        assert result["matches"][0]["context"] == "x = 1↵print(x)"

    @pytest.mark.asyncio
    async def test_pattern_is_literal_unless_regex(self):
        _, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        literal = await tools["colab_search_cells"].fn(pattern="x.")
        assert literal["matchCount"] == 0
        regex = await tools["colab_search_cells"].fn(
            pattern=r"print\(\w\)", use_regex=True
        )
        assert [m["match"] for m in regex["matches"]] == ["print(x)"]

    @pytest.mark.asyncio
    async def test_case_insensitive_and_truncation(self):
        _, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        result = await tools["colab_search_cells"].fn(
            pattern="TITLE", case_sensitive=False
        )
        assert result["matches"][0]["cellId"] == "md1"
        capped = await tools["colab_search_cells"].fn(pattern="x", max_results=1)
        assert capped["matchCount"] == 4
        assert capped["truncated"] is True
        assert len(capped["matches"]) == 1

    @pytest.mark.asyncio
    async def test_invalid_regex(self):
        _, tools = _cells_proxy_client(NOTEBOOK_CELLS)
        with pytest.raises(session.ToolError, match="Invalid regex"):
            await tools["colab_search_cells"].fn(pattern="(", use_regex=True)


def test_top_level_server_has_instructions():
    import colab_mcp

    assert colab_mcp.mcp.instructions == session.SERVER_INSTRUCTIONS
    assert "60 seconds" in colab_mcp.mcp.instructions
