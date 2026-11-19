# MOSS Image、Qwen Image 2.1 与 OLDX

来源为 ComfyUI 画布 `nous-MossImage_QwenImage2.1_含OLDX.json`。在原服务 `nous-qwen21-all-in-one` 上更新模板和映射，原有服务 ID、API 路径和授权继续使用。Playground 默认打开 MOSS 文生图，API 缺省和显式 `text` 均走 Qwen。

| 模式 | 行为 |
| --- | --- |
| `moss_text` | MOSS 文生图，可调画幅、像素、采样和风格 LoRA |
| `text` | Qwen Turbo 文生图 |
| `reference` | 根据参考图反推，可选择用 MOSS 或 Qwen 重新生图 |
| `edit` | Qwen Turbo 编辑，最多上传六张参考图 |
| `oldx` | 人物源图提供身份，姿势与场景参考提供构图、姿势和光线 |
| `character_card` | 上传人物源图，使用 ComfyUI 中固定的资产卡作为版式参考 |
| `outpaint` | 按四边扩展原图，可选提示词 |
| `remove` | 白色遮罩标记要移除的区域 |
| `upscale` | SeedVR2 直接放大原图 |

最终输出仍是 `image_url`。处理模式可选用 SeedVR2 放大结果。额外参考图留空时由桥剪掉各自的预处理支路。

## 原位更新

依照仓库流程部署新版 Playground 后，在现有 ComfyUI sidecar 保留人物资产卡。通过 `CHARACTER_ASSET` 指定相对于 sidecar `input/` 的文件名。模板附件中的默认文件仅用于构建和验证。

```bash
export CHARACTER_ASSET=use-case-design.webp
./upgrade.sh --preview
./upgrade.sh --apply
```

脚本从环境读取 `ADMIN_TOKEN`，确认本机 sidecar 可访问人物资产卡，并在应用前备份当前模板和映射。工作流、画布布局和字段映射在一次请求里更新。

## 重新转换

准备原画布 JSON 及其所有分支启用后的 API 格式导出。可复制原画布文件，把节点的 `mode` 都设为 `0` 后使用 `../upscale/convert.cjs` 转换该副本。生成运行工作流、字段契约和画布坐标时，把原始画布作为第二个参数。

```bash
python3 build_template.py all-enabled.api.json original-canvas.json
```
