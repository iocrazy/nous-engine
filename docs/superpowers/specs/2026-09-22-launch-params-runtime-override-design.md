# 模型启动参数的运行时覆盖 — 设计

**日期**:2026-09-22
**状态**:设计待用户确认
**动机**:用户两次指出「服务里调不了模型的关键参数(上下文长度、KV cache、
`gpu_memory_utilization`)」。调研发现入口**存在但是坏的**,且它的白名单本身有设计缺陷。

---

## 1. 现状(全部已在真机核实)

### 1.1 `PATCH /engines/{name}/launch-params` 是坏的

它写 `configs/models.yaml`,而模型定义 2026-06-20 已迁到 `models.d/<id>.yaml`,
那个文件现在只剩 `models: []` 空锚点。于是它遍历空列表、对**任何**模型都 404:

```
PATCH /api/v1/engines/qwen3_8_27b_uncensored_fp8/launch-params {"max_model_len":16384}
→ HTTP 404  "unknown engine: qwen3_8_27b_uncensored_fp8"
```

`grep launch-params tests/` **零结果** —— 没有测试覆盖,所以坏了没人发现。

### 1.2 即便修好,写 yaml 的方向也是错的

隔壁 `resident` 端点的 docstring 自己写明了原因:

> **持久到 DB 表 model_runtime_overrides**(数据加载统一 2026-06-16;不碰 git 跟踪的
> models.yaml)—— 否则 UI 设的常驻写进 yaml 是未提交本地改动,git checkout/pull/reset 冲掉。

### 1.3 前端几乎没有编辑器

`DashboardOverlay.tsx` 的 vLLM 面板里**只有一个 `Prefix Caching` 复选框**(:1641),
而且它正是那个 404 的调用方。不在服务页,也没有其他参数 —— 用户的抱怨完全准确。

### 1.4 ⚠️ 显存旋钮已经有了,而且比 `gpu_memory_utilization` 更好

`vram_budget_mode/value` 早在 override 表里,端点 `PATCH /engines/{name}/vram-budget`
**是好的**(2026-09-22 实测 200 + 落库)。它在**加载时**按实际那张卡换算:

```
llm_vllm.py:459  显存预算优先级:overlay vram_budget(percent/absolute)
                 > yaml gpu_memory_utilization > auto 公式
```

这正好根治本仓库反复踩的坑 —— util 是「占该卡总量」的比例,**换卡必须重算**:
- huihui 从 GPU 0 搬到 GPU 1:0.80 → 0.70
- FP8 开 256K:又要重算
- 2026-09-11 真事故:改了落卡忘改 util,模型在 Pro 6000 上抓 53.3 GiB,把 ComfyUI 挤到 19 GiB

**写绝对 GiB 就永远不用换算。**

---

## 2. 设计决策

### 2.1 `gpu_memory_utilization` **移出**可编辑白名单

现白名单里它赫然在列。把它做成 UI 按钮 = 把上面那个坑做成一个按钮。
PATCH 到它一律 400,并在错误里指向 `vram-budget`。

同理移除 **`tensor_parallel_size`** —— tp 是**放置结论**,由 `_placement` 决定
(CLAUDE.md:「tp 是放置结论的一部分,不是调优旋钮」),不是调优参数。

### 2.2 新增 `params` JSONB 覆盖列

真正缺的是 `params` 子 dict 里的**非显存**参数。新列只存被覆盖的键:

```json
{"max_model_len": 262144, "max_num_seqs": 8}
```

- 键不存在 = 未覆盖(回退 yaml)
- 整列 NULL = 全部未覆盖
- 清单个键 = 从 dict 里移除该键(端点收到显式 `null` 时删键)

**最终白名单**:`max_model_len` / `max_num_seqs` / `max_num_batched_tokens` /
`enable_prefix_caching` / `dtype` / `quantization`

### 2.3 ⚠️ 合并逻辑要改成嵌套深合并 —— 且有个隐蔽陷阱

`_apply_runtime_overrides`(`config.py:326`)现在只合并**顶层键**
(`_OVERRIDABLE_KEYS = resident, gpu, gpus, vram_budget`),用浅 `update()`。
而新参数在 `params` 子 dict 里,必须深合并。

**陷阱**:`copy_before_write=True` 那条路(给 `model_scanner._with_runtime_overrides`
的 TTL 缓存用)只做了 cfg 的**浅**拷贝,`params` 子 dict 仍是共享引用。
直接深合并会**写穿进 TTL 缓存** —— 正是该函数 docstring 警告过的
「覆盖值渗进缓存、被 TTL 烘死」的镜像问题。

所以 `copy_before_write` 时必须**连 `params` 一起浅拷**:

```python
cfgs[mid] = {**cfgs[mid], "params": {**cfgs[mid].get("params", {}), **ov_params}}
```

这条要有专门的回归测试,否则会以「改了参数 30 秒内看不到 / 或改了一次污染所有后续读」
的形式偶发。

### 2.4 端点改写 DB

`set_launch_params` 改为走 `runtime_override_store.set_override(session, name, "params", {...})`,
与 `resident` / `gpu` / `vram-budget` 同一条路径。保留
`{"applied": false, "hint": "unload + load 生效"}` 的语义(参数只在下次加载时读)。

### 2.5 校验

- **白名单外的键** → 400,列出允许的键
- **`gpu_memory_utilization` / `tensor_parallel_size`** → 400 + 指向 `vram-budget` / 说明 tp 由放置决定
- **`max_model_len`**:不做硬性拒绝,也**不给并发估算**。

  不拒绝的理由:vLLM 启动时自己会检查「KV 池至少装得下一条 max_model_len 序列」,
  装不下直接拒绝启动 —— 失败模式是"起不来",不是"悄悄跑错",安全。

  ~~原设计要返回「预计并发 N.NNx」的估算~~ —— **写计划时撤掉了**:那需要每个模型的
  KV 单价,而我们只实测过 qwen3.8 系列(65.3 KiB/token,2026-09-21)。对没测过的模型
  给估算,等于给一个看着精确、实际编造的数字;而且 vLLM 自己那行
  "GPU KV cache size" 在混合注意力模型上本身就不准(vllm#40691)。
  UI 上改为给**定性**提示(「上下文与并发此消彼长;调大可能因 KV 装不下而起不来」),
  真实数字让用户从 vLLM 启动日志看。

### 2.6 迁移

Alembic 迁移 + micro-migration **双写**(记忆 `alembic-adoption-state` 的既定规矩)。
当前 head:`d7a4b1e6c093`。新列 nullable,无需回填。

---

## 3. 前端

现状只有一个复选框,所以这是**新建编辑器**而非改输入框,工作量要如实计入。

- 位置:引擎页(参数是**模型级**,不是服务级 —— `source_type=model` 的服务只是模型的
  对外包装,服务页本就不该有这些旋钮)
- 控件:`max_model_len`(数字 + 常用档位 32K/128K/256K)、`max_num_seqs`、
  `max_num_batched_tokens`、`enable_prefix_caching`(复选)、`dtype` / `quantization`(下拉)
- 显存单独一块,走 **`vram-budget`**:模式 auto / percent / absolute,
  absolute 时输入**绝对 GiB**并显示「= 该卡 X.XX%」
- 每次保存后显示 `applied: false` 的提示:**需 unload + load 才生效**
- 去掉 `DashboardOverlay` 里那个孤立的复选框,避免两处编辑同一份状态

---

## 4. 测试

| 测试 | 防什么 |
|---|---|
| `launch-params` 对真实模型返回 200 并落库 | 回归本次的 404 |
| 白名单外的键 → 400 | 越权写任意参数 |
| `gpu_memory_utilization` → 400 且提示 vram-budget | 把换卡坑做成按钮 |
| 深合并:只覆盖给定键,其余保留 yaml 值 | 整个 params 被替换 |
| **`copy_before_write` 不写穿缓存** | §2.3 那个隐蔽陷阱 |
| 显式 `null` 删键、整列 NULL 回退 yaml | 三态退化成两态(同 `gpus` 的 `[]` 哨兵教训) |
| Alembic upgrade/downgrade 往返 | 迁移写坏 |

---

## 5. 明确不做

- **`kv_cache_dtype` 不进白名单** —— 本机没有 nvcc,fp8 KV 会在 FlashInfer JIT 处
  起不来(2026-09-21 两次实测)。加进去等于给一个"点了会起不来"的按钮。
  等装了 nvcc 再单独评估。
- 不动 `vram_budget` 现有的端点与语义(它是好的)
- 不碰放置相关的 `gpu` / `gpus`(已有端点,且是硬约束)
