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

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
import asyncio
import logging
import mcp.types as types
from mcp.shared.message import SessionMessage
from pydantic_core import ValidationError
import secrets
from urllib.parse import parse_qs, urlsplit
import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol


COLAB = "https://colab.research.google.com"
COLAB_ALT_DOMAIN = "https://colab.google.com"
SCRATCH_PATH = "/notebooks/empty.ipynb"


class ColabWebSocketServer:
    """
    A WebSocket server designed to accept a single connection specifically
    from a Google Colab session (colab.google.com).
    """

    def __init__(self, host="localhost"):
        self.host = host
        self.port = 0
        self.connection_lock = asyncio.Lock()
        self.connection_live = asyncio.Event()
        self.allowed_origins = [COLAB, COLAB_ALT_DOMAIN]
        self._server: websockets.Server | None = None
        # set when the current connection ends; replaced for every new connection
        self.connection_done = asyncio.Event()

        self.read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
        self._read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
        self.write_stream: MemoryObjectSendStream[SessionMessage]
        self._write_stream_reader: MemoryObjectReceiveStream[SessionMessage]
        self._open_streams()
        self.token = secrets.token_urlsafe(16)

    def _open_streams(self):
        """Creates fresh streams, so that each connection gets its own MCP session."""
        self._read_stream_writer, self.read_stream = anyio.create_memory_object_stream(
            0
        )
        self.write_stream, self._write_stream_reader = (
            anyio.create_memory_object_stream(0)
        )

    async def _read_from_socket(self, websocket):
        """Listens to the socket and puts messages into the read stream."""
        async for msg in websocket:
            try:
                client_message = types.jsonrpc_message_adapter.validate_json(
                    msg, by_name=False
                )
            except ValidationError as exc:
                await self._read_stream_writer.send(exc)
                continue
            await self._read_stream_writer.send(SessionMessage(client_message))

    async def _write_to_socket(self, websocket):
        """Reads from the write stream and sends over the socket."""
        try:
            while True:
                # Wait for a message from the application
                msg = await self._write_stream_reader.receive()

                try:
                    json_obj = msg.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await websocket.send(json_obj)
                except ConnectionClosed:
                    break
        except (anyio.ClosedResourceError, anyio.EndOfStream):
            # server closed write stream
            pass

    def _validate_authorization(self, websocket: ServerConnection, request: Request):
        query = parse_qs(urlsplit(request.path).query)
        if any(
            secrets.compare_digest(t, self.token) for t in query.get("access_token", [])
        ):
            return None
        try:
            headers: Headers = request.headers
            auth_header = headers.get("Authorization")
            if not auth_header:
                return Response(401, "Missing authorization", Headers([]))
            scheme, token = auth_header.split(None, 1)
            if scheme.lower() != "bearer":
                return Response(400, "Invalid authorization header", Headers([]))
        except ValueError:
            return Response(400, "Invalid header format", Headers([]))
        if secrets.compare_digest(token, self.token):
            return None
        return Response(403, "Bad authorization token", Headers([]))

    async def _connection_handler(self, websocket: ServerConnection):
        """
        Handles incoming websocket connections.
        Validates Origin and ensures single-client exclusivity.
        """
        if self.connection_lock.locked():
            logging.warning(
                f"Connection rejected: {websocket.remote_address}. A client is already connected"
            )
            await websocket.close(code=1013, reason="Server is busy")
            return

        async with self.connection_lock:
            if self.connection_done.is_set():
                # the previous session's streams are closed; start a new session
                self._open_streams()
                self.connection_done = asyncio.Event()
            read_stream_writer = self._read_stream_writer
            write_stream_reader = self._write_stream_reader
            reading_task = asyncio.create_task(self._read_from_socket(websocket))
            writing_task = asyncio.create_task(self._write_to_socket(websocket))
            try:
                self.connection_live.set()
                await asyncio.wait(
                    [reading_task, writing_task], return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                self.connection_live.clear()
                for task in (reading_task, writing_task):
                    task.cancel()
                for result in await asyncio.gather(
                    reading_task, writing_task, return_exceptions=True
                ):
                    if isinstance(result, ConnectionClosed):
                        logging.info(f"Connection closed: {result}")
                    elif isinstance(result, Exception):
                        logging.error(f"Unexpected error: {result}")
                # end of stream fails requests still waiting on Colab with "Connection closed"
                read_stream_writer.close()
                write_stream_reader.close()
                self.connection_done.set()

    async def __aenter__(self):
        self._server = await websockets.serve(
            self._connection_handler,
            host=self.host,
            port=0,
            subprotocols=[Subprotocol("mcp")],
            origins=self.allowed_origins,
            process_request=self._validate_authorization,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logging.info(f"Starting WebSocket server on ws://{self.host}:{self.port}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logging.info("Closing WebSocket server")
        if self._server:
            self._server.close()
            self.write_stream.close()
            self.read_stream.close()
            await self._server.wait_closed()
