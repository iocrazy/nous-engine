#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
base=${BASE:-http://127.0.0.1:8000}
comfy=${COMFY_URL:-http://127.0.0.1:8888}
name=nous-qwen21-all-in-one
: "${ADMIN_TOKEN:?需要 Nous Engine 管理令牌}"
: "${CHARACTER_ASSET:?需要 ComfyUI input 目录中的人物资产卡文件名}"

case ${1:---preview} in
  --preview|--apply) action=${1:---preview} ;;
  *) printf 'usage: %s [--preview|--apply]\n' "$0" >&2; exit 2 ;;
esac

engine() {
  curl --noproxy '*' --fail-with-body -sS --max-time 30 \
    --header @<(printf 'Authorization: Bearer %s\n' "$ADMIN_TOKEN") \
    --header 'Content-Type: application/json' "$@"
}

curl --noproxy '*' --fail -sS --max-time 10 --output /dev/null \
  --get "$comfy/view" --data-urlencode "filename=${CHARACTER_ASSET##*/}" \
  --data-urlencode "subfolder=$(if [[ "$CHARACTER_ASSET" == */* ]]; then printf '%s' "${CHARACTER_ASSET%/*}"; fi)" \
  --data-urlencode 'type=input'

id=$(engine "$base/api/v1/comfy-templates" | jq -er --arg name "$name" \
  '[.[] | select(.service_name == $name)] | select(length == 1) | .[0].id')
url="$base/api/v1/comfy-templates/$id"
current=$(engine "$url")
revision=$(jq -er '.revision' <<< "$current")
payload=$(jq -n --slurpfile workflow workflow.api.json --slurpfile canvas canvas.json \
  --slurpfile mapping mapping.json --arg asset "$CHARACTER_ASSET" --arg revision "$revision" \
  '{workflow:($workflow[0] | .["654"].inputs.image = $asset), canvas_layout:$canvas[0],
    mapping:$mapping[0], revision:$revision}')

if [[ "$action" == --preview ]]; then
  engine --request POST "$url/preview" --data-binary @- <<< "$payload"
  exit 0
fi

umask 077
backup=$(mktemp /tmp/nous-qwen21-before-oldx.XXXXXX.json)
printf '%s\n' "$current" > "$backup"
printf 'Original template and mapping saved to %s\n' "$backup"
engine --request PUT "$url" --data-binary @- <<< "$payload"
curl --noproxy '*' --fail -sS --max-time 10 "$base/v1/services/$name/schema" \
  | jq -e '.input_schema.properties.input_mode
           | select(.default == "text" and (.enum | index("moss_text") and index("oldx") and index("character_card")))
           | {default, modes:.enum}'
