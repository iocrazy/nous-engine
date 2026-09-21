"""SGLangOmniAdapter 单测(spec 2026-07-20 §7 Arc 2)—— 全 mock subprocess/health,
CI 可跑(不起真 sgl-omni / 不碰 GPU)。

覆盖:① 是 InferenceAdapter(AUDIO 模态)② load 起子进程 + 等健康,env 钉 UUID(忽略
device index)③ 子进程秒退 → RuntimeError ④ unload 经 safe_killpg 杀**整进程组** ⑤ pgid<=1
广播防护(真 safe_killpg 拒 pid<=1,不误杀)⑥ health = GET /health 200。
"""
from __future__ import annotations

import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.inference.asr_sglang import SGLangOmniAdapter
from src.services.inference.base import InferenceAdapter, MediaModality

_UUID = "GPU-2fd7c91c-af39-7b02-66b9-988331ce3bd7"


def _make(tmp_path, **kw):
    return SGLangOmniAdapter(
        paths={"main": str(tmp_path)},
        gpu_uuid=_UUID,
        asr_port=18099,
        config_path=str(tmp_path / "moss_config.yaml"),
        venv_dir=str(tmp_path / ".venv"),
        **kw,
    )


def test_adapter_is_inference_adapter(tmp_path):
    a = _make(tmp_path)
    assert isinstance(a, InferenceAdapter)
    assert a.modality == MediaModality.AUDIO
    # Pro 5000 上 mem_fraction_static 0.10 的实测稳态 8996 MiB,取整 9000(见 moss_config.yaml
    # 与 models.d/moss_transcribe_diarize.yaml 的 vram_mb)。2026-09-20 两卡迁移前这里是
    # 15000(Pro 6000 / 0.15 口径)—— 比例按整卡总量算,换卡必须连这个申报值一起改。
    assert a.estimated_vram_mb == 9000


def test_build_env_pins_uuid_not_index(tmp_path):
    """env 完整复刻 start_serve.sh 且 CUDA_VISIBLE_DEVICES 钉 UUID(不是 device index)。"""
    a = _make(tmp_path)
    env = a._build_env()
    assert env["CUDA_VISIBLE_DEVICES"] == _UUID  # UUID,不是 "0"/"cuda:0"
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["NO_PROXY"] == "127.0.0.1,localhost"
    assert env["PATH"].startswith(f"{a._venv_dir / 'bin'}:")
    assert str(a._venv_dir / "lib" / "python3.12" / "site-packages" / "nvidia" / "cu13") == env["CUDA_HOME"]


def test_build_argv_uses_config(tmp_path):
    a = _make(tmp_path)
    argv = a._build_argv(18099)
    assert argv[0].endswith("/bin/python")
    assert argv[1].endswith("/bin/sgl-omni")
    assert argv[2] == "serve"
    assert "--config" in argv and str(a._config_path) in argv
    assert "--port" in argv and "18099" in argv


@pytest.mark.asyncio
async def test_load_spawns_and_waits_health(tmp_path):
    """load:health 起初失败(等待)→ 转 200 → is_loaded;spawn 用 start_new_session。"""
    a = _make(tmp_path)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # 进程活着
    fake_proc.pid = 4242
    fake_proc.stdout = iter(())  # drain 线程立即结束

    health_seq = [False, True]  # 第一次未起,第二次健康

    async def _health():
        return health_seq.pop(0)

    with patch("subprocess.Popen", return_value=fake_proc) as popen, \
         patch.object(a, "_health_check", side_effect=_health), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        # 已存在实例的 reconnect 探测也走 _health_check;上面 seq 第一次 False 会被它吃掉,
        # 所以补一个 False 在最前:reconnect 探测 False → 进入 spawn。
        health_seq.insert(0, False)
        await a.load("cuda:0")

    assert a.is_loaded
    assert a._managed is True
    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == _UUID
    assert a.base_url == "http://127.0.0.1:18099"
    assert a._port == 18099


@pytest.mark.asyncio
async def test_load_raises_when_process_exits(tmp_path):
    """子进程秒退(poll 返回非 None)→ RuntimeError,不留 loaded。"""
    a = _make(tmp_path)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = 1  # 已退出
    fake_proc.returncode = 1
    fake_proc.pid = 4243
    fake_proc.stdout = iter(("boom\n",))

    with patch("subprocess.Popen", return_value=fake_proc), \
         patch.object(a, "_health_check", new_callable=AsyncMock, return_value=False), \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(a, "_kill_process"):
        with pytest.raises(RuntimeError, match="failed to start"):
            await a.load("cuda:0")
    assert not a.is_loaded


@pytest.mark.asyncio
async def test_unload_kills_process_group(tmp_path):
    """unload 经 safe_killpg(广播防护)给**整进程组**发 SIGTERM,清状态。"""
    import src.services.safe_signal as ss

    a = _make(tmp_path)
    a._model = True
    a._managed = True
    proc = MagicMock()
    proc.pid = 525252  # 真 int,让 pid<=1 guard 求值
    proc.wait = MagicMock()
    a._process = proc

    sent: list = []
    with patch.object(ss, "safe_killpg",
                      side_effect=lambda pid, sig, **kw: sent.append((pid, sig)) or True):
        a.unload()

    assert sent and sent[0] == (525252, signal.SIGTERM)  # 我们自己 spawn 的 pid
    assert not a.is_loaded
    assert a._process is None


def test_kill_refuses_pgid_le_1(tmp_path):
    """pgid<=1 广播防护:proc.pid=1 → 真 safe_killpg 拒发(pid<=1 guard),不 wait、不误杀。"""
    a = _make(tmp_path)
    a._model = True
    a._managed = True
    proc = MagicMock()
    proc.pid = 1  # init:safe_killpg 必拒(broadcast guard)
    a._process = proc

    a.unload()  # 用真 safe_killpg
    proc.wait.assert_not_called()  # 拒发 → 不会去 wait
    assert a._process is None  # finally 仍清引用


def test_unload_external_doesnt_kill(tmp_path):
    """接管的外部实例(_managed=False)只断连,不杀进程。"""
    a = _make(tmp_path)
    a._model = True
    a._managed = False
    a._process = None
    a.unload()
    assert not a.is_loaded


@pytest.mark.asyncio
async def test_health_check_hits_health_path(tmp_path):
    a = _make(tmp_path)
    a._base_url = "http://127.0.0.1:18099"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch.object(a._client, "get", new_callable=AsyncMock, return_value=mock_resp) as g:
        ok = await a._health_check()
    assert ok
    assert g.call_args.args[0] == "http://127.0.0.1:18099/health"


@pytest.mark.asyncio
async def test_infer_not_implemented(tmp_path):
    """ASR 经端点直连 base_url,不走 adapter.infer。"""
    a = _make(tmp_path)
    with pytest.raises(NotImplementedError):
        await a.infer(MagicMock())
