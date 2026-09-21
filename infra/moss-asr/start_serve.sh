#!/bin/sh
# nous-moss-asr 启动脚本。
#
# ⚠️ **这不再是生产路径**(Arc 2,spec 2026-07-20-moss-asr-sglang-serving §7):生产由
# 后端的 SGLangOmniAdapter 起 sgl-omni 子进程统一纳管,配置在
# `backend/configs/models.d/moss_transcribe_diarize.yaml`。配套的
# `infra/systemd/nous-engine-moss-asr.service` 已退役且未安装。
# 本脚本保留作**手动跑 / 排障**用,并且是 adapter `_build_env()` 复刻的那份环境的原型 ——
# 改这里的 env 要同步看 asr_sglang.py,反之亦然。
#
# 脚本文件启动(非 inline `sh -c`),故进程 cmdline = `python .venv/bin/sgl-omni serve ...`;
# systemd KillMode=control-group 收整个 cgroup,不靠 pgrep -f 匹配(SPIKE.md:sglang 有
# worker 子进程,cmdline 不含 config 名,pgrep 清不干净会留孤儿吃显存)。
cd "$(dirname "$0")"

# torch 默认 FASTEST_FIRST 枚举 → Pro 6000 会被排到 cuda:0。PCI_BUS_ID 让枚举跟随总线号,
# 配合下面 UUID 钉死后进程内只见这一张卡。
export CUDA_DEVICE_ORDER=PCI_BUS_ID
# UUID 钉到 index1 的 Pro 6000(96GB)。裸 cuda:0 随枚举顺序/槽位变,不可靠;UUID 绑定不变。
# 进程内只可见它,故 config 里 device=cuda:0 即指它。
# 单机 infra,硬编码 UUID(同本仓库其余绝对路径/UUID 硬编码);换卡需同步改这里。
#
# 2026-09-05 #709 从 3090(GPU-2fd7c91c…)搬到 Pro 6000:两张 3090 整块让给 Qwen3.8 做
# tp=2。老注释写的「绝不落 Pro 6000」(GSP 固件负载崩卡)已失效 —— 该问题 2026-08-11 经
# 用户确认解决,驱动 595.91.07 起零 Xid。**本文件必须与
# backend/configs/models.d/moss_transcribe_diarize.yaml 的 params.gpu_uuid 保持一致**:
# 生产实际走的是那份 yaml(见文件头),这里只是手动跑/排障用的等价环境。
export CUDA_VISIBLE_DEVICES=GPU-d24ed424-5712-55e9-9b95-77d997ac80dc

# sgl-omni 首次冷启会现场 nvcc 编译 sgl-kernel(本机卡的 arch 无预编译,如 fused_rope);本机无
# /usr/local/cuda,指向 venv 内自带的 cu13 工具链(setup.sh 已装好并建 lib64/libcudart 软链)。
# 三样缺一不可(PR-0 主循环 serve 调通实证):CUDA_HOME 定工具链根;PATH 里 venv/bin 找 ninja、
# cu13/bin 找 nvcc;LD_LIBRARY_PATH 让链接/运行期找到 libcudart 等。warm cache 时不重编(秒级起),
# 但首次部署冷启会真编,缺这些直接编不过。
VENV="$(pwd)/.venv"
export CUDA_HOME="$VENV/lib/python3.12/site-packages/nvidia/cu13"
export PATH="$VENV/bin:$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib:${LD_LIBRARY_PATH:-}"

# 模型是本地全量检出 + trust_remote_code,不需联网;强制 HF 离线,免得经 mihomo 代理去
# 探 huggingface.co 拖慢/挂起冷启。
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

# 本机 mihomo 代理会拦 127.0.0.1 回环(backend→微服务同机),排除掉。
export NO_PROXY=127.0.0.1,localhost

# 端口默认 8003(拓扑约定;backend 经 NOUS_MOSS_ASR_URL 指过来)。
PORT="${NOUS_MOSS_ASR_PORT:-8003}"

# 日志走 stdout,systemd journal 接管(journalctl -u nous-moss-asr -f);不再 >> 文件。
exec "$VENV/bin/python" "$VENV/bin/sgl-omni" serve \
  --config moss_config.yaml \
  --host 127.0.0.1 --port "$PORT"
