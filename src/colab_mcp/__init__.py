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
import getpass
import logging
import os
import tempfile
import sys

from fastmcp import FastMCP
from fastmcp.utilities import logging as fastmcp_logger

from colab_mcp.session import ColabSessionProxy, SERVER_INSTRUCTIONS


# instructions must live on the top-level server; those of mounted servers never reach the client
mcp = FastMCP(name="ColabMCP", instructions=SERVER_INSTRUCTIONS)


def _user_name():
    try:
        return getpass.getuser()
    except OSError:  # no login name, e.g. a container user without a passwd entry
        return str(os.getuid()) if hasattr(os, "getuid") else "user"


# one directory per user; a mkdtemp() default left a new empty folder behind on every start
DEFAULT_LOG_DIR = os.path.join(tempfile.gettempdir(), f"colab-mcp-logs-{_user_name()}")


def _private_default_log_dir():
    """Returns DEFAULT_LOG_DIR if it is ours and private, else a fresh mkdtemp() directory."""
    try:
        os.makedirs(DEFAULT_LOG_DIR, mode=0o700, exist_ok=True)
    except FileExistsError:  # a file or dangling link is in the way
        return tempfile.mkdtemp(prefix="colab-mcp-logs-")
    if not hasattr(os, "getuid"):
        return DEFAULT_LOG_DIR
    if (
        os.path.islink(DEFAULT_LOG_DIR)
        or os.lstat(DEFAULT_LOG_DIR).st_uid != os.getuid()
    ):
        # someone else made it first; never write logs into a directory they control
        return tempfile.mkdtemp(prefix="colab-mcp-logs-")
    os.chmod(DEFAULT_LOG_DIR, 0o700)
    return DEFAULT_LOG_DIR


def init_logger(logdir):
    if logdir == DEFAULT_LOG_DIR:
        logdir = _private_default_log_dir()
    else:
        os.makedirs(logdir, exist_ok=True)
    # only the file name is a strftime format, so a "%" in logdir stays as is
    log_filename = os.path.join(
        logdir, datetime.datetime.now().strftime("colab-mcp.%Y-%m-%d_%H-%M-%S.log")
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
        help="enable the runtime proxy (enabled by default; --no-enable-proxy disables it).",
        action=argparse.BooleanOptionalAction,
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
