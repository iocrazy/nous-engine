#!/usr/bin/env bash
# 注册三个放大桥服务(模板 + 服务 + mapping)。只走控制面 API,不碰 DB、不重启任何东西。
#
#   BASE=http://127.0.0.1:8000 ADMIN_TOKEN=... ./docs/replications/upscale/deploy.sh
#
# 可重跑,且会把仓库里的产物同步到已存在的服务(2026-09-27):
# - 模板不存在 → 新建 + 核对 output_kind + 写 mapping(失败回滚);
# - 模板已存在 → workflow 与仓库不同就 `PUT /{id}` 重传,mapping 与仓库不同就
#   `PUT /{id}/mapping`;都相同则什么也不写。改了 mapping 默认值 / 参数范围后重跑本脚本即生效,
#   不必再手动 PUT。输出类型(image/video)只在新建时定,已存在的模板不改 —— 要换类型先
#   `DELETE /api/v1/comfy-templates/{id}`(级联删服务)再重跑。
# 最后一步修旧图片模板的输出契约。
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

# 已存在的模板:按仓库产物同步 workflow 与 mapping(按 JSON 语义比较,只写有差异的部分)。
sync_existing() {
  local name="$1" artifact="$2" tid="$3" cur changed=0
  cur="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates/$tid")"
  if ! jq -e --slurpfile wf "$DIR/$artifact.api.json" '.workflow_json == $wf[0]' <<<"$cur" >/dev/null; then
    jq -n --slurpfile wf "$DIR/$artifact.api.json" '{workflow: $wf[0]}' \
      | curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid" -d @- >/dev/null
    echo "  $name:workflow 已同步"
    changed=1
  fi
  # workflow 重传后 mapping 一律重写(旧 mapping 可能指向已不存在的节点);否则只在有差异时写。
  # 后端回读时把没写的字段补成 null/false(random、multiple、options_source…),所以比较前两边
  # 都去掉 null/false/空值;真改动(如 required true→false)在一边留键一边没有,照样判为不同。
  local norm='map(with_entries(select(.value != null and .value != false and .value != [] and .value != {})))'
  # discovery(服务发现元数据)一并比较;视频放大的 mapping 没有它,两边都是 null。
  if (( changed )) || ! jq -e --slurpfile m "$DIR/$artifact.mapping.json" \
      "(.exposed_params | $norm) == (\$m[0].exposed_params | $norm) and .discovery == \$m[0].discovery" \
      <<<"$cur" >/dev/null; then
    curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid/mapping" \
      --data-binary "@$DIR/$artifact.mapping.json" >/dev/null
    echo "  $name:mapping 已同步($(jq '.exposed_params | length' "$DIR/$artifact.mapping.json") 个参数)"
    changed=1
  fi
  (( changed )) || echo "  $name:已是最新,未改动"
  discovery_landed "$artifact" "$tid" || { echo "ERROR $name:$DISCOVERY_ERR" >&2; exit 1; }
  echo "synced $name → template_id=$tid"
}

# 回读核对 discovery 真的落库了 —— 老后端的 MappingBody 不认这个键,会静默丢掉。
# 返回非零由调用方决定怎么收场(新建路径要回滚,同步路径直接报错退出)。
discovery_landed() {
  local artifact="$1" tid="$2" got
  got="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates/$tid")"
  jq -e --slurpfile m "$DIR/$artifact.mapping.json" '.discovery == $m[0].discovery' <<<"$got" >/dev/null
}
DISCOVERY_ERR="回读的 discovery 与仓库不一致(后端是否已上线带 discovery 的代码?)"

existing="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates")"
for row in "${SERVICES[@]}"; do
  IFS='|' read -r name artifact kind <<<"$row"
  tid="$(jq -r --arg n "$name" 'first(.[] | select(.name == $n) | .id) // empty' <<<"$existing")"
  if [[ -n "$tid" ]]; then
    sync_existing "$name" "$artifact" "$tid"
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
  discovery_landed "$artifact" "$tid" || rollback "$DISCOVERY_ERR"
done

# 必做:修复前建的图片模板(krea2 / qwen21-*)输出契约 video_url → image_url(幂等)。
BASE="$BASE" ADMIN_TOKEN="$ADMIN_TOKEN" "$DIR/fix-image-outputs.sh"
