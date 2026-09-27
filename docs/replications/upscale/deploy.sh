#!/usr/bin/env bash
# 注册三个放大桥服务(模板 + 服务 + mapping)。只走控制面 API,不碰 DB、不重启任何东西。
#
#   BASE=http://127.0.0.1:8000 ADMIN_TOKEN=... ./docs/replications/upscale/deploy.sh
#
# 可重跑:已存在的同名模板直接跳过(不覆盖 workflow/mapping;要重建先
# `DELETE /api/v1/comfy-templates/{id}`,级联删服务)。最后一步修旧图片模板的输出契约。
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

existing="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates")"
for row in "${SERVICES[@]}"; do
  IFS='|' read -r name artifact kind <<<"$row"
  if jq -e --arg n "$name" 'any(.[]; .name == $n)' <<<"$existing" >/dev/null; then
    echo "skip $name:模板已存在"
    continue
  fi
  payload="$(jq -n --arg name "$name" --arg kind "$kind" \
    --slurpfile wf "$DIR/$artifact.api.json" \
    '{name: $name, output_kind: $kind, workflow: $wf[0]}')"
  created="$(curl -sS --fail-with-body "${AUTH[@]}" -X POST "$BASE/api/v1/comfy-templates" -d "$payload")"
  tid="$(jq -r '.id' <<<"$created")"
  got_kind="$(jq -r '.output_kind' <<<"$created")"
  echo "created $name → template_id=$tid output_kind=$got_kind"
  rollback() {
    echo "ERROR $name:$1 —— 回滚刚建的模板 $tid" >&2
    if ! curl -sS --fail-with-body "${AUTH[@]}" -X DELETE "$BASE/api/v1/comfy-templates/$tid" >/dev/null; then
      echo "回滚失败,手动清理:curl -H 'Authorization: Bearer \$ADMIN_TOKEN' -X DELETE $BASE/api/v1/comfy-templates/$tid" >&2
    fi
    exit 1
  }
  # 旧后端(没有输出类型推断)会忽略 output_kind、一律写 video_url —— 先上线代码再注册。
  [[ "$got_kind" == "$kind" ]] || rollback "output_kind=$got_kind,期望 $kind(后端是否已上线新代码?)"
  curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid/mapping" \
    --data-binary "@$DIR/$artifact.mapping.json" >/dev/null || rollback "PUT mapping 失败"
  echo "  mapping ok($(jq '.exposed_params | length' "$DIR/$artifact.mapping.json") 个参数)"
done

# 必做:修复前建的图片模板(krea2 / qwen21-*)输出契约 video_url → image_url(幂等)。
BASE="$BASE" ADMIN_TOKEN="$ADMIN_TOKEN" "$DIR/fix-image-outputs.sh"
