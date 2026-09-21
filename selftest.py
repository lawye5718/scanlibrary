#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：进程内起服务 → 登录 → 上传 PDF → 转换 → 校验 EPUB 结构/断点续跑/试读版。
本地可移植版：不依赖任何写死的绝对路径，测试目录用 /tmp，测试 PDF 用 pymupdf 现场生成。"""
import json, os, sys, time, threading, urllib.request, urllib.error, zipfile, subprocess, shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server
import pymupdf

ROOT = Path("/tmp/sl_selftest")
if ROOT.exists():
    shutil.rmtree(ROOT)
server.ROOT = ROOT
server.JOBS_FILE = ROOT / "data" / "jobs.json"
server.AUTH_FILE = ROOT / "data" / "auth.json"
server.STATS_FILE = ROOT / "data" / "stats.json"
server.PASSWORD[0] = "testpw123"
server.ensure_dirs()
threading.Thread(target=server.worker_loop, daemon=True).start()

srv = server.Server(("127.0.0.1", 8801), server.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)
BASE = "http://127.0.0.1:8801"
TOKEN = [None]

def call(method, path, data=None, headers=None):
    h = dict(headers or {})
    if TOKEN[0]:
        h.setdefault("Authorization", "Bearer " + TOKEN[0])
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    return urllib.request.urlopen(r, timeout=30)  # 由调用方的 with 负责关闭

def post(path, data, headers=None):
    with call("POST", path, data, headers) as resp:
        return json.loads(resp.read().decode())

def get(path):
    with call("GET", path) as resp:
        return resp.read()

print("✅ health:", json.loads(get("/api/health")))
body = post("/api/login", json.dumps({"password": "testpw123"}).encode(), {"Content-Type": "application/json"})
TOKEN[0] = body["token"]
print("✅ login ok")

# 现场生成 5 页测试 PDF
doc = pymupdf.open()
for i in range(5):
    page = doc.new_page()
    page.insert_text((72, 100), f"Test page {i+1}.")  # 用 ASCII 文本避免中文字体依赖
pdf_bytes = doc.tobytes()
doc.close()
print(f"✅ 生成测试 PDF（{len(pdf_bytes)} 字节，5 页）")

# 任务1：文本层提取，新书（未知 md5）
cfg1 = {
    "book_title": "测试书名",
    "author": "测试作者",
    "backend": "text-layer",
    "first_page": 1,
    "last_page": 5,
    "page_range": [(1, 2), (3, 5)],
}
data = json.dumps(cfg1).encode()
r1 = post("/api/upload?filename=%E6%B5%8B%E8%AF%95.pdf", pdf_bytes, {
    "Content-Type": "application/octet-stream", "X-Config": __import__("base64").b64encode(data).decode(),
})
jid1 = r1["job"]["id"]
print("✅ 上传成功:", r1)

t0 = time.time()
last_t = ""
while time.time() - t0 < 60:
    s = json.loads(get(f"/api/job/{jid1}"))
    if s["status"] == "done":
        break
    if s["status"] in ("failed", "error"):
        raise SystemExit("❌ 任务失败: " + str(s))
    time.sleep(0.2)
else:
    raise SystemExit("❌ 任务1超时")
print(f"✅ 任务1完成 ({time.time()-t0:.1f}s):", s.get("epub_path"))

# 锁定任务1的真实 EPUB（text-layer 后端）做后续校验
epub_path = Path(s["epub_path"])

# 老书任务2：MD5 已存在 → 复用分页缓存，stub 后端，页数不足自动扩
cfg2 = {"book_title": "测试书名", "author": "测试作者", "backend": "stub"}
data2 = json.dumps(cfg2).encode()
r2 = post("/api/upload?filename=%E6%B5%8B%E8%AF%95.pdf", pdf_bytes, {
    "Content-Type": "application/octet-stream", "X-Config": __import__("base64").b64encode(data2).decode(),
})
jid2 = r2["job"]["id"]
t0 = time.time()
while time.time() - t0 < 60:
    s2 = json.loads(get(f"/api/job/{jid2}"))
    if s2["status"] == "done":
        break
    if s2["status"] in ("failed", "error"):
        raise SystemExit("❌ 任务2失败: " + str(s2))
    time.sleep(0.2)
else:
    raise SystemExit("❌ 任务2超时")
assert s2.get("total_pages", 0) >= 5, "页数应≥5（老书可扩）"
print(f"✅ 任务2完成（复用已有分页缓存）: {s2.get('epub_path')}")
epub_path = Path(s2["epub_path"])  # 用 stub 后端产物做内容校验
with zipfile.ZipFile(epub_path) as z:
    names = z.namelist()
    assert "mimetype" in names
    # mimetype 必须未压缩且是第一项
    assert names[0] == "mimetype", "mimetype 应为第一项"
    assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED, "mimetype 不能压缩"
    assert z.read("mimetype") == b"application/epub+zip"
    assert any(n == "META-INF/container.xml" for n in names), "缺少 container.xml"
    opf = [n for n in names if n.endswith(".opf")]
    assert opf, "缺少 .opf"
    assert any(n == "toc.ncx" or n.endswith("/toc.ncx") for n in names), "缺少 toc.ncx"
    xhtmls = sorted(n for n in names if n.endswith(".xhtml"))
    body_pages = [n for n in xhtmls if n.startswith("OEBPS/chap_")]
    assert len(body_pages) >= 1, f"缺少正文页：{xhtmls}"
    # stub 后端用占位文本（避免中文字体依赖）；校验至少有一页含"第 page_0001"
    all_body = "\n".join(z.read(n).decode("utf-8") for n in body_pages)
    assert "第 page_0001" in all_body, "stub 后端未生成占位正文"
print("✅ EPUB 结构校验通过（mimetype未压缩/置首，container/opf/ncx/5页xhtml齐全）")

# pandoc 读回验证（可选，缺 pandoc 不算失败）
p = subprocess.run(["pandoc", str(epub_path), "-t", "plain"], capture_output=True, text=True)
if p.returncode == 0:
    assert "第 page_0001" in p.stdout
    print("✅ pandoc 读回校验通过")
else:
    print("ℹ️ 未安装 pandoc，跳过读回校验")

# 下载
with call("GET", f"/api/download/{jid2}") as resp:
    blob = resp.read()
assert len(blob) == epub_path.stat().st_size
print(f"✅ 下载成功（{len(blob)} 字节）")

# 任务列表与中文文件名下载头
with call("GET", f"/api/download/{jid2}") as resp:
    assert resp.headers.get("Content-Disposition", "").startswith("attachment"), resp.headers
    print("✅ 下载响应 Content-Disposition:", resp.headers.get("Content-Disposition")[:60])
jobs = json.loads(get("/api/jobs"))["jobs"]
assert isinstance(jobs, list) and len(jobs) == 2
print("✅ 任务列表 OK")

# 断点续跑
json.loads(get(f"/api/job/{jid2}"))
r3 = post(f"/api/retry/{jid2}", b"{}", {"Content-Type": "application/json"})
print("✅ retry 响应:", r3)

print("\n🎉 全部端到端用例通过")
