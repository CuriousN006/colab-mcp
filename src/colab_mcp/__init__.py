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

import argparse
import asyncio
import datetime
import logging
import os
import tempfile
import sys

from fastmcp import FastMCP
from fastmcp.utilities import logging as fastmcp_logger

from colab_mcp.session import ColabSessionProxy, SERVER_INSTRUCTIONS


# instructions must live on the top-level server; those of mounted servers never reach the client
mcp = FastMCP(name="ColabMCP", instructions=SERVER_INSTRUCTIONS)

# one shared directory; a mkdtemp() default left a new empty folder behind on every start
DEFAULT_LOG_DIR = os.path.join(tempfile.gettempdir(), "colab-mcp-logs")


def init_logger(logdir):
    os.makedirs(logdir, exist_ok=True)
    log_filename = datetime.datetime.now().strftime(
        f"{logdir}/colab-mcp.%Y-%m-%d_%H-%M-%S.log"
    )
    logging.basicConfig(
        format="%(asctime)s %(levelname)s:%(message)s",
        datefmt="%m/%d/%Y %I:%M:%S %p",
        filename=log_filename,
        level=logging.INFO,  # Set the minimum logging level to capture
    )
    fastmcp_logger.get_logger("colab-mcp").info("logging to %s" % log_filename)


def parse_args(v):
    parser = argparse.ArgumentParser(
        description="ColabMCP is an MCP server that lets you interact with Colab."
    )
    parser.add_argument(
        "-l",
        "--log",
        help="if set, use this directory as a location for logfiles (if unset, will log to %s)"
        % DEFAULT_LOG_DIR,
        action="store",
        default=DEFAULT_LOG_DIR,
    )
    parser.add_argument(
        "-p",
        "--enable-proxy",
        help="if set, enable the runtime proxy (enabled by default).",
        action="store_true",
        default=True,
    )
    return parser.parse_args(v)


async def main_async():
    args = parse_args(sys.argv[1:])
    init_logger(args.log)

    if args.enable_proxy:
        logging.info("enabling session proxy tools")
        session_mcp = ColabSessionProxy()
        await session_mcp.start_proxy_server()
        for middleware in session_mcp.middleware:
            mcp.add_middleware(middleware)

    try:
        await mcp.run_async()

    finally:
        if args.enable_proxy:
            await session_mcp.cleanup()


def main() -> None:
    asyncio.run(main_async())
