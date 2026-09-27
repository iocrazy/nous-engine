"""Lane C: GPU Runner IPC 协议编解码测试（纯内存，无子进程、无 GPU）。"""
import pytest

from src.runner import protocol as P


def _round_trip(msg, fmt: str):
    """encode -> decode 往返，断言还原成同类型同字段。"""
    raw = P.encode(msg, fmt=fmt)
    assert isinstance(raw, bytes)
    back = P.decode(raw, fmt=fmt)
    assert type(back) is type(msg)
    assert back == msg
    return back


ALL_MESSAGES = [
    P.Ready(runner_id="runner-i", group_id="image", gpus=[2]),
    P.LoadModel(model_key="flux2-dev", config={"vram_gb": 24}),
    P.UnloadModel(model_key="flux2-dev"),
    P.RunNode(
        task_id=101, node_id="sampler", node_type="image",
        model_key="flux2-dev", inputs={"prompt": "a cat", "steps": 30},
        is_deterministic=False,
    ),
    P.Abort(task_id=101, node_id="sampler"),
    P.Ping(),
    P.NodeResult(
        task_id=101, node_id="sampler", status="completed",
        outputs={"path": "outputs/101/0.png", "meta": {"w": 1024}},
        error=None, duration_ms=4200,
    ),
    P.NodeProgress(task_id=101, node_id="sampler", progress=0.4, detail="step 12/30"),
    # PR-1a:NodeProgress 带 L3 stage 字段 —— 验 stage/step/total_steps/step_latency_ms/eta_ms
    # 编解码 round-trip 不丢失。
    P.NodeProgress(
        task_id=101, node_id="sampler", progress=0.4,
        detail="dit_denoise 12/30",
        stage="dit_denoise", step=12, total_steps=30,
        step_latency_ms=850, eta_ms=15300,
    ),
    P.ModelEvent(event="loaded", model_key="flux2-dev", error=None),
    P.Pong(runner_id="runner-i", loaded_models=["flux2-dev"]),
    # spec ram-pinned-linkage PR-1b:Pong 带 host RAM 占用快照,验编解码 round-trip 不丢。
    P.Pong(runner_id="runner-i", loaded_models=["flux2-dev"], stash_ram_mb=22800),
]


@pytest.mark.parametrize("msg", ALL_MESSAGES, ids=lambda m: type(m).__name__)
def test_round_trip_msgpack(msg):
    _round_trip(msg, fmt="msgpack")


@pytest.mark.parametrize("msg", ALL_MESSAGES, ids=lambda m: type(m).__name__)
def test_round_trip_json(msg):
    """dev 模式 NOUS_IPC_FORMAT=json fallback —— 同样往返成立。"""
    _round_trip(msg, fmt="json")


def test_msgpack_is_more_compact_than_json():
    """sanity: msgpack 编码不大于 JSON（不是严格更小，但典型负载应更紧凑）。"""
    msg = ALL_MESSAGES[3]  # RunNode，带 inputs dict
    assert len(P.encode(msg, fmt="msgpack")) <= len(P.encode(msg, fmt="json"))


def test_decode_unknown_kind_raises():
    """收到未知 kind 的消息 —— decode 抛 ProtocolError，不静默吞。"""
    import msgpack
    bogus = msgpack.packb({"kind": "not_a_real_message", "x": 1})
    with pytest.raises(P.ProtocolError):
        P.decode(bogus, fmt="msgpack")


def test_pong_backward_compat_without_stash_field():
    """老 runner 发的 Pong 不带 stash_ram_mb（spec PR-1b 新增）→
    decode 用默认 0,不抛(向后兼容)。"""
    import msgpack
    old = msgpack.packb({"kind": "pong", "runner_id": "r", "loaded_models": [],
                         "loaded_components": []})
    pong = P.decode(old, fmt="msgpack")
    assert pong.stash_ram_mb == 0


def test_default_format_from_env(monkeypatch):
    """encode/decode 不传 fmt 时读 NOUS_IPC_FORMAT，默认 msgpack。"""
    monkeypatch.delenv("NOUS_IPC_FORMAT", raising=False)
    assert P.default_format() == "msgpack"
    monkeypatch.setenv("NOUS_IPC_FORMAT", "json")
    assert P.default_format() == "json"
    msg = P.Ping()
    # 不传 fmt → 用 env 的 json
    raw = P.encode(msg)
    assert P.decode(raw) == msg
