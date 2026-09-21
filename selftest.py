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

# 失败块一次对半重试：正常 / 偷懒 / 幻觉 / 丢标记 / 异常 五类场景
_calls, _opts = {"n": 0}, []


def _fake_chat(base, model, messages, options=None, timeout=600, keep_alive="30m"):
    _calls["n"] += 1
    _opts.append(options)
    body = messages[1]["content"].split("【本次待校对文本】\n", 1)[1] \
                              .rsplit("\n\n【输出要求】", 1)[0]
    if "第一段" in body:
        return body.replace("—", "一")
    if "第2段" in body and len(body) > 80:
        return body[:len(body) // 2]
    if "第3段" in body and len(body) > 80:
        return body + "幻觉内容" * 10
    if "第四段" in body and len(body) > 80:
        return body.replace("\ue0005\ue001", "")
    if "第5段" in body and len(body) > 80:
        raise RuntimeError("boom")
    return body.replace("校队", "校对")


_real, server.ollama_chat_messages = server.ollama_chat_messages, _fake_chat
_logs = []
_records = []
try:
    _out = server.proofread_with_llm(_book, "http://x", "fake",
                                    {"proof_chunk_chars": 120, "proof_retry_min_chars": 40}, "t",
                                    lambda j, m: _logs.append(m), records=_records)
finally:
    server.ollama_chat_messages = _real

assert _calls["n"] > 6
assert "第一段。他说道这是第一段的正文" in _out                     # 正常→采纳
assert "这段正常校对通过" in _out                                  # 正常→采纳
assert "[[NOTE_REF:5|⑤]]" in _out                                 # 引注标记保留
assert any("." in str(r["i"]) for r in _records)                # 记录包含子块标号（如 2.a/2.b）
assert any(r["status"] == "ok" and "." in str(r["i"]) for r in _records)  # 子块重试可通过
assert all(o["temperature"] == 0.0 and o["top_p"] == 0.1 for o in _opts)  # 低温确定性
assert all(any(k in m for m in _logs) for k in
           ("丢失引注标记", "对半切开重试一次", "校对完成"))
assert "省略号" in server.PROOFREAD_SYSTEM_PROMPT
assert "禁止润色" in server.PROOFREAD_SYSTEM_PROMPT
assert "测试块" in server.PROOFREAD_USER_TEMPLATE.format(chunk="测试块")
print("✅ 校对引擎单测通过（分块守恒/引注保护/兜底判定/失败块对半重试/低温参数）")

# ==================== 结构页识别 / 段首去重 / 真实目录页 OCR ====================
# 1) 段首字符递归比对去重（用户算法）
assert server.shared_prefix_len("正文重复开头ABCDE", "正文重复开头XYZ") == 6  # 共享"正文重复开头"6字
assert server.shared_prefix_len("甲乙", "甲乙") == 2
_page = "春天来了万物复苏鸟语花香柳树发芽。\n\n春天来了万物复苏鸟语花香桃花也开了。\n\n另一段完全不同的内容保持原样。"
_deduped, _trim, _rm = server.dedupe_page_paragraph_prefixes(_page)
assert _trim == 1 and _rm == 0, (_trim, _rm)
assert "桃花也开了" in _deduped and _deduped.count("春天来了万物复苏鸟语花香") == 1
# 整段被前段包含 → 后段整段删除
_full_dup = "这一段是完全重复的内容出现两次。\n\n这一段是完全重复的内容出现两次。"
_d2, _t2, _r2 = server.dedupe_page_paragraph_prefixes(_full_dup)
assert _r2 == 1 and _d2.count("完全重复") == 1
# 共有前缀 ≤4 字（正常段落开头相似）→ 不动
_ok_page = "第一章开篇。\n\n第一章正文从这里开始。"
_d3, _t3, _r3 = server.dedupe_page_paragraph_prefixes(_ok_page)
assert _t3 == 0 and _r3 == 0 and _d3 == _ok_page
# 多轮递归：第一轮删共同前缀，第二轮再互检出新增的前缀重复
_rounds = "起始段落内容一。\n\n起始段落内容一起始段落后续。\n\n起始段落内容一起始段落后续再重复一次。"
_d4, _t4, _r4 = server.dedupe_page_paragraph_prefixes(_rounds)
assert _d4 == "起始段落内容一。\n\n起始段落后续。\n\n再重复一次。", repr(_d4)
assert _d4.count("起始段落内容一。") == 1

# 2) 自动结构页识别（封面/封底/扉页/版权/目录/正文）
assert server.classify_page_type("约翰逊博士传", 1, 10, pos=0) == "cover"
assert server.classify_page_type(
    "图书在版编目（CIP）数据\n书号 ISBN 978-7-02-015894-5\n出版发行：人民文学出版社\n印次：2023年第1次印刷", 5, 10, pos=3) == "copyright"
assert server.classify_page_type(
    "目 录\n第一章 早年生活…1\n第二章 牛津岁月…24\n第三章 伦敦初到…52\n第四章 文坛成名…80\n第五章 晚年岁月…130", 6, 10, pos=4) == "toc"
assert server.classify_page_type("本书完\n感谢阅读", 10, 10, pos=9) == "back_cover"
assert server.classify_page_type("约翰逊博士传\n（英）詹姆斯·鲍斯威尔 著", 3, 40, pos=2) == "title_page"
assert server.classify_page_type("正文第一段内容详细叙述着故事情节的发展变化。" * 5, 9, 40, pos=6) is None
_proc = [
    {"page": 1, "text": "约翰逊博士传", "illustration": False},
    {"page": 2, "text": "约翰逊博士传\n（英）詹姆斯·鲍斯威尔 著", "illustration": False},
    {"page": 3, "text": "书号 ISBN 978-7-02-015894-5\n出版发行：人民文学出版社\n版权所有 翻版必究", "illustration": False},
    {"page": 4, "text": "目 录\n第一章 早年生活…1\n第二章 牛津岁月…24\n第三章 伦敦初到…52\n第四章 文坛成名…80\n第五章 晚年岁月…130", "illustration": False},
    {"page": 5, "text": "第一章 早年生活\n\n正文内容开始叙述童年的经历与成长环境。", "illustration": False},
    {"page": 6, "text": "正文继续展开叙述更多细节内容以充实故事情节发展。", "illustration": False},
]
_auto = server.auto_classify_pages(_proc)
assert _auto["cover"] == [1], _auto
assert _auto["title_page"] == [2], _auto
assert _auto["copyright"] == [3], _auto
assert _auto["toc"] == [4], _auto
_desc = server.describe_auto_pages(_auto)
assert "封面=1" in _desc and "目录=4" in _desc

# 3) 更贴近真实的目录页 OCR 回归（点线/空格/页码/噪声混合）
_real_toc = """目 录
第一章早年生活……………………1
第二章牛津岁月…………………24
第三章伦敦初到 52
第四章文坛成名 · · · · · · · · 80
第五章晚年岁月……130
第六章书信往来          168
一个不太像目录的普通段落，没有页码结尾。
第七章附录资料………201"""
_entries = server.extract_toc_entries_from_text(_real_toc)
_titles = [t for t, _p in _entries]
assert len(_entries) == 7, _entries
assert _titles[0] == "第一章早年生活" and _entries[0][1] == 1
assert _entries[2] == ("第三章伦敦初到", 52)
assert _entries[4] == ("第五章晚年岁月", 130)
assert _entries[6] == ("第七章附录资料", 201)
assert all("普通段落" not in t for t in _titles)
_toc_kind = server.classify_page_type(_real_toc, 4, 10, pos=3)
assert _toc_kind == "toc", _toc_kind

# 4) 推荐默认参数：键完整、可补缺、不覆盖人工值
_missing = {"dpi": None, "proof_model": ""}
_filled = server.apply_recommended_defaults(dict(server.RECOMMENDED_DEFAULTS, **_missing))
assert _filled["dpi"] == 200 and _filled["proof_model"] == "qwen14b-pro"
_manual = server.apply_recommended_defaults({"dpi": 300, "proofread": True})
assert _manual["dpi"] == 300 and "mode" not in _manual  # 人工值不动，流程分支键不自动填
assert server.RECOMMENDED_DEFAULTS["prefix_dedupe_min_shared"] == 5
assert set(server._RECOMMENDED_SAFE_KEYS) <= set(server.RECOMMENDED_DEFAULTS)

# 5) 人工分章 + 自动识别并行：留空字段回退自动识别
_ch = server.build_manual_chapters(_proc, {"chapter_config_enabled": True, "chapter_method": "toc"},
                                    pdf_toc_chapters=[], auto_pages=_auto)
_ch_titles = [t for t, _b in _ch]
assert "封面" in _ch_titles and "扉页" in _ch_titles and "版权页" in _ch_titles and "目录" in _ch_titles, _ch_titles
# 人工覆盖：指定封面为第 2 页，自动值被忽略
_ch2 = server.build_manual_chapters(_proc, {"chapter_config_enabled": True, "cover_page": 2,
                                            "chapter_method": "toc"},
                                    pdf_toc_chapters=[], auto_pages=_auto)
_cover_body = [b for t, b in _ch2 if t == "封面"]
assert _cover_body and "鲍斯威尔" in _cover_body[0], _cover_body
print("✅ 结构页识别/段首去重/真实目录页OCR/默认参数 单测通过")


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
assert not server.is_chapter_line("1. 研究背景")
assert server.is_chapter_line("1. Chapter One")
stitched = server.merge_wrapped_lines(server.join_processed_pages([
    {"page": 1, "text": "这一段跨页未完", "illustration": False},
    {"page": 2, "text": "下一页接着写完。\n\n新一段。", "illustration": False},
]))
assert stitched.split("\n") == ["这一段跨页未完下一页接着写完。", "", "新一段。"], stitched
deduped_pages, removed = server.dedupe_processed_pages([
    {"page": 1, "text": "甲段。\n\n乙段。", "illustration": False},
    {"page": 2, "text": "乙段。\n\n丙段。", "illustration": False},
])
assert removed == 1 and deduped_pages[1]["text"] == "丙段。", deduped_pages
print("✅ 注释抽取不会打乱正文")

manual_processed = [
    {"page": 1, "text": "封面", "illustration": False},
    {"page": 2, "text": "封底", "illustration": False},
    {"page": 3, "text": "扉页", "illustration": False},
    {"page": 4, "text": "版权页", "illustration": False},
    {"page": 5, "text": "目录\n\n第一章 起始 …… 1\n第二章 收束 …… 3", "illustration": False},
    {"page": 6, "text": "目录续", "illustration": False},
    {"page": 7, "text": "序言第一页。", "illustration": False},
    {"page": 8, "text": "序言第二页。", "illustration": False},
    {"page": 9, "text": "第一章第一页。", "illustration": False},
    {"page": 10, "text": "第一章第二页未完", "illustration": False},
    {"page": 11, "text": "续页收束。", "illustration": False},
    {"page": 12, "text": "下章第一页。", "illustration": False},
    {"page": 13, "text": "下章第二页。", "illustration": False},
]
manual_cfg = {
    "cover_page": 1,
    "back_cover_page": 2,
    "title_page": 3,
    "copyright_page": 4,
    "toc_page_range": [5, 6],
    "preface_page_range": [7, 8],
    "chapter_method": "toc",
    "chapter_target_pages": 2,
}
assert not server.has_manual_chapter_config(manual_cfg)
manual_cfg_enabled = dict(manual_cfg, chapter_config_enabled=True)
assert server.has_manual_chapter_config(manual_cfg_enabled)
toc_chapters = server.build_manual_chapters(manual_processed, manual_cfg_enabled, [])
assert [t for t, _ in toc_chapters[:6]] == ["封面", "封底", "扉页", "版权页", "目录", "序言"], toc_chapters
assert [t for t, _ in toc_chapters[6:]] == ["第一章 起始", "第二章 收束"], toc_chapters
fixed_chapters = server.build_manual_chapters(
    manual_processed[8:],
    {"chapter_method": "fixed", "chapter_target_pages": 2},
    [],
)
assert len(fixed_chapters) == 2 and "续页收束。" in fixed_chapters[0][1], fixed_chapters
mixed_page_chapters = server.build_manual_chapters([
    {"page": 9, "text": "正文页。", "illustration": False},
    {"page": 9, "text": server.image_marker_md(Path("plate.jpg"), "插图"), "illustration": True},
], {"chapter_method": "fixed", "chapter_target_pages": 1}, [])
assert "正文页。" in mixed_page_chapters[0][1] and "![插图](images/plate.jpg)" in mixed_page_chapters[0][1], mixed_page_chapters
print("✅ 人工分页分章（目录页/固定页数）可用")

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

notes_epub = ROOT / "books" / "注释重编.epub"
server.build_epub(
    notes_epub,
    "注释重编",
    "测试作者",
    [{
        "title": "正文",
        "body": "正文[[NOTE_REF:7|③]]继续[[NOTE_REF:8|⑩]]。",
        "notes": [
            {"id": 7, "label": "③", "text": "第三条原始注释"},
            {"id": 8, "label": "⑩", "text": "第十条原始注释"},
        ],
    }],
)
with zipfile.ZipFile(notes_epub) as z:
    chap = z.read("OEBPS/chap_0001.xhtml").decode("utf-8")
    assert 'href="#note-7">[1]</a>' in chap and '<strong>[1]</strong> 第三条原始注释' in chap, chap
    assert 'href="#note-8">[2]</a>' in chap and '<strong>[2]</strong> 第十条原始注释' in chap, chap
print("✅ EPUB 注释按章重编号")

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

        server.paddle_layout_detect = lambda _p: [
            {"label": "Text", "bbox": [0, 0, 220, 220], "score": 0.99},
            {"label": "Image", "bbox": [20, 20, 200, 170], "score": 0.95},
            {"label": "figure", "bbox": [5, 5, 35, 35], "score": 0.99},
        ]
        server.ocr_page_glmocr = lambda _cfg, p, attempt=1: "正文段落" if p.stem.endswith("_crop") else ""
        mixed = server.ocr_page_paddle_glm({}, probe)
        assert "正文段落" in mixed and mixed.count("![") == 1, mixed
        print("✅ paddle-layout 保留真实插图并过滤伪图")
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

# ==================== 校对：对照表 / 备份回退 / 单独校对 ====================
print("\n===== 校对链路 =====")
pr = server.pr

# --- 1. 改动比对与对照表渲染 ---
assert ("巳", "已") in pr.change_pairs("他巳经到此。", "他已经到此。")
assert pr.change_pairs("完全相同", "完全相同") == []
assert pr.classify_change("巳", "已")[0] == "错字"
assert pr.classify_change("", "新增了一句")[1] == "warn"
assert pr.classify_change("，", "。")[0] == "标点"
assert pr.classify_change("很长的一段旧文本内容", "完全不同的一段新文本")[0] == "大段改写"

recs = [
    {"i": 1, "total": 2, "status": "ok", "reason": "", "orig": "他巳经到此。", "fixed": "他已经到此。"},
    {"i": 2, "total": 2, "status": "ok", "reason": "", "orig": "甲未末。", "fixed": "甲未末。。"},
]
agg = pr.summarize_changes(recs)
assert {a["old"] for a in agg} == {"巳", ""}, agg
assert agg[0]["level"] == "warn", "增补应排在最前（风险优先）"
assert next(a for a in agg if a["old"] == "巳")["count"] == 1
assert pr.summarize_changes([{"i": 1, "total": 1, "status": "fallback", "reason": "x",
                              "orig": "甲", "fixed": "乙"}]) == [], "回退块不列入对照表"

news = {"model": "fake", "total": 2, "n_ok": 2, "n_fallback": 0, "n_changed": 2}
assert "改动明细" in pr.render_report_html("测试", recs, news)
assert "巳" in pr.render_report_md("测试", recs, news)
assert "<del>" in pr.render_report_html("测试", recs, news), "应高亮原文被删改处"
print("✅ 对照表渲染（改动聚合/风险排序/回退块排除/高亮）")

# --- 2. 备份 / 回退 / 只留最近 N 份 ---
tb = ROOT / "books" / "_校对备份测试"
shutil.rmtree(tb, ignore_errors=True)
tb.mkdir(parents=True)
(tb / "book.md").write_text("原始正文", encoding="utf-8")
(tb / "book.source.md").write_text("原始正文", encoding="utf-8")
(tb / "notes.json").write_text("[]", encoding="utf-8")
(tb / "测试书名.epub").write_bytes(b"PK-old")

snap = pr.backup_artifacts(tb, note="测试备份")
assert snap.is_dir() and (snap / "book.md").read_text(encoding="utf-8") == "原始正文"
assert (snap / "测试书名.epub").read_bytes() == b"PK-old", "EPUB 也要进快照"
assert pr.list_backups(tb)[0]["note"] == "测试备份"

(tb / "book.md").write_text("被校对改过的正文", encoding="utf-8")
(tb / "book.proofread.md").write_text("校对后正文", encoding="utf-8")
pr.write_report(tb, "校对测试", recs, {"model": "fake"})
(tb / "测试书名.epub").write_bytes(b"PK-new")
assert (tb / "proofread-report.html").is_file()

info = pr.restore_backup(tb)
assert "book.md" in info["restored"] and "测试书名.epub" in info["restored"], info
assert "book.proofread.md" in info["removed"] and "proofread-report.html" in info["removed"], info
assert (tb / "book.md").read_text(encoding="utf-8") == "原始正文"
assert (tb / "测试书名.epub").read_bytes() == b"PK-old"
assert not (tb / "book.proofread.md").exists() and not (tb / "proofread-report.html").exists()

for i in range(5):
    pr.backup_artifacts(tb, tag=f"2020010{i}-000000")
assert len(pr.list_backups(tb)) == pr.KEEP_BACKUPS, pr.list_backups(tb)
print("✅ 备份/一键回退/清理旧备份")

# --- 3. 校对源优先级 + 打包复用（注释要能挂回章节）---
sd = ROOT / "books" / "_校对源测试"
shutil.rmtree(sd, ignore_errors=True)
(sd / "images").mkdir(parents=True)
(sd / "book.source.md").write_text("## 第一章\n\n正文[[NOTE_REF:1|[1]]]。", encoding="utf-8")
(sd / "notes.json").write_text(json.dumps([{"id": 1, "label": "[1]", "text": "注释内容"}]),
                              encoding="utf-8")
(sd / "book.md").write_text("扁平化后的正文", encoding="utf-8")
text, notes, origin = server.load_proofread_source(sd)
assert origin == "book.source.md" and "[[NOTE_REF:1|[1]]]" in text, origin
assert notes and notes[0]["text"] == "注释内容"
assert server.notes_block(notes).startswith("# 注释") and "注释内容" in server.notes_block(notes)

epub2, chapters = server.build_epub_from_source(text, notes, sd, "校对源测试",
                                                "校对源测试", "作者", {"mode": "full"})
assert epub2.is_file() and len(chapters) == 1 and chapters[0]["notes"], chapters
with zipfile.ZipFile(epub2) as _z:
    _body = "".join(_z.read(n).decode("utf-8") for n in _z.namelist() if n.endswith(".xhtml"))
assert "注释内容" in _body, "章节注释未写入 EPUB"
(sd / "book.source.md").unlink()                # 退回到 book.md
assert server.load_proofread_source(sd)[2].startswith("book.md")
print("✅ 校对源优先带标记的 book.source.md，注释可挂回章节")

# --- 4. 不启用校对时清掉上一轮产物 ---
(sd / "book.proofread.md").write_text("x", encoding="utf-8")
pr.write_report(sd, "t", recs, {})
assert set(server.clear_proofread_products(sd)) >= {"book.proofread.md", "proofread-report.html"}
assert not (sd / "book.proofread.md").exists() and not (sd / "proofread-report.html").exists()
print("✅ 未勾选校对时上一轮校对产物被清理（转换走最快路径）")

# --- 5. 单独校对（HTTP 全链路）：备份 → 校对 → 重建 EPUB → 对照表 → 一键回退 ---
_keep = (server.ollama_chat_messages, server.ollama_loaded_models, server.ollama_unload)
server.ollama_loaded_models = lambda *a, **k: []
server.ollama_unload = lambda *a, **k: False
server.ollama_chat_messages = (
    lambda base, model, messages, options=None, timeout=600, keep_alive="30m":
    messages[1]["content"].split("【本次待校对文本】\n", 1)[1].rsplit("\n\n【输出要求】", 1)[0])
try:
    js = json.loads(get(f"/api/job/{jid2}"))
    bd = ROOT / "books" / js["slug"]
    assert (bd / "book.source.md").is_file(), "转换流程应留下带标记的校对源"
    assert (bd / "notes.json").is_file(), "转换流程应留下注释数据"
    before_src = (bd / "book.source.md").read_text(encoding="utf-8")
    assert js["can_proofread"] and not js["has_report"] and js["backup_count"] == 0

    rp = post(f"/api/proofread/{jid2}", json.dumps({"model": "fake", "proof_chunk_chars": 300}).encode(),
              {"Content-Type": "application/json"})
    assert rp["ok"], rp
    t0 = time.time()
    while time.time() - t0 < 60:
        s3 = json.loads(get(f"/api/job/{jid2}"))
        if s3["status"] == "done" and s3.get("proofread"):
            break
        if s3["status"] == "error":
            raise SystemExit("❌ 单独校对失败: " + str(s3.get("error")))
        time.sleep(0.2)
    else:
        raise SystemExit("❌ 单独校对超时")
    assert s3["has_report"] and s3["backup_count"] >= 1, s3
    assert (bd / "book.proofread.md").is_file()
    assert (bd / "proofread-report.md").is_file() and (bd / "proofread-report.json").is_file()
    assert s3["proofread"]["blocks"] >= 1 and s3["proofread"]["ok"] >= 1, s3["proofread"]
    assert "改动明细" in get(f"/api/report/{jid2}").decode("utf-8")
    bks = json.loads(get(f"/api/backups/{jid2}"))["backups"]
    assert bks and bks[0]["tag"] == s3["latest_backup"], (bks, s3["latest_backup"])
    print("✅ 单独校对完成:", s3["message"])
    print(f"✅ 对照表/备份接口正常（备份 {len(bks)} 份，最新 {bks[0]['tag']}）")

    try:
        rb = post(f"/api/rollback/{jid2}", b"{}", {"Content-Type": "application/json"})
    except urllib.error.HTTPError as _e:
        raise SystemExit("❌ 回退失败: " + _e.read().decode())
    assert rb["ok"] and rb["rollback"]["tag"], rb
    assert not (bd / "book.proofread.md").exists()
    assert not (bd / "proofread-report.html").exists()
    assert (bd / "book.source.md").read_text(encoding="utf-8") == before_src, "正文应还原"
    s4 = json.loads(get(f"/api/job/{jid2}"))
    assert not s4.get("proofread") and Path(s4["epub_path"]).stat().st_size > 0
    print("✅ 一键回退到校对前:", rb["rollback"]["tag"],
          "恢复", len(rb["rollback"]["restored"]), "个文件，删除", rb["rollback"]["removed"])
finally:
    (server.ollama_chat_messages, server.ollama_loaded_models,
     server.ollama_unload) = _keep
    with server.JOBS_LOCK:
        _j = server.JOBS.get(jid2)
        if _j:
            _j["action"] = "convert"
            _j["proofread"] = None
    server.save_jobs()
    shutil.rmtree(tb, ignore_errors=True)
    shutil.rmtree(sd, ignore_errors=True)

# 断点续跑
json.loads(get(f"/api/job/{jid2}"))
r3 = post(f"/api/retry/{jid2}", b"{}", {"Content-Type": "application/json"})
print("✅ retry 响应:", r3)

print("\n🎉 全部端到端用例通过")
