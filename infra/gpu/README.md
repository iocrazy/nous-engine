# GPU 崩卡排查存档(RTX PRO 6000 Blackwell GSP 固件 bug)

> **这份文档是存档,不是操作指南。** 记录 2026 年 7 月那次 PRO 6000 崩卡的排查过程与当时
> 的处置。**问题当前不复现**,下面「处置」各条的状态标注已按 2026-09-08 实测改写 ——
> 别照着它做例行运维,只在**崩卡真复发**时回来对症状、走恢复流程。
>
> 尤其:**`setup-gpu-mitigations.sh` 不是任何东西的安装前置**。CLAUDE.md 里那条已经摘掉(#730)。

## 现状(2026-09-08 复核)

| 项 | 实测 |
|---|---|
| 驱动 / GSP 固件 | **595.91.07**(下文「定性」点名的问题版本是 595.71.05) |
| `setup-gpu-mitigations.sh` | **从未上机** |
| ↳ persistence mode | 三卡全 `Disabled`(脚本要求 `1`) |
| ↳ `DynamicPowerManagement` | `3`,GC6 仍开(脚本要求 `0`) |
| ↳ `nous-gpu-guard` unit | `could not be found` |
| Xid / FULLCHIP_RESET | 2026-07-01 至今内核日志**零条** |

期间 PRO 6000 一直带着常驻模型跑、ComfyUI 也钉在这张卡(`CUDA_VISIBLE_DEVICES=1`,
2026-09-07 连跑 12h54m)。**即缓解从未生效,而卡也没崩。**

驱动从 595.71.05 升到 595.91.07 是最可能的原因,但**没核实修复具体落在哪个版本** ——
这是实测证据,不是因果结论。用户 2026-08-11 亦明确确认该问题不再出现。

## 症状(复发时用这个对)
跑模型时 **RTX PRO 6000 Blackwell** GSP 固件崩溃 → 卡进 `NV_ERR_GPU_IN_FULLCHIP_RESET`
僵死(`status 0x0f = NV_ERR_GPU_NOT_FULL_POWER`),把同机显示用的 3090 一起拖黑 → 开机黑屏 /
整机硬死,SSH 可能还活但 `nvidia-smi` 掉句柄(`Unable to determine the device handle ... Unknown Error`)。

## 定性(当时)
595.71.05 / 580 open 驱动上 Blackwell 的**已知 GSP 固件 bug**,**负载触发**(开机即满载预加载、
或长时间满载推理最易触发)。换驱动 / 装 Windows 均无效(GSP 固件与 OS 无关)。佐证:
open-gpu-kernel-modules issues **#1111**(同款 PRO6000+2×3090 WRX90)/ #1151 / #1134。
完整排查见 Notion「WRX90 工作站 Ubuntu 启动黑屏排查:GPU0 FULLCHIP_RESET 僵死」。

## 处置(分层,状态为 2026-09-08 实测)

**1. 恢复(崩了之后,唯一可靠)—— 这条永远有效**:**冷断电** —— 关机 → 断 AC(拔电源线 /
关 PSU 硬开关)→ 长按机箱电源键 30s 放电 → 等 2-3 分钟 → 通电开机。热重启不一定够
(GSP 要重新上电复位)。

**2. 开机空载(当时做过,现已回退)**:
- 「所有模型取消常驻」**已不成立** —— 现在开机预载常驻模型
  (`Preloading N resident model(s)`),`resident: true` 是 CLAUDE.md 里的明文机制。
- 当时写的「靠按需懒加载、首调自动 load」也**已不成立**:`ensure_vllm_base_url` 已不存在,
  数据面现在对模型放置**只读**,未加载的模型直接 503 `model_not_ready`
  (spec 2026-09-05 engine-app-boundary)。**复发时想恢复 boot 空载,得改 `resident` 配置,
  不能指望懒加载兜底。**
- `nous-engine-aligner` 已彻底删除(2026-07-28,核实仍属实):它开机会在 3090 上常驻 2GB
  对齐模型。ASR 词级时间戳现由 MOSS-Transcribe-Diarize 内建接管。

**3. 系统级缓解(本目录,从未上机)**:
```bash
sudo ./infra/gpu/setup-gpu-mitigations.sh   # 关 GC6 动态省电 + 开 persistence;重启生效
```
脚本改两处,均无副作用、可逆。**当前不建议跑** —— 两个月零 Xid 说明没这个必要,
留着是给复发时用的。

**4. 双 3090 张量并行 —— 当时记为「未做」,其实已经做了**:
`backend/configs/models.d/qwen3_8_27b_abliterated_awq.yaml` 的 `gpus: [0, 2]` 就是这条
(两张 3090 跑 tp=2,MOSS ASR 搬去 PRO 6000)。#1111 作者验证双 3090 TP 稳跑 20+ 小时。
放置规则见 CLAUDE.md 的「GPU 放置 / 张量并行」一节。

## BIOS(当时做过,现已不在生效)
当时:Primary Graphics → `Onboard`(ASPEED VGA)+ grub `nvidia_drm.modeset=0` → 控制台走
板载 BMC VGA,显卡僵死不再连累黑屏、BMC KVM 全程可见。

**2026-09-08 实测已回退**:`/proc/cmdline` 里没有 `nvidia_drm.modeset=0`,且显示器就接在
GPU 0 的 3090 上(`display_active: Enabled`,与 CLAUDE.md「`cuda:0` = RTX 3090 驱动显示器」
一致)。**所以崩卡若复发,黑屏会再次连累控制台** —— 这是复发时第一个要重新做的。
