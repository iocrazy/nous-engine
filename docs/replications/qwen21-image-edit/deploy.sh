#!/usr/bin/env bash
# 把仓库里的 qwen21 图像编辑产物(8 张参考图版)同步到生产服务 nous-qwen21-image-edit。
# 只走控制面 API,不碰 DB、不重启任何东西。
#
#   BASE=http://127.0.0.1:8000 ADMIN_TOKEN=... ./docs/replications/qwen21-image-edit/deploy.sh
#
# 前置:后端已上线带 `omit_when_empty` 的代码(否则 PUT mapping 会把这个字段静默丢掉,
# 参考图 2…8 未传时 LoadImage 会喂模板占位图)。脚本先 GET 一次回读核对,老后端直接报错退出。
#
# 按**服务名**查模板 id(模板名 `qwen21-image-edit` 与服务名不同);服务不存在就报错 ——
# 这个脚本只同步已存在的服务,不新建(新建走 POST /api/v1/comfy-templates + 本目录产物)。
# workflow 与仓库不同 → `PUT /{id}` 重传;之后(或 mapping 有差异时)`PUT /{id}/mapping`;
# 都相同则什么也不写。可重跑。
#
# 产物来源:`node ../upscale/convert.cjs variants.json <out>`(ComfyUI 官方前端
# app.graphToPrompt,拦截一切非 GET),之后做了两处与现网对齐的归一:476 的 unet_name 路径
# 分隔符 `\` → `/`,487 的 filename_prefix 固定成现网值(%date% 会被前端展开成转换时刻)。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
: "${ADMIN_TOKEN:?需要 ADMIN_TOKEN(backend/.env)}"
DIR="$(cd "$(dirname "$0")" && pwd)"
AUTH=(-H "Authorization: Bearer ${ADMIN_TOKEN}" -H "Content-Type: application/json")
SERVICE="nous-qwen21-image-edit"
ARTIFACT="qwen21-image-edit"
WF="$DIR/$ARTIFACT.api.json"
MAP="$DIR/$ARTIFACT.mapping.json"

list="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates")"
tid="$(jq -r --arg s "$SERVICE" 'first(.[] | select(.service_name == $s) | .id) // empty' <<<"$list")"
[[ -n "$tid" ]] || { echo "ERROR 找不到服务 $SERVICE 对应的模板" >&2; exit 1; }

cur="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates/$tid")"
# 老后端的 GET 回读里没有 omit_when_empty 键 —— 先上线代码再同步。
jq -e '.exposed_params | length == 0 or (.[0] | has("omit_when_empty"))' <<<"$cur" >/dev/null \
  || { echo "ERROR 后端还不认 omit_when_empty(先上线新代码)" >&2; exit 1; }

changed=0
if ! jq -e --slurpfile wf "$WF" '.workflow_json == $wf[0]' <<<"$cur" >/dev/null; then
  jq -n --slurpfile wf "$WF" '{workflow: $wf[0]}' \
    | curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid" -d @- >/dev/null
  echo "  workflow 已同步($(jq 'length' "$WF") 个节点)"
  changed=1
fi
# 回读把没写的字段补成 null/false,比较前两边都去掉 null/false/空值(同 upscale/deploy.sh)。
norm='map(with_entries(select(.value != null and .value != false and .value != [] and .value != {})))'
if (( changed )) || ! jq -e --slurpfile m "$MAP" \
    "(.exposed_params | $norm) == (\$m[0].exposed_params | $norm)" <<<"$cur" >/dev/null; then
  curl -sS --fail-with-body "${AUTH[@]}" -X PUT "$BASE/api/v1/comfy-templates/$tid/mapping" \
    --data-binary "@$MAP" >/dev/null
  echo "  mapping 已同步($(jq '.exposed_params | length' "$MAP") 个参数)"
  changed=1
fi
(( changed )) || echo "  已是最新,未改动"

# 回读核对:omit_when_empty 真的落库了(不是被老后端静默丢掉)。
after="$(curl -sS --fail-with-body "${AUTH[@]}" "$BASE/api/v1/comfy-templates/$tid")"
n="$(jq '[.exposed_params[] | select(.omit_when_empty == true)] | length' <<<"$after")"
[[ "$n" == "7" ]] || { echo "ERROR 回读 omit_when_empty=true 的参数有 $n 个,期望 7" >&2; exit 1; }
echo "synced $SERVICE → template_id=$tid"
