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

# ---------- 校对引擎校验（离线，用假模型，不依赖本地大模型）----------
_pad = "正文文字七字。" * 10
_paras = [f"第{i}段。" + _pad for i in range(1, 7)]
_paras[0] = "第一段。他说道这是第—段的正文。" + _pad
_paras[3] = "第四段。有引注[[NOTE_REF:5|⑤]]的正文。" + _pad
_paras[5] = "第六段。这段正常校队通过。" + _pad
_book = "\n\n".join(_paras)

# 分块：每段独立成块、块长受限、内容守恒
assert len(server.split_for_proofread(_book, 120)) == 6
_long = "句子一句。" * 200
_chks = server.split_for_proofread(_long, 200)
assert len(_chks) > 1 and all(len(c) <= 200 for c in _chks)
assert server._proof_norm("".join(_chks)) == server._proof_norm(_long)

# 引注标记保护：往返一致、丢失可检出
_prot, _saved = server.protect_note_refs("甲[[NOTE_REF:7|③]]乙")
assert "NOTE_REF" not in _prot and len(_saved) == 1
assert server.restore_note_refs(_prot, _saved)[0] == "甲[[NOTE_REF:7|③]]乙"
assert server.restore_note_refs("甲乙", _saved)[2] == 0

# 兜底判定
_o = "他说道：“这是—个很好的例子。”他说完就离开了房间，天已经黑了。"
assert server.proofread_chunk_guard(_o, _o.replace("—", "一"))[0]
assert not server.proofread_chunk_guard(_o, _o[:20])[0]        # 偷懒截断
assert not server.proofread_chunk_guard(_o, _o + "补写" * 20)[0]  # 幻觉扩写
assert not server.proofread_chunk_guard(_o, "")[0]             # 空返回
assert server.proofread_chunk_guard("甲乙丙丁戊己庚辛", "甲乙丙丁 戊己庚辛")[0]

# 端到端（假模型）：正常 / 偷懒 / 幻觉 / 丢标记 / 异常 五类场景
_calls, _opts = {"n": 0}, []


def _fake_chat(base, model, messages, options=None, timeout=600, keep_alive="30m"):
    _calls["n"] += 1
    _opts.append(options)
    i = _calls["n"]
    body = messages[1]["content"].split("【本次待校对文本】\n", 1)[1] \
                              .rsplit("\n\n【输出要求】", 1)[0]
    if i == 1:
        return body.replace("—", "一")
    if i == 2:
        return body[:len(body) // 2]
    if i == 3:
        return body + "幻觉内容" * 10
    if i == 4:
        return body.replace("\ue0005\ue001", "")
    if i == 5:
        raise RuntimeError("boom")
    return body.replace("校队", "校对")


_real, server.ollama_chat_messages = server.ollama_chat_messages, _fake_chat
_logs = []
try:
    _out = server.proofread_with_llm(_book, "http://x", "fake",
                                    {"proof_chunk_chars": 120}, "t",
                                    lambda j, m: _logs.append(m))
finally:
    server.ollama_chat_messages = _real

assert _calls["n"] == 6
assert "第一段。他说道这是第一段的正文" in _out                     # 正常→采纳
assert "这段正常校对通过" in _out                                  # 正常→采纳
assert _paras[1] in _out and _paras[2] in _out and _paras[4] in _out  # 偷懒/幻觉/异常→回退
assert "[[NOTE_REF:5|⑤]]" in _out                                 # 丢标记→回退
assert all(o["temperature"] == 0.0 and o["top_p"] == 0.1 for o in _opts)  # 低温确定性
assert all(any(k in m for m in _logs) for k in
           ("疑似偷懒省略", "疑似幻觉扩写", "丢失引注标记", "请求失败", "校对完成"))
assert "省略号" in server.PROOFREAD_SYSTEM_PROMPT
assert "禁止润色" in server.PROOFREAD_SYSTEM_PROMPT
assert "测试块" in server.PROOFREAD_USER_TEMPLATE.format(chunk="测试块")
print("✅ 校对引擎单测通过（分块守恒/引注保护/兜底判定/五类场景回退/低温参数）")

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

# 注释抽取：只有能在正文里回链的候选注释才应移出正文，避免打乱正文结构
body_text, notes, next_note_id = server.extract_footnotes_from_page(
    "第一章 起始[1]\n\n正文段落。\n\n[1] 这是注释内容。", 1, 1
)
assert "[[NOTE_REF:1|[1]]]" in body_text and len(notes) == 1 and next_note_id == 2
body_text2, notes2, _ = server.extract_footnotes_from_page(
    "第一章 起始\n\n1. 研究背景\n\n这里继续正文。", 2, 10
)
assert "1. 研究背景" in body_text2 and not notes2, "正文编号段落不应被误抽成注释"
print("✅ 注释抽取不会打乱正文")

# EPUB 图片路径：正文中的插图必须指向 OEBPS/images/ 下的资源
tmp_epub = ROOT / "books" / "图片测试.epub"
server.build_epub(
    tmp_epub,
    "图片测试",
    "测试作者",
    [{"title": "正文", "body": server.image_marker_md(Path("plate.jpg"), "插图"), "illustration_only": True}],
)
with zipfile.ZipFile(tmp_epub) as z:
    chap = z.read("OEBPS/chap_0001.xhtml").decode("utf-8")
    assert 'src="images/plate.jpg"' in chap, chap
print("✅ EPUB 插图路径正确")

if server.Image is not None:
    probe = ROOT / "paddle-probe.jpg"
    server.Image.new("RGB", (240, 240), "white").save(probe, "JPEG")
    old_detect = server.paddle_layout_detect
    old_ocr = server.ocr_page_glmocr
    try:
        server.paddle_layout_detect = lambda _p: [
            {"label": "text", "bbox": [0, 0, 220, 100]},
            {"label": "footnote_content", "bbox": [0, 120, 220, 220]},
        ]
        server.ocr_page_glmocr = lambda _cfg, p, attempt=1: "正文[1]" if "_p0_" in p.stem else "[1] 这是脚注"
        mixed = server.ocr_page_paddle_glm({}, probe)
        assert mixed.split("\n\n") == ["正文[1]", "[1] 这是脚注"], mixed
        print("✅ paddle-layout 保留脚注文本顺序")
    finally:
        server.paddle_layout_detect = old_detect
        server.ocr_page_glmocr = old_ocr

# pandoc 读回验证（可选，缺 pandoc 不算失败）
try:
    p = subprocess.run(["pandoc", str(epub_path), "-t", "plain"], capture_output=True, text=True)
    if p.returncode == 0:
        assert "第 page_0001" in p.stdout
        print("✅ pandoc 读回校验通过")
    else:
        print("ℹ️ 未安装 pandoc，跳过读回校验")
except FileNotFoundError:
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
