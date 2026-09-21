#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 Ollama 调用参数：起 mock ollama → 直接调 run_job（glm-ocr 后端）→ 校验请求路径/JSON 参数/图片编码。
本地可移植版：测试目录用 /tmp，测试 PDF 用 pymupdf 现场生成。"""
import json, sys, shutil, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server
import pymupdf

ROOT = Path("/tmp/sl_selftest_ollama")
if ROOT.exists():
    shutil.rmtree(ROOT)
server.ROOT = ROOT
server.JOBS_FILE = ROOT / "data" / "jobs.json"
server.AUTH_FILE = ROOT / "data" / "auth.json"
server.STATS_FILE = ROOT / "data" / "stats.json"
server.ensure_dirs()

SEEN = []

class MockOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        SEEN.append({"path": self.path, "body": body})
        resp = json.dumps({
            "response": f"mock OCR 文本。",
            "message": {"content": f"mock OCR 文本。"},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass

mock = HTTPServer(("127.0.0.1", 8899), MockOllama)
threading.Thread(target=mock.serve_forever, daemon=True).start()

# 现场生成 5 页测试 PDF
doc = pymupdf.open()
for i in range(5):
    page = doc.new_page()
    page.insert_text((72, 100), f"page {i+1}")
pdf_path = ROOT / "test_book.pdf"
pdf_path.write_bytes(doc.tobytes())
doc.close()

job = {
    "id": "ollama-check-job",
    "slug": "ollama-check",
    "title": "测试书",
    "author": "作者",
    "config": {
        "book_title": "测试书",
        "author": "作者",
        "backend": "glm-ocr",
        "first_page": 1, "last_page": 5,
        "ollama_url": "http://127.0.0.1:8899",
        "ocr_model": "glm-ocr",
    },
    "pdf_path": str(pdf_path),
}
jobs = {job["id"]: job}
server.JOBS.update(jobs)
server.run_job(job["id"])

# 校验 5 次调用：路径 /api/chat，模型名，图片 base64
assert len(SEEN) == 5, f"调用次数错误: {len(SEEN)}"  # 每页重试 1 次成功 = 5 次成功调用
for s in SEEN:
    assert s["path"] in ("/api/generate", "/api/chat"), s["path"]
    body = s["body"]
    assert body["model"] == "glm-ocr", body["model"]
    b = s["body"]
    assert b["model"] == "glm-ocr", b["model"]
    assert isinstance(b.get("stream"), bool)
    images = b.get("images") or b.get("messages", [{}])[0].get("images")
    assert images, "缺少图片字段"
    assert isinstance(images[0], str) and len(images[0]) > 100, "图片应为 base64 字符串"
    print("✅", s["path"], "model=", b["model"], "stream=", b.get("stream"), "images=", len(images))

# 校验 OCR 结果写回了任务
job = jobs["ollama-check-job"]
print("job keys:", sorted(job.keys()))
status = job.get("status")
print("status:", status)
if status == "done":
    print("✅ run_job 流程走通，OCR 文本已按页记录")
else:
    raise SystemExit("❌ run_job 未正常完成: " + str({k: job.get(k) for k in ("status", "error")}))

print("\n🎉 Ollama 调用参数校验通过")
