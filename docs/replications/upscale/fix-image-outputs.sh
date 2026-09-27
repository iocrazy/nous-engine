#!/usr/bin/env bash
# 把「修复前建的图片桥模板」的输出契约从 video_url(永远 null)改成 image_url。
# 由 deploy.sh 调用,也可单独跑。幂等:已是 image_url 的跳过;形状不认识的拒绝改动。
#
#   BASE=http://127.0.0.1:8000 ADMIN_TOKEN=... ./docs/replications/upscale/fix-image-outputs.sh
#
# 只改 exposed_outputs(PATCH /api/v1/services/{id} 只带这一个字段,其余字段不动)。
# 服务 id 运行时按名字查,不写死。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
: "${ADMIN_TOKEN:?需要 ADMIN_TOKEN(backend/.env)}"
AUTH=(-H "Authorization: Bearer ${ADMIN_TOKEN}" -H "Content-Type: application/json")

IMAGE_SERVICES=(nous-krea2 nous-qwen21-text-to-image nous-qwen21-image-edit)
TARGET='[{"key":"image_url","node_id":"out","input_name":"image_url","type":"image","label":"图片"}]'
# 目标 / 旧默认 的判定只看 (key, node_id, input_name),不看 label/required 等附带字段。
SIG='[.[] | {key, node_id: (.node_id|tostring), input_name}]'

services="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/services")"
for name in "${IMAGE_SERVICES[@]}"; do
  sid="$(jq -r --arg n "$name" '.[] | select(.name == $n) | .id' <<<"$services")"
  if [[ -z "$sid" ]]; then
    echo "ERROR $name:服务不存在(改过名?)—— 核对服务名后重跑" >&2
    exit 1
  fi
  detail="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/services/$sid")"
  if [[ "$(jq -r .source_type <<<"$detail")" != "comfy_template" ]]; then
    echo "ERROR $name(id=$sid):不是 comfy_template 服务,拒绝改动" >&2
    exit 1
  fi
  current="$(jq -c ".exposed_outputs | $SIG" <<<"$detail")"
  case "$current" in
    '[{"key":"image_url","node_id":"out","input_name":"image_url"}]')
      echo "skip $name(id=$sid):已是 image_url"; continue ;;
    '[{"key":"video_url","node_id":"out","input_name":"video_url"}]')
      ;;
    *)
      echo "ERROR $name(id=$sid):exposed_outputs 不是旧默认形状,拒绝覆盖:$current" >&2
      exit 1 ;;
  esac
  curl -sS --fail-with-body "${AUTH[@]}" -X PATCH "$BASE/api/v1/services/$sid" \
    -d "{\"exposed_outputs\": $TARGET}" >/dev/null
  after="$(curl -sS --fail-with-body "$BASE/v1/services/$name/schema" \
    | jq -c '.output_schema.properties | keys')"
  [[ "$after" == '["image_url"]' ]] || { echo "ERROR $name:PATCH 后 schema 仍是 $after" >&2; exit 1; }
  echo "fixed $name(id=$sid):video_url → image_url"
done
