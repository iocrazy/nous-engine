#!/usr/bin/env bash
# 注册三个放大桥服务(模板 + 服务 + mapping)。只走控制面 API,不碰 DB、不重启任何东西。
#
#   BASE=http://127.0.0.1:8000 ADMIN_TOKEN=... ./docs/replications/upscale/deploy.sh
#
# 已存在同名模板/服务时 POST 回 409,脚本就此停下 —— 要重建先
# `DELETE /api/v1/comfy-templates/{id}`(级联删服务),别在已发布服务上重复建。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
: "${ADMIN_TOKEN:?需要 ADMIN_TOKEN(backend/.env)}"
DIR="$(cd "$(dirname "$0")" && pwd)"
AUTH=(-H "Authorization: Bearer ${ADMIN_TOKEN}" -H "Content-Type: application/json")

# service 名 | 产物文件前缀 | 输出类型
SERVICES=(
  "nous-seedvr2-image-upscale|seedvr2-image-upscale|image"
  "nous-vosr2-image-upscale|vosr2-image-upscale|image"
  "nous-vosr2-video-upscale|vosr2-video-upscale|video"
)

for row in "${SERVICES[@]}"; do
  IFS='|' read -r name artifact kind <<<"$row"
  payload="$(jq -n --arg name "$name" --arg kind "$kind" \
    --slurpfile wf "$DIR/$artifact.api.json" \
    '{name: $name, output_kind: $kind, workflow: $wf[0]}')"
  created="$(curl -sS --fail-with-body "${AUTH[@]}" -X POST "$BASE/api/v1/comfy-templates" -d "$payload")"
  tid="$(jq -r '.id' <<<"$created")"
  echo "created $name → template_id=$tid output_kind=$(jq -r '.output_kind' <<<"$created")"
  curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid/mapping" \
    --data-binary "@$DIR/$artifact.mapping.json" >/dev/null
  echo "  mapping ok($(jq '.exposed_params | length' "$DIR/$artifact.mapping.json") 个参数)"
done
