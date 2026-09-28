# 重排 `/v1/rerank`

2026-09-27 接入 Qwen3-VL-Reranker(2B / 8B)。给一个 query 和一组候选,返回按相关性降序的分数 ——
用在向量召回之后的精排。

## 调用

Cohere / Jina 兼容形(与 vLLM 原生 `/v1/rerank` 一致):

```bash
curl http://<engine>:8000/v1/rerank \
  -H "Authorization: Bearer sk-..." -H "Content-Type: application/json" \
  -d '{
    "model": "nous-qwen3-vl-reranker-2b",
    "query": "橘猫在窗台晒太阳",
    "documents": ["一只橘色的猫趴在窗边", "今天股市大跌"],
    "top_n": 2
  }'
```

```json
{"id": "...", "model": "nous-qwen3-vl-reranker-2b",
 "usage": {"prompt_tokens": 123, "total_tokens": 123},
 "results": [{"index": 0, "relevance_score": 0.89, "document": {"text": "..."}}, ...]}
```

- `query` 与每个 `documents[i]` 可以是字符串,也可以是 `{"content": [part, ...]}`:
  part 只收 `{"type":"text","text":...}` 与 `{"type":"image_url","image_url":{"url":...}}`
  (与 skill-runs preview 同一白名单与尺寸上限;视频不收)。
- 图片 URL 只收 `data:` 与公网 https(与 chat 同一套 SSRF 校验,内网/回环地址 400 `unsafe_image_url`)。
- `instruction`(可选,≤ 4096 字):任务指令,进模板的 `<Instruct>`;不给时用默认
  「Given a search query, retrieve relevant candidates that answer the query.」。也可写成
  `chat_template_kwargs: {"instruction": ...}`(vLLM 的等价写法;该 dict 只收这一个键)。
  **按业务写指令收益明显**:nous-app 557 对图文实测,2B 的 AUC 0.658 → 0.815。
- `return_documents` 缺省时 vLLM 会把每个 document 原样回显(含整段 base64 图片),
  图文重排建议显式传 `false`。
- 上限:documents ≤ 256,每个 content ≤ 32 个 part;`top_n` > 0;`return_documents` 透传。
- `model` 必须是 **rerank 类**服务,否则 400 `not_a_rerank_model`。

## 行为

- 鉴权 / grant / 限流 / 配额 / 用量记账与 chat 共用(`src/api/chat_invoke.py`、`src/api/service_access.py`);
  计量维度 `tokens`(prompt tokens)。
- **数据面只读**:模型没加载即刻 503 `model_not_ready`,不在请求路径上加载
  (`tests/test_data_plane_readonly.py` 锁住 `routes/rerank.py`)。
- 上游(vLLM)非 200 原样透出,不计费。

## 模型

| 服务 | yaml | 落卡 | 加载 |
|---|---|---|---|
| `nous-qwen3-vl-reranker-2b` | `configs/models.d/qwen3_vl_reranker_2b.yaml` | GPU 0(Pro 5000) | 常驻 |
| `nous-qwen3-vl-reranker-8b` | `configs/models.d/qwen3_vl_reranker_8b.yaml` | GPU 1(Pro 6000,与 ComfyUI 共卡) | 按需,ttl 1h |

vLLM 以 pooling runner + `hf-overrides`(`Qwen3VLForSequenceClassification`,yes/no token 分类头)+
仓库里的 score 模板 `backend/configs/chat_templates/qwen3_vl_reranker.jinja`(= vLLM 上游 examples 的
副本)起。**不用模型自带的 `additional_chat_templates/reranker.jinja`**:它不读 `instruction` 变量,
且 vLLM score 路径下不输出 `<|im_start|>assistant`,yes/no 的 logit 取错位置,分数被整体压缩
(2026-09-27 修之前的所有分数都受影响,阈值要重标)。显存标定写在两份 yaml 的注释里。

**两档分数尺度不同**(同一组数据 2B 0.90/0.63/0.43,8B 0.27/0.17/0.03):只在同一模型内部比排序,
别拿一个阈值同时套两档。

## 真机验证

```bash
cd backend && uv run python tests/manual/verify_rerank.py --service nous-qwen3-vl-reranker-2b [--image cat.jpg]
```
