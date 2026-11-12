#!/usr/bin/env bash
# 把仓库里的 Qwen Image 2.1 文生图一采产物(8 槽 LoRA 版)同步到生产服务 nous-qwen21-text-to-image。
# 只走控制面 API,不碰 DB、不重启任何东西。
#
#   BASE=http://127.0.0.1:8000 ADMIN_TOKEN=... ./docs/replications/qwen21-text-to-image/deploy.sh
#
# 前置:后端已上线带 `discovery` 的代码(否则 PUT mapping 会把 discovery 静默丢掉)。脚本最后
# 回读核对,老后端直接报错退出。
#
# 按**服务名**查模板 id(生产上的模板名是 `qwen21-text-to-image`,与服务名不同);服务不存在
# 就用服务名新建(output_kind=image)。已存在时:workflow 与仓库不同 → 先 `PUT /{id}`;
# mapping 不同 → 再 `PUT /{id}/mapping`;都相同则什么也不写。可重跑。
#
# 产物来源:生产模板 workflow_json(官方前端 app.graphToPrompt 转出的文生图分支,11 个节点)
# + 两个串联的 `Lora Loader Stack (rgthree)`(700 → 701,各 4 槽 = 8 槽),KSampler.model 与
# TextEncodeQwenImage21.clip 改接 701。LoRA 选项只列 ComfyUI 上与 Qwen Image 2.1 兼容的文件。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
: "${ADMIN_TOKEN:?需要 ADMIN_TOKEN(backend/.env)}"
DIR="$(cd "$(dirname "$0")" && pwd)"
AUTH=(-H "Authorization: Bearer ${ADMIN_TOKEN}" -H "Content-Type: application/json")
SERVICE="nous-qwen21-text-to-image"
ARTIFACT="qwen21-text-to-image"
WF="$DIR/$ARTIFACT.api.json"
MAP="$DIR/$ARTIFACT.mapping.json"

list="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates")"
tid="$(jq -r --arg s "$SERVICE" 'first(.[] | select(.service_name == $s) | .id) // empty' <<<"$list")"

if [[ -z "$tid" ]]; then
  payload="$(jq -n --arg name "$SERVICE" --slurpfile wf "$WF" \
    '{name: $name, output_kind: "image", workflow: $wf[0]}')"
  tid="$(curl -sS --fail-with-body "${AUTH[@]}" -X POST "$BASE/api/v1/comfy-templates" -d "$payload" \
    | jq -r '.id')"
  echo "  created $SERVICE → template_id=$tid"
  curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid/mapping" \
    --data-binary "@$MAP" >/dev/null
  echo "  mapping 已写入($(jq '.exposed_params | length' "$MAP") 个参数)"
else
  cur="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates/$tid")"
  changed=0
  # **先 workflow 后 mapping**:新 workflow 的两个 Stack 默认 8 槽全 "None",旧 mapping 不引用
  # 700/701 —— 中间态与现在完全一样。反过来的话,mapping 写完而 workflow 写失败时,目录会声明
  # lora_slots=8,但桥找不到 700/701、LoRA 全被静默丢掉。
  if ! jq -e --slurpfile wf "$WF" '.workflow_json == $wf[0]' <<<"$cur" >/dev/null; then
    jq -n --slurpfile wf "$WF" '{workflow: $wf[0]}' \
      | curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid" -d @- >/dev/null
    echo "  workflow 已同步($(jq 'length' "$WF") 个节点)"
    changed=1
  fi
  # 回读把没写的字段补成 null/false,比较前两边都去掉 null/false/空值(同 upscale/deploy.sh)。
  norm='map(with_entries(select(.value != null and .value != false and .value != [] and .value != {})))'
  if ! jq -e --slurpfile m "$MAP" \
      "(.exposed_params | $norm) == (\$m[0].exposed_params | $norm) and .discovery == \$m[0].discovery" \
      <<<"$cur" >/dev/null; then
    curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid/mapping" \
      --data-binary "@$MAP" >/dev/null
    echo "  mapping 已同步($(jq '.exposed_params | length' "$MAP") 个参数)"
    changed=1
  fi
  (( changed )) || echo "  已是最新,未改动"
fi

# 回读核对:discovery 真的落库了(不是被老后端静默丢掉)。
after="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates/$tid")"
jq -e --slurpfile m "$MAP" '.discovery == $m[0].discovery' <<<"$after" >/dev/null \
  || { echo "ERROR 回读的 discovery 与仓库不一致(后端是否已上线新代码?)" >&2; exit 1; }
echo "synced $SERVICE → template_id=$tid"
