#!/usr/bin/env python3
"""nous-status —— 独立公开状态监控(2026-06-17)。

刻意**拨离主系统**:独立进程 / 独立端口 / 独立 systemd unit,**纯 stdlib**(不 import
torch/fastapi,不依赖 nous-backend 的 venv),自己跑 nvidia-smi + 读 /proc 拿硬件,
用 urllib 探 nous-backend /health。**nous-backend 进程死了,本服务照样能显示「后端 API:
中断」** —— 这才是监控该有的样子(对齐 status.claude.ai 是独立平台,不是系统模块)。

公开无登录(用户拍板):只露硬件概况 + 组件在线/离线,不露模型路径/密钥/内部错误细节。

跑:python3 status_service.py(默认 127.0.0.1:8001;公网隧道已退役,只在本机/内网看)。
环境变量:NOUS_STATUS_PORT / NOUS_STATUS_HOST / NOUS_STATUS_BACKEND。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("NOUS_STATUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOUS_STATUS_PORT", "8001"))
BACKEND = os.environ.get("NOUS_STATUS_BACKEND", "http://127.0.0.1:8000")

OPERATIONAL, DEGRADED, DOWN = "operational", "degraded", "down"
_RANK = {OPERATIONAL: 0, DEGRADED: 1, DOWN: 2}


def _worst(statuses) -> str:
    s = [x for x in statuses if x]
    return max(s, key=lambda x: _RANK.get(x, 0)) if s else OPERATIONAL


# ---------- 硬件(纯 stdlib / /proc / nvidia-smi)----------

def read_cpu_pct() -> float | None:
    """两次读 /proc/stat 求 busy 占比(阻塞 ~0.1s)。"""
    def snap():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle
    try:
        t0, i0 = snap()
        time.sleep(0.1)
        t1, i1 = snap()
        dt, di = t1 - t0, i1 - i0
        return round(100.0 * (dt - di) / dt, 1) if dt > 0 else None
    except Exception:
        return None


def read_mem() -> dict | None:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])  # kB
        total = info["MemTotal"] / 1024 / 1024  # GiB
        avail = info.get("MemAvailable", info["MemFree"]) / 1024 / 1024
        used = total - avail
        return {"used_gb": round(used, 1), "total_gb": round(total, 1),
                "pct": round(100.0 * used / total, 1) if total else None}
    except Exception:
        return None


def read_load() -> list | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except Exception:
        return None


def read_uptime() -> int | None:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.readline().split()[0]))
    except Exception:
        return None


def read_gpus() -> list | None:
    """nvidia-smi 查每卡 name/mem/util/temp。无 GPU/无 nvidia-smi → None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return None
        gpus = []
        for line in out.stdout.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) < 6:
                continue
            used, total = float(f[2]), float(f[3])
            gpus.append({
                "index": int(f[0]), "name": f[1],
                "mem_used_gb": round(used / 1024, 1), "mem_total_gb": round(total / 1024, 1),
                "mem_pct": round(100.0 * used / total, 1) if total else None,
                "util_pct": None if f[4] in ("[N/A]", "") else float(f[4]),
                "temp_c": None if f[5] in ("[N/A]", "") else float(f[5]),
            })
        return gpus
    except Exception:
        return None


# ---------- 探 nous-backend ----------

def backend_health() -> dict | None:
    """GET <backend>/health(unauth)。连不上 → None(= 后端挂)。"""
    try:
        req = urllib.request.Request(f"{BACKEND}/health", headers={"User-Agent": "nous-status"})
        with urllib.request.urlopen(req, timeout=4) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _runner_status(health: dict, group: str) -> str:
    """runner 在线/离线(基础设施视角,不看是否加载模型 —— 监控的是「能不能服务」)。"""
    for r in health.get("runners", []) or []:
        if r.get("group_id") == group:
            return OPERATIONAL if r.get("running") else DOWN
    return DOWN


def snapshot() -> dict:
    health = backend_health()
    services = []
    if health is None:
        services.append({"key": "backend", "name": "后端 API", "status": DOWN, "detail": "无法连接(后端进程可能已挂)"})
        # 后端探不到,下游全标未知
        for k, n in [("database", "数据库"), ("image", "图像 Runner"), ("tts", "语音 Runner")]:
            services.append({"key": k, "name": n, "status": DOWN, "detail": "后端不可达"})
        overall = DOWN
        models_loaded = None
    else:
        services.append({"key": "backend", "name": "后端 API", "status": OPERATIONAL, "detail": ""})
        db = health.get("database")
        services.append({"key": "database", "name": "数据库",
                         "status": OPERATIONAL if db == "ok" else DOWN, "detail": "" if db == "ok" else f"db={db}"})
        for grp, nm in [("image", "图像 Runner"), ("tts", "语音 Runner")]:
            st = _runner_status(health, grp)
            services.append({"key": grp, "name": nm, "status": st,
                             "detail": "在线" if st == OPERATIONAL else "离线"})
        models_loaded = health.get("models_loaded")
        lf = health.get("load_failures") or {}
        overall = _worst([s["status"] for s in services] + ([DEGRADED] if lf else []))

    return {
        "overall": overall,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "models_loaded": models_loaded,
        "services": services,
        "hardware": {
            "gpus": read_gpus(),
            "cpu_pct": read_cpu_pct(),
            "mem": read_mem(),
            "load": read_load(),
            "uptime_s": read_uptime(),
        },
    }


# ---------- HTTP ----------

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>nous-center 系统状态</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--bd:#262b36;--tx:#e6e9ef;--mut:#8b93a3;
--op:#22c55e;--dg:#f59e0b;--dn:#ef4444}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0}.sub{color:var(--mut);font-size:13px;margin-top:4px}
.banner{display:flex;align-items:center;gap:10px;padding:14px 16px;border-radius:10px;
background:var(--card);margin:16px 0;border-left:4px solid var(--op)}
.banner .t{font-size:16px;font-weight:600}.banner .u{margin-left:auto;color:var(--mut);font-size:12px}
.sec{background:var(--card);border:1px solid var(--bd);border-radius:10px;margin-bottom:16px;overflow:hidden}
.sec h2{font-size:13px;color:var(--mut);font-weight:600;margin:0;padding:12px 16px;border-bottom:1px solid var(--bd);
text-transform:uppercase;letter-spacing:.04em}
.row{display:flex;align-items:center;gap:12px;padding:12px 16px;border-top:1px solid var(--bd)}
.row:first-of-type{border-top:none}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.name{font-weight:500}.detail{color:var(--mut);font-size:12px;margin-left:auto}
.bar{height:8px;border-radius:4px;background:#0c0e12;overflow:hidden;width:120px;flex:none}
.bar>i{display:block;height:100%}
.gname{font-size:12px;color:var(--mut)}
.meta{display:flex;gap:18px;flex-wrap:wrap;padding:12px 16px;color:var(--mut);font-size:12px}
.err{color:var(--dn);padding:16px}
</style></head><body><div class="wrap">
<h1>nous-center 系统状态</h1><div class="sub">独立监控 · 自动刷新 15s</div>
<div id="app"><div class="sub" style="margin-top:20px">加载中…</div></div>
</div>
<script>
const C={operational:'var(--op)',degraded:'var(--dg)',down:'var(--dn)'};
const L={operational:'运行正常',degraded:'部分降级',down:'中断'};
const BL={operational:'所有系统运行正常',degraded:'部分系统降级',down:'存在服务中断'};
function bar(pct,color){return `<div class="bar"><i style="width:${pct||0}%;background:${color}"></i></div>`}
function memColor(p){return p>=90?'var(--dn)':p>=75?'var(--dg)':'var(--op)'}
function fmtUp(s){if(s==null)return '—';const d=Math.floor(s/86400),h=Math.floor(s%86400/3600);return d>0?`${d}天${h}时`:`${h}时${Math.floor(s%3600/60)}分`}
async function load(){
 try{
  const d=await (await fetch('/api.json',{cache:'no-store'})).json();
  const ov=d.overall||'operational';
  let h=`<div class="banner" style="border-left-color:${C[ov]}"><span class="dot" style="background:${C[ov]}"></span>`
   +`<span class="t">${BL[ov]}</span><span class="u">更新于 ${new Date(d.generated_at).toLocaleTimeString()}</span></div>`;
  // services
  h+='<div class="sec"><h2>服务</h2>';
  for(const s of d.services){h+=`<div class="row"><span class="dot" style="background:${C[s.status]}"></span>`
   +`<span class="name">${s.name}</span><span style="color:${C[s.status]};font-size:12px">${L[s.status]}</span>`
   +`<span class="detail">${s.detail||''}</span></div>`}
  if(d.models_loaded!=null)h+=`<div class="meta">已加载模型:${d.models_loaded}</div>`;
  h+='</div>';
  // hardware
  const hw=d.hardware||{};h+='<div class="sec"><h2>硬件</h2>';
  for(const g of (hw.gpus||[])){
   h+=`<div class="row"><span class="dot" style="background:${memColor(g.mem_pct)}"></span>`
    +`<div><div class="name">GPU ${g.index} · ${g.name}</div>`
    +`<div class="gname">显存 ${g.mem_used_gb}/${g.mem_total_gb}G · 利用 ${g.util_pct??'—'}% · ${g.temp_c??'—'}°C</div></div>`
    +`<span class="detail">${g.mem_pct??'—'}%</span>${bar(g.mem_pct,memColor(g.mem_pct))}</div>`}
  if(!(hw.gpus||[]).length)h+='<div class="row"><span class="detail">无 GPU 数据</span></div>';
  const m=hw.mem;
  h+=`<div class="meta">`
   +`<span>CPU ${hw.cpu_pct??'—'}%</span>`
   +(m?`<span>内存 ${m.used_gb}/${m.total_gb}G (${m.pct}%)</span>`:'')
   +(hw.load?`<span>负载 ${hw.load.join(' / ')}</span>`:'')
   +`<span>运行 ${fmtUp(hw.uptime_s)}</span></div></div>`;
  document.getElementById('app').innerHTML=h;
 }catch(e){document.getElementById('app').innerHTML=`<div class="err">状态加载失败:${e}</div>`}
}
load();setInterval(load,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/status"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path in ("/api.json", "/status.json"):
            self._send(200, json.dumps(snapshot(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):  # 静默,不刷 journal
        pass


def main() -> None:
    socket.setdefaulttimeout(10)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"nous-status listening on {HOST}:{PORT} (backend={BACKEND})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
