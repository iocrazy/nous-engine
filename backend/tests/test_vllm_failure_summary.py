"""vLLM 起不来时,报错要指到**真根因**,而不是尾部的包装异常。

2026-09-22 huihui 被设成 256K 后加载失败,日志与 UI 上只看得到
`RuntimeError: Engine core initialization failed. See root cause above.` ——
真正的原因(KV 缓存装不下一条 262144 序列)在更前面、由 EngineCore 打印,被「尾部
200/500 字符」截掉了。只能靠启动命令 + 显存算账反推。

样本行按 vLLM 0.28 的真实输出形状构造(前缀 `(EngineCore pid=…)` / `(APIServer pid=…)`)。
"""
from src.services.inference.llm_vllm import summarize_vllm_failure

_KV_ROOT = (
    "(EngineCore pid=3433300) ValueError: To serve at least one request with the models's "
    "max seq len (262144), (16.31 GiB KV cache) is needed, which is larger than the available "
    "KV cache memory (12.70 GiB). Based on the available memory, the estimated maximum model "
    "length is 204032. Try increasing `gpu_memory_utilization` or decreasing `max_model_len` "
    "when initializing the engine.\n"
)

_HUIHUI_TAIL = [
    "(EngineCore pid=3433300) INFO 09-22 18:53:58 [gpu_worker.py:420] Available KV cache memory: 12.70 GiB\n",
    "(EngineCore pid=3433300) ERROR 09-22 18:54:01 [core.py:900] EngineCore failed to start.\n",
    "(EngineCore pid=3433300) Traceback (most recent call last):\n",
    '(EngineCore pid=3433300)   File "/x/vllm/v1/core/kv_cache_utils.py", line 700, in check\n',
    _KV_ROOT,
    "(APIServer pid=3433023) Traceback (most recent call last):\n",
    '(APIServer pid=3433023)   File "/x/vllm/v1/engine/utils.py", line 1000, in wait\n',
    "(APIServer pid=3433023)     raise RuntimeError(\n",
    "(APIServer pid=3433023) RuntimeError: Engine core initialization failed. "
    "See root cause above. Failed core proc(s): {}\n",
]


def test_picks_root_cause_not_the_wrapper():
    s = summarize_vllm_failure(_HUIHUI_TAIL, 1)
    assert "ValueError" in s
    assert "262144" in s and "12.70 GiB" in s
    assert "See root cause above" not in s


def test_kv_shortage_gets_actionable_hint():
    s = summarize_vllm_failure(_HUIHUI_TAIL, 1)
    assert "KV" in s and "上下文" in s        # 中文处置建议:调小上下文或抬显存预算


def test_negative_returncode_is_named_signal():
    """-9 以前只记成 `exited with code -9`,看不出是被 SIGKILL 杀的
    (2026-09-22 崩溃循环就是被这个耽误的)。"""
    s = summarize_vllm_failure(["INFO loading weights\n"], -9)
    assert "SIGKILL" in s


def test_falls_back_to_tail_when_no_exception_line():
    s = summarize_vllm_failure(["some line\n", "last words here\n"], 1)
    assert "last words here" in s


def test_summary_is_bounded():
    s = summarize_vllm_failure(["ValueError: " + "x" * 5000 + "\n"], 1)
    assert len(s) < 1200
