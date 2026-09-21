# nous-moss-asr — MOSS-Transcribe-Diarize ASR 微服务(SGLang Omni)

转写 + 说话人分离 + 段级时间戳的独立微服务,取代 Qwen3-ASR + ForcedAligner 两件套
(MOSS 内建时间戳/分离,不再需要对齐器)。设计
`docs/superpowers/specs/2026-07-20-moss-asr-sglang-serving-design.md`,PR-0 spike 结论
`infra/moss-asr/SPIKE.md`(必读)。

> **⚠️ 现状(2026-07-21,Arc 2):独立 systemd unit 已退役,改由 backend ModelManager 纳管。**
> MOSS 现由 `configs/models.d/moss_transcribe_diarize.yaml`(`type: asr`、`resident: true`、
> `SGLangOmniAdapter`)统一管理:backend 起 `sgl-omni serve` 子进程、随 backend 常驻、UI 可见启停。
> 转写端点经 ModelManager 取引擎 base_url(`NOUS_MOSS_ASR_URL` env 仍是应急/测试 override,
> 优先级 env > ModelManager)。**本目录的 `.venv` / `setup.sh` / `moss_config.yaml` / `start_serve.sh`
> 仍是运行载体**(adapter 起的就是 `.venv/bin/sgl-omni serve --config moss_config.yaml`);
> 退役的只是 `nous-moss-asr.service` 这个 systemd 管理层。切换/回滚 runbook 见 spec §5b-Arc2。
> 下文「systemd unit」相关段落是 Arc 1 历史,保留作回滚参考。

## 为什么独立进程 / 独立 venv

MOSS 走 `sglang-omni`(SGLang Omni serving runtime),自带 `torch 2.11+cu130` /
`transformers 5.6` / `sglang 0.5.12` 一整套,和 backend 的 `vllm 0.22` / 图像 diffusers
钉是两条互不相干的升级轨。装一起必然打架。所以做成:独立进程、独立
venv(`infra/moss-asr/.venv`,~9.8GB)、独立端口(8003)、独立 systemd unit,和 backend
完全隔离。backend 仅在转写时 HTTP 调它;它挂了转写主路返回 503(MOSS 是唯一 ASR 主路,
**不静默降级**)。

模型自定义架构 `MossTranscribeDiarizeForConditionalGeneration`(trust_remote_code),
prod vLLM registry 不认 → 不能走既有 vLLM 直引擎路径,故走 SGLang Omni 独立服务。

## 接口

```
GET  /health                                   → 200(存活探针)
POST /v1/audio/transcriptions                  → OpenAI 兼容转写
```

`POST /v1/audio/transcriptions`(multipart form):

| 字段 | 说明 |
|---|---|
| `file` | **16k mono WAV**(见下方硬约束) |
| `model` | 模型路径或名,取 `moss-transcribe-diarize` |
| `response_format` | `verbose_json`(要分段)/ `json`(只回 text) |
| `max_new_tokens` | 长音频要调大(90min 播客量级要几万;spike 用 16384~65536) |

`response_format=verbose_json` 响应结构(PR-2 后端按此解析):

```json
{"task":"transcribe","duration":3.48,
 "text":"[0.26][S01]希望你以后能够做得比我还好哟。[3.24]",
 "segments":[{"id":0,"start":0.26,"end":3.24,"text":"[S01]希望你以后能够做得比我还好哟。"}],
 "usage":{"type":"duration","seconds":4}}
```

**说话人无独立字段**:`[Sxx]` 是 `segments[].text` 的前缀(也在顶层 `text` 里),后端要
正则抠出说话人。`json` 格式则只回顶层 `text`。7.7min 双人播客 → 53 段、S01/S02 全程归属
正确(spike 实测)。

## 硬约束:只吃 16k mono WAV

微服务用 torchcodec 解码,本机缺 libav 共享库,**mp3 / 非 16k WAV 直传会 500**。
生产无影响:backend 的 `_ffmpeg_to_wav16k` 在调本服务前已把上传音频归一化成 16k/mono WAV,
微服务只见规范 WAV。手工 curl 测试也必须先转好 16k WAV(仓库 `assets/voices/*.wav` 是现成的)。

## 搭建(一次性 / 重建 prod 检出时)

```bash
# 1. 下模型(若没下)
modelscope download --model MOSS/MOSS-Transcribe-Diarize \
  --local_dir $MODELS_ROOT/nous/speech/MOSS-Transcribe-Diarize
# 2. 建独立 venv + 装 sglang-omni + 补 CUDA 工具链(~9.8GB;幂等,可重跑)
./infra/moss-asr/setup.sh
# 3. 装 + 起 systemd unit
sudo cp infra/systemd/nous-moss-asr.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nous-moss-asr
# 4. 自检
curl -s http://127.0.0.1:8003/health
curl -s -X POST http://127.0.0.1:8003/v1/audio/transcriptions \
  -F model=moss-transcribe-diarize \
  -F file=@assets/voices/default_zh_female.wav \
  -F response_format=verbose_json | head
```

`.venv/`、`sglang-omni/`(clone,setup.sh 钉死 commit)、`logs/` 都 gitignore;每检出一份重跑
`setup.sh`。GPU 钉 index0 的 **Pro 5000**(UUID `GPU-f4334111-…`,硬编码在**四处**,
改一处要四处同步:
`backend/configs/models.d/moss_transcribe_diarize.yaml` 的 `params.gpu_uuid` ← **生产真相源**、
`start_serve.sh`、退役 unit `infra/systemd/nous-engine-moss-asr.service`、
`backend/src/services/inference/asr_sglang.py` 的 `_gpu_uuid` **缺省值**(旧文档漏列了这处,
2026-09-20 迁移时四处里有三处没跟着改,ASR 差点被起到 ComfyUI 的卡上))。
历史:2026-09-05 #709 从 3090 搬到 Pro 6000(两张 3090 让给 Qwen3.8 做 tp=2);
老文档的「绝不 Pro 6000」(GSP 固件崩卡)已失效,该问题 2026-08-11 确认解决。
2026-09-20 两张 3090 拔除、换上 Pro 5000,**Pro 6000 改为 ComfyUI 独占,ASR 落 Pro 5000**。
端口 8003(env `NOUS_MOSS_ASR_PORT` 可改)。日志走 journald:`journalctl -u nous-engine-backend -f`
(Arc 2 后 MOSS 是 backend 的子进程,不再有自己的 unit)。

`start_spike_serve.sh` / `moss_spike_config.yaml` / `SPIKE.md` 是 PR-0 spike 遗留,保留在 git
作参考;生产走 `start_serve.sh` + `moss_config.yaml`。
