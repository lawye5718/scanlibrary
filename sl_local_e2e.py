#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地端到端自测：认证 + 模式 + 估算 + text-layer 转换全流程。"""
import json, sys, time, threading, urllib.request, urllib.error
from pathlib import Path
import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import server

ROOT = Path("/tmp/sl_localtest")
import shutil
if ROOT.exists():
    shutil.rmtree(ROOT)
server.ROOT = ROOT
server.JOBS_FILE = ROOT / "data" / "jobs.json"
server.AUTH_FILE = ROOT / "data" / "auth.json"
server.STATS_FILE = ROOT / "data" / "stats.json"
server.PASSWORD[0] = "testpw123"
server.ensure_dirs()
server.Server.ollama_url = "http://localhost:11434"
threading.Thread(target=server.worker_loop, daemon=True).start()

srv = server.Server(("127.0.0.1", 8802), server.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = "http://127.0.0.1:8802"
TOKEN = None
PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra else ""))

def req(path, data=None, headers=None, method=None):
    h = dict(headers or {})
    if TOKEN:
        h.setdefault("Authorization", "Bearer " + TOKEN)
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

# 构造测试 PDF（带文字层，12 页）
doc = pymupdf.open()
for i in range(12):
    p = doc.new_page()
    p.insert_text((72, 100), f"第{i+1}页 测试内容段落。")
pdf_bytes = doc.tobytes()
doc.close()

print("== 1. 认证机制 ==")
code, _ = req("/api/health")
check("health 免认证可达", code == 200)
code, body = req("/api/jobs")
check("未登录访问 jobs 返回 401", code == 401)
code, _ = req("/api/login", json.dumps({"password": "wrong"}).encode(), {"Content-Type": "application/json"})
check("错误密码返回 401", code == 401)
code, body = req("/api/login", json.dumps({"password": "testpw123"}).encode(), {"Content-Type": "application/json"})
check("正确密码登录成功", code == 200 and json.loads(body).get("token"))
TOKEN = json.loads(body)["token"]
code, _ = req("/api/jobs")
check("登录后 jobs 可访问", code == 200)

print("== 2. 测试版模式（前10页）==")
from urllib.parse import quote
cfg = {"mode": "test", "backend": "text-layer", "lang": "zh-CN"}
code, body = req("/api/upload", pdf_bytes, {
    "X-Filename": quote("测试书.pdf"), "X-Title": quote("测试书"),
    "X-Config": quote(json.dumps(cfg))})
job = json.loads(body)["job"]
check("上传成功", code == 200)
check("模式识别为 test", job.get("mode") == "test", str(job.get("mode")))
check("标题带试读版后缀", "试读版" in job.get("title", ""))
jid = job["id"]
for _ in range(40):
    time.sleep(0.5)
    code, body = req(f"/api/job/{jid}")
    j = json.loads(body)
    if j["status"] in ("done", "error"):
        break
check("测试版转换完成", j["status"] == "done", j.get("message", ""))
epub_test = ROOT / "books" / "测试书" / "测试书-试读版.epub"
check("试读版 EPUB 单独命名", epub_test.exists())
code, body = req(f"/api/download/{jid}")
check("下载 EPUB 需要认证且成功", code == 200 and body[:2] == b"PK")

print("== 3. 全书模式 + 耗时估算 ==")
# 预置历史统计：10页 100 串行秒 → 10 秒/页；并发2 → 12页 ≈ 60 秒 ≈ 1 分钟
server.record_pages_sec("glm-ocr@200dpi", 10, 100.0)
cfg = {"mode": "full", "backend": "glm-ocr", "dpi": 200, "concurrency": 2, "lang": "zh-CN"}
code, body = req("/api/upload", pdf_bytes, {
    "X-Filename": quote("测试书.pdf"), "X-Title": quote("测试书"),
    "X-Config": quote(json.dumps(cfg))})
job2 = json.loads(body)["job"]
check("全书上传成功", code == 200)
check("页码估算已生成", bool(job2.get("estimate")), str(job2.get("estimate")))
check("估算基于历史(含'历史')", "历史" in (job2.get("estimate") or ""))
est_sec = server.estimate_seconds(12, {"backend": "glm-ocr", "dpi": 200, "concurrency": 2})
check("估算数值正确(≈60秒)", est_sec and 55 <= est_sec <= 65, str(est_sec))

print("== 4. 模型状态接口 ==")
code, body = req("/api/model-state")
d = json.loads(body)
check("model-state 可访问", code == 200 and "loaded" in d)

print("== 5. 统计持久化 ==")
st = json.loads((ROOT / "data" / "stats.json").read_text())
check("stats.json 已落盘", "glm-ocr@200dpi" in st and st["glm-ocr@200dpi"]["pages"] == 10)

srv.shutdown()
print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("全部通过 ✅")
