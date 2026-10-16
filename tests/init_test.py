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

import logging
import os

import pytest

import colab_mcp


@pytest.mark.parametrize(
    "argv, expected",
    [
        ([], True),
        (["-p"], True),
        (["--enable-proxy"], True),
        (["--no-enable-proxy"], False),
    ],
)
def test_enable_proxy_flag(argv, expected):
    assert colab_mcp.parse_args(argv).enable_proxy is expected


def test_percent_in_log_dir_is_kept(tmp_path, monkeypatch):
    logdir = tmp_path / "run%d"
    captured = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: captured.update(kw))
    colab_mcp.init_logger(str(logdir))
    assert os.path.dirname(captured["filename"]) == str(logdir)
    assert os.path.basename(captured["filename"]).startswith("colab-mcp.")
