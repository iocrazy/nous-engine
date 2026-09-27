"""服务层 API spec PR-4:输出交付 TTL 归服务层配置(不再是出图节点 widget)。

用户:URL 有效期不该是节点的事,该是工作流 API 的功能。url_ttl_seconds widget 从出图节点
(当时 3 个:flux2 VAE decode / seedvr2 增强 / image-io;前两个已随自建图像引擎删除)删掉,改读 get_settings().IMAGE_URL_TTL_SECONDS。
"""
from __future__ import annotations

import pathlib

import yaml

_ROOT = pathlib.Path(__file__).parent.parent
_SRC = _ROOT / "src"
_NODES = _ROOT / "nodes"


def test_config_has_image_url_ttl():
    from src.config import get_settings
    assert get_settings().IMAGE_URL_TTL_SECONDS == 3600


def test_image_io_node_yaml_no_url_ttl_widget():
    """出图节点 image-io 的 node.yaml 不再有 url_ttl_seconds widget(仍是合法 yaml)。

    2026-09-26 从 test_node_yaml_no_url_ttl_widget 拆出:flux2-components / seedvr2 两个节点包
    随自建图像引擎删除,image-io 留着,断言逐字保留。"""
    for rel in ("image-io/node.yaml",):
        text = (_NODES / rel).read_text()
        assert "url_ttl_seconds" not in text, f"{rel} 仍有 url_ttl_seconds widget"
        d = yaml.safe_load(text)
        assert d.get("nodes"), f"{rel} 不是合法 node yaml"


def test_image_io_reads_ttl_from_config_not_node():
    """落盘/签 URL 处读 config,不再 node.inputs.get('url_ttl_seconds')。

    2026-09-26 从 test_code_reads_ttl_from_config_not_node 拆出:runner 的图像出图段已随
    图像派发删除(不再签图像 URL),「runner 读 IMAGE_URL_TTL_SECONDS」那条随之删;
    其余断言逐字保留。"""
    runner = (_SRC / "runner/runner_process.py").read_text()
    assert 'node.inputs.get("url_ttl_seconds")' not in runner
    io_exec = (_NODES / "image-io/executor.py").read_text()
    assert "get_settings().IMAGE_URL_TTL_SECONDS" in io_exec
    assert 'data.get("url_ttl_seconds")' not in io_exec
