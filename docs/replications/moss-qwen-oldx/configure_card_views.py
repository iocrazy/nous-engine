"""Preview or apply the three local reference views to the active image service."""

import argparse
import json
import os
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

SERVICE = "nous-qwen21-all-in-one"
INPUT_KEY = "character_card_view"
VIEW_FILES = ("view1 (1).png", "view2.png", "cef44f77-662f-4e37-9782-673303e4f976.png")
VIEW_NODES = ("654", "681", "683")
SWITCH = "685"


def build_update(template, files=VIEW_FILES):
    workflow = deepcopy(template["workflow_json"])
    target = workflow["655"]["inputs"]
    if target.get("on_true") not in (["654", 0], [SWITCH, 0]):
        raise ValueError("人物资产卡节点 #655 的参考图连线不符合预期")

    changes = {
        node_id: {"class_type": "LoadImage", "inputs": {"image": filename},
                  "_meta": {"title": f"人物资产卡参考图 {index + 1}"}}
        for index, (node_id, filename) in enumerate(zip(VIEW_NODES, files, strict=True))
    }
    changes[SWITCH] = {"class_type": "easy imageIndexSwitch", "_meta": {"title": "资产视图参考切换"},
                       "inputs": {"index": 1, **{f"image{i}": [node_id, 0] for i, node_id in enumerate(VIEW_NODES)}}}
    for node_id, replacement in changes.items():
        if node_id in workflow and workflow[node_id]["class_type"] != replacement["class_type"]:
            raise ValueError(f"节点 #{node_id} 已用于其他功能")
        if node_id not in ("654", SWITCH) and node_id in workflow and target["on_true"] != [SWITCH, 0]:
            raise ValueError(f"节点 #{node_id} 已存在，请检查连线")
    workflow.update(changes)
    target["on_true"] = [SWITCH, 0]
    fields = [field for field in template["exposed_params"] if field["key"] != INPUT_KEY]
    fields.append({
        "key": INPUT_KEY, "label": "资产卡参考图", "type": "integer", "required": False,
        "default": 1, "comfy_node_id": SWITCH, "comfy_input": "index",
        "visible_when": {"input_mode": "character_card"},
        "options": [
            {"value": index, "label": f"参考图 {index + 1}",
             "image": f"/api/v1/comfy/card-reference/{index}/thumbnail"}
            for index in range(3)
        ],
    })
    return {"workflow": workflow, "mapping": {"exposed_params": fields}, "revision": template["revision"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--comfy", default="http://127.0.0.1:8888")
    parser.add_argument("--pics", type=Path, default=Path("/media/heygo/program/models/nous/pics"))
    parser.add_argument("--files", nargs=3, default=VIEW_FILES, metavar="INPUT_FILE")
    args = parser.parse_args()
    token = os.environ["ADMIN_TOKEN"]
    opener = build_opener(ProxyHandler({}))

    def engine(path, method="GET", payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(args.base.rstrip("/") + path, data=body, method=method,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    path = "/api/v1/comfy-templates"
    item = next(row for row in engine(path) if row["service_name"] == SERVICE)
    path += "/" + item["id"]
    template = engine(path)
    update = build_update(template, args.files)

    for index, filename in enumerate(args.files, 1):
        query = urlencode({"type": "input", "filename": filename})
        with opener.open(f"{args.comfy.rstrip('/')}/view?{query}", timeout=10) as response:
            comfy_bytes = response.read()
        if sha256(comfy_bytes).digest() != sha256((args.pics / f"view{index}.png").read_bytes()).digest():
            raise ValueError(f"ComfyUI 的 {filename} 与 view{index}.png 内容不一致")

    preview = engine(path + "/preview", "POST", update)
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False))
        return

    with NamedTemporaryFile(mode="w", prefix="nous-card-views-", suffix=".json", delete=False) as backup:
        json.dump(template, backup, ensure_ascii=False)
        print(f"已备份模板 {backup.name}")
    print(json.dumps(engine(path, "PUT", update), ensure_ascii=False))


if __name__ == "__main__":
    main()
