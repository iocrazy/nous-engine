"""/v1/embeddings 端点 + embedding 模型注册 wiring(2026-06-12 embedding 模态接入)。

真推理(vLLM pooling 实例)standalone 验过:Qwen3-Embedding-4B 2560 维,
同义句余弦 0.72 > 无关句 0.41。CI 只验注册/路由/参数 wiring。
"""
from __future__ import annotations

import pathlib

from src.config import collect_model_entries

_ROOT = pathlib.Path(__file__).parent.parent


def test_models_yaml_registers_embedding_models():
    # 模型定义已迁到 configs/models.d/<id>.yaml(2026-06-20);走 collect_model_entries 单一来源。
    # 2026-09-11:embedding 收成顶层桶 `embedding/<MODEL>`(原 `text/embedding/`,`text/`
    # 下只有这一个 bucket,scanner/metadata 两处 depth-3 特例一并删掉)。
    by_id = {m["id"]: m for m in collect_model_entries(_ROOT / "configs/models.yaml")}
    for mid, subdir in (("qwen3_embedding_4b", "Qwen3-Embedding-4B"),
                        ("qwen3_embedding_8b", "Qwen3-Embedding-8B"),
                        ("wemm_embedding_4b", "WeMM-Embedding-4B"),
                        ("wemm_embedding_9b", "WeMM-Embedding-9B")):
        m = by_id.get(mid)
        assert m, f"models.yaml 缺 {mid}"
        assert m["type"] == "embedding"
        assert m["paths"]["main"] == f"embedding/{subdir}"
        # pooling runner 必须显式给(否则 vLLM 当生成模型起,/v1/embeddings 404)
        assert m.get("params", {}).get("vllm_runner") == "pooling", f"{mid} 缺 vllm_runner=pooling"


def test_wemm_passes_embedding_chat_template():
    """WeMM 必须透传 `--chat-template …/embedding_chat_template.jinja`。

    该模板在末尾追加 `<embedding>` token,是官方 vLLM serve 命令的一部分;不给的话
    vLLM 回退到 tokenizer_config 里的**生成用**模板,messages 形式的请求算出来的向量
    与官方 SentenceTransformer 对不上。纯文本 `input:` 路径不过模板,不受影响。
    """
    by_id = {m["id"]: m for m in collect_model_entries(_ROOT / "configs/models.yaml")}
    for mid in ("wemm_embedding_4b", "wemm_embedding_9b"):
        args = by_id[mid].get("params", {}).get("vllm_args", {})
        tmpl = args.get("chat-template")
        assert tmpl, f"{mid} 缺 vllm_args.chat-template"
        assert tmpl.endswith("/embedding_chat_template.jinja"), tmpl
        # 必须走 `{model_dir}` 占位符,不许把 LOCAL_MODELS_PATH 写死进 git 里的 yaml
        # (model_roots.yaml 文件头:重格/搬盘只改 .env 的两个根)。
        assert tmpl.startswith("{model_dir}/"), f"{mid} 的模板路径写死了绝对路径:{tmpl}"


def test_vllm_adapter_passes_runner_flag():
    """vllm_runner param → --runner 透传;缺省不传(生成模型零回归)。源码检查。"""
    src = (_ROOT / "src/services/inference/llm_vllm.py").read_text()
    assert "vllm_runner: str | None = None" in src
    assert '"--runner", self._vllm_runner' in src


def test_embeddings_route_registered():
    """/v1/embeddings 路由在 openai_compat 注册,解析链同 chat(M:N grant → vLLM 透传)。"""
    src = (_ROOT / "src/api/routes/openai_compat.py").read_text()
    assert '@router.post("/v1/embeddings")' in src
    blk = src[src.find('@router.post("/v1/embeddings")'):src.find("# --- /v1/images/generations ---")]
    assert "resolve_target_service" in blk, "缺 M:N 服务解析"
    assert "get_vllm_base_url" in blk, "缺 vLLM base_url 查找(只读 base_url 查找)"
    assert "record_llm_usage" in blk, "缺 usage 记账"
    assert 'body["model"] = ""' in blk, "必须置空 model 让 vLLM 用自己的(同 chat)"
