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

"""End-to-end tests of the proxy client against a fake Colab frontend."""

import asyncio
import json

import pytest
import websockets
from mcp import MCPError

from colab_mcp.session import ColabProxyClient
from colab_mcp.websocket_server import COLAB, ColabWebSocketServer


class FakeColabTab:
    """Plays the Colab tab: connects to the server and answers as an MCP server."""

    def __init__(self, wss, name, reply_to_calls=True):
        self.wss = wss
        self.name = name
        self.reply_to_calls = reply_to_calls
        self.methods = []
        self.call_received = asyncio.Event()

    async def connect(self):
        self.ws = await websockets.connect(
            f"ws://localhost:{self.wss.port}",
            origin=COLAB,
            subprotocols=["mcp"],
            additional_headers={"Authorization": f"Bearer {self.wss.token}"},
        )
        self._task = asyncio.create_task(self._serve())

    async def close(self):
        await self.ws.close()
        await self._task

    async def _reply(self, msg, result):
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        )

    async def _serve(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                method = msg.get("method")
                self.methods.append(method)
                if method == "initialize":
                    await self._reply(
                        msg,
                        {
                            "protocolVersion": msg["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": self.name, "version": "0"},
                        },
                    )
                elif method == "tools/list":
                    await self._reply(
                        msg,
                        {
                            "tools": [
                                {"name": "get_cells", "inputSchema": {"type": "object"}}
                            ]
                        },
                    )
                elif method == "tools/call":
                    self.call_received.set()
                    if self.reply_to_calls:
                        await self._reply(
                            msg, {"content": [{"type": "text", "text": self.name}]}
                        )
        except websockets.ConnectionClosed:
            pass


async def _connected(proxy):
    await asyncio.wait_for(proxy._ready.wait(), 5)
    return proxy.is_connected()


@pytest.mark.asyncio
async def test_reopened_tab_gets_its_own_session():
    async with ColabWebSocketServer() as wss, ColabProxyClient(wss) as proxy:
        tab1 = FakeColabTab(wss, "tab1")
        await tab1.connect()
        assert await _connected(proxy)
        result = await asyncio.wait_for(proxy.call_colab_tool("get_cells", {}), 5)
        assert result.content[0].text == "tab1"

        await tab1.close()
        await asyncio.sleep(0.1)
        assert not proxy.is_connected()

        tab2 = FakeColabTab(wss, "tab2")
        await tab2.connect()
        assert await _connected(proxy)
        result = await asyncio.wait_for(proxy.call_colab_tool("get_cells", {}), 5)
        assert result.content[0].text == "tab2"
        assert tab2.methods[0] == "initialize"
        await tab2.close()


@pytest.mark.asyncio
async def test_pending_call_fails_when_tab_closes():
    async with ColabWebSocketServer() as wss, ColabProxyClient(wss) as proxy:
        tab = FakeColabTab(wss, "tab", reply_to_calls=False)
        await tab.connect()
        assert await _connected(proxy)

        call = asyncio.create_task(
            proxy.call_colab_tool("run_code_cell", {"cellId": "x"})
        )
        await asyncio.wait_for(tab.call_received.wait(), 5)
        await tab.close()

        with pytest.raises(MCPError, match="Connection closed"):
            await asyncio.wait_for(call, 5)
