"""Qwen3-VL-Reranker 的 score 模板(2026-09-27 nous-app 实测回报后修)。

模型仓库自带的 `additional_chat_templates/reranker.jinja` 在 vLLM 0.28 的 score 路径下有两处错:
- 只从 system 消息取指令,不读 vLLM 传进来的 `instruction` 变量 → 请求带 instruction 被静默忽略;
- `<|im_start|>assistant\\n` 只在 add_generation_prompt 时才加,而 vLLM score 路径不传它 →
  yes/no 的 logit 取在错位置,分数整体被压缩。
改用 vLLM 上游 examples/pooling/score/template/qwen3_vl_reranker.jinja 的副本(仓库内)。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
TEMPLATE = CONFIGS / "chat_templates" / "qwen3_vl_reranker.jinja"
MSGS = [{"role": "query", "content": "橘猫"}, {"role": "document", "content": "窗台上的猫"}]


def test_template_text_reads_instruction_and_closes_on_assistant():
    """不依赖 jinja2 的兜底(CI 环境没装 jinja2,下面几条渲染用例会 skip)。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "instruction | default(" in text
    assert text.rstrip("\n").endswith("<|im_start|>assistant")
    assert "add_generation_prompt" not in text


def _render(**kw) -> str:
    jinja2 = pytest.importorskip("jinja2")
    # 与 transformers apply_chat_template 同配置(trim_blocks / lstrip_blocks)
    return jinja2.Environment(trim_blocks=True, lstrip_blocks=True).from_string(TEMPLATE.read_text(encoding="utf-8")).render(messages=MSGS, **kw)


def test_template_uses_instruction_variable():
    out = _render(instruction="判断画面是否符合描述")
    assert "<Instruct>: 判断画面是否符合描述<Query>:橘猫" in out


def test_template_default_instruction_when_absent():
    assert "<Instruct>: Given a search query, retrieve relevant candidates" in _render()


def test_template_ends_with_assistant_turn_without_generation_prompt():
    """vLLM score 路径不传 add_generation_prompt —— 模板必须自己收在 assistant 起始处。"""
    assert _render().endswith("<|im_start|>assistant\n")


@pytest.mark.parametrize("mid", ["qwen3_vl_reranker_2b", "qwen3_vl_reranker_8b"])
def test_reranker_yaml_uses_repo_template(mid):
    cfg = yaml.safe_load((CONFIGS / "models.d" / f"{mid}.yaml").read_text(encoding="utf-8"))
    assert cfg["params"]["vllm_args"]["chat-template"] == "{configs_dir}/chat_templates/qwen3_vl_reranker.jinja"
