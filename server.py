#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ScanLibrary —— 本地扫描书 → 可重排 EPUB 工作台

设计目标：
  1. 零云服务、零 API 费用：OCR 走本机 Ollama（glm-ocr 视觉模型），校对可用已装文本模型。
  2. 一条命令启动浏览器页面：上传 PDF → 后台转换 → 自动落到 books/<书名>/ 目录 → 页面直接下载。
  3. 断点续跑：每一页的 OCR 结果单独落盘，中断后重跑自动跳过已完成页。

依赖：pip install pymupdf Pillow
  - pymupdf：把 PDF 页渲染成图片
  - Pillow  ：paddle-layout 后端用：版面分析后裁剪文字/插图区域
可选：系统装 pandoc（仅在使用 pandoc 打包后端时需要，默认不用）

另外，后端 "paddle-layout" 需要本机有 PaddleOCR 3.x（通过 SCANLIBRARY_PADDLE_VENV 环境变量指定 venv 中的 python3 路径）。

启动：
    python3 server.py                 # 默认 http://127.0.0.1:8765
    python3 server.py --root ~/ScanLibrary --port 8765
"""

import argparse
import base64
import difflib
import html
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from PIL import Image  # paddle-layout 后端用：版面分析后裁剪文字/插图区域
except ImportError:
    Image = None  # 其他后端不依赖 PIL

from urllib.parse import unquote, urlparse
from urllib.request import Request, build_opener, ProxyHandler

# 本机 Ollama 请求一律直连，避免被系统代理（如 Clash）劫持导致 502
urlopen = build_opener(ProxyHandler({})).open
from urllib.error import URLError, HTTPError
from concurrent.futures import ThreadPoolExecutor

APP_NAME = "ScanLibrary"
VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------

ROOT: Path = None           # 数据根目录
JOBS_FILE: Path = None      # 任务索引
JOBS = {}                   # job_id -> job dict
JOBS_LOCK = threading.Lock()
TASK_QUEUE: "queue.Queue[str]" = queue.Queue()
CANCEL_FLAGS = {}           # job_id -> True 表示请求取消
LOG_BUFFERS = {}            # job_id -> list[str]

# ---- 认证 / 空闲模型管理 / 历史耗时统计 ----
AUTH_FILE: Path = None      # 登录密码文件（明文，本地单用户）
STATS_FILE: Path = None     # 历史每页耗时统计（用于全书时长估算）
PASSWORD = [None]           # 当前密码（--password 或 auth.json）
OCR_FAIL_TAG = "【OCR-FAILED】"  # 失败页缓存标记：重跑时识别并重新 OCR
NO_TEXT_TOKEN = "〔无文字〕"

# ---- PP-DocLayout_plus-L 类别映射（仅 paddle-layout 后端使用）----
# 丢弃：规则化移除，永远不进正文
PADDLE_DROP_LABELS = frozenset({"header", "footer", "page_number", "seal"})
# 嵌入：保留为 jpg 图，markdown 引用
PADDLE_FIGURE_LABELS = frozenset({"figure", "chart"})
# 资产图：表格/公式以图片形式嵌入（不强求 OCR 还原复杂排版）
PADDLE_ASSET_LABELS = frozenset({"table", "formula", "equation"})
# 文本：裁剪后调 glm-ocr
PADDLE_TEXT_LABELS = frozenset({
    "text", "doc_title", "paragraph_title", "reference_title",
    "figure_title", "table_title", "reference_content", "reference",
    "abstract", "algorithm", "catalogue",
})
TOKENS: dict = {}           # token -> 过期时间戳
LAST_ACTIVITY = [time.time()]  # 最后一次已认证请求时间
IDLE_MINUTES = 30           # 空闲超过该分钟数且无任务时：清登录态+卸载模型


def log(job_id, msg):
    """写任务日志（同时打印到控制台）。"""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with JOBS_LOCK:
        LOG_BUFFERS.setdefault(job_id, []).append(line)
        if len(LOG_BUFFERS[job_id]) > 400:
            LOG_BUFFERS[job_id] = LOG_BUFFERS[job_id][-400:]
        job = JOBS.get(job_id)
        if job is not None:
            job["message"] = msg
    print(f"  {job_id[:8]} | {line}", flush=True)


# ---------------- 认证 ----------------
def load_or_create_password() -> str:
    """读取或首次生成随机密码，存于 data/auth.json（可手改）。"""
    default = "scan-" + uuid.uuid4().hex[:4]
    if AUTH_FILE.exists():
        try:
            d = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            if d.get("password"):
                return str(d["password"])
        except Exception:
            pass
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps({"password": default}, ensure_ascii=False, indent=2), encoding="utf-8")
    return default

def new_token() -> str:
    tok = uuid.uuid4().hex
    TOKENS[tok] = time.time() + 8 * 3600
    return tok

def token_ok(req) -> bool:
    """校验 Bearer token（滑动续期），并刷新最后活动时间。"""
    h = req.headers.get("Authorization", "")
    tok = h[7:] if h.startswith("Bearer ") else ""
    exp = TOKENS.get(tok)
    if exp and exp > time.time():
        TOKENS[tok] = time.time() + 8 * 3600
        LAST_ACTIVITY[0] = time.time()
        return True
    if tok:
        TOKENS.pop(tok, None)
    return False

def prune_tokens():
    now = time.time()
    for t in [t for t, e in TOKENS.items() if e <= now]:
        TOKENS.pop(t, None)

# ---------------- 历史耗时统计（全书时长估算依据）----------------
def stats_key(cfg: dict) -> str:
    return f"{cfg.get('backend', 'glm-ocr')}@{cfg.get('dpi', 200)}dpi"

def load_stats() -> dict:
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def record_pages_sec(key: str, pages: int, serial_seconds: float):
    """serial_seconds = 墙钟耗时 × 并发数（折算为串行页耗时），消除并发差异。"""
    if pages <= 0 or serial_seconds <= 0:
        return
    st = load_stats()
    e = st.get(key) or {"pages": 0, "total_sec": 0.0}
    # 限制累计样本量，近期经验权重更高
    if e.get("pages", 0) > 2000:
        e["pages"] = 1000
        e["total_sec"] = e["total_sec"] / 2
    e["pages"] = e.get("pages", 0) + pages
    e["total_sec"] = e.get("total_sec", 0.0) + serial_seconds
    e["updated_at"] = now_iso()
    st[key] = e
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATS_FILE)

def estimate_seconds(pages: int, cfg: dict):
    """按历史经验估算全书 OCR 墙钟秒数；历史样本不足返回 None。"""
    if pages <= 0:
        return None
    e = load_stats().get(stats_key(cfg))
    if not e or e.get("pages", 0) < 5:
        return None
    per_serial = e["total_sec"] / e["pages"]
    conc = max(1, int(cfg.get("concurrency", 1) or 1))
    return per_serial * pages / conc

def fmt_minutes(sec) -> str:
    m = int(round(sec / 60))
    if m >= 60:
        return f"{m // 60} 小时 {m % 60} 分钟"
    return f"{max(1, m)} 分钟"

# ---------------- Ollama 模型生命周期 ----------------
def ollama_loaded_models(base_url) -> list:
    try:
        with urlopen(base_url.rstrip("/") + "/api/ps", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []

def ollama_unload(base_url, model) -> bool:
    """keep_alive=0 请求：让 Ollama 立即把模型移出内存。"""
    try:
        req = Request(base_url.rstrip("/") + "/api/generate",
                      data=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=60) as r:
            r.read()
        return True
    except Exception:
        return False

def ollama_preload(base_url, model, minutes=180) -> bool:
    """空 prompt 请求预热模型（登录后预启动用）。"""
    try:
        req = Request(base_url.rstrip("/") + "/api/generate",
                      data=json.dumps({"model": model, "prompt": "", "keep_alive": f"{minutes}m"}).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=600) as r:
            r.read()
        return True
    except Exception:
        return False

def idle_watchdog():
    """空闲超过 IDLE_MINUTES 分钟且无运行中任务：清登录态并卸载大模型。"""
    while True:
        time.sleep(60)
        try:
            prune_tokens()
            with JOBS_LOCK:
                busy = any(j.get("status") in ("running", "queued") for j in JOBS.values())
            idle_for = time.time() - LAST_ACTIVITY[0]
            if busy or not TOKENS or idle_for < IDLE_MINUTES * 60:
                continue
            # 收集本服务涉及过的模型
            models = {"glm-ocr"}
            for j in JOBS.values():
                c = j.get("config", {})
                for k in ("ocr_model", "proof_model"):
                    if c.get(k):
                        models.add(c[k].split(":")[0])
            base = "http://localhost:11434"
            loaded = ollama_loaded_models(base)
            unloaded = []
            for m in sorted(models):
                for lm in loaded:
                    if lm.split(":")[0] == m:
                        if ollama_unload(base, lm):
                            unloaded.append(lm)
            TOKENS.clear()
            if unloaded:
                print(f"[idle] 已空闲 {int(idle_for // 60)} 分钟，卸载模型: {unloaded}", flush=True)
        except Exception as e:
            print(f"[idle] watchdog error: {e}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------------

def slugify(name, maxlen=60):
    """把书名转成一个安全目录名（保留中日韩字符）。"""
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "", name).strip()
    s = re.sub(r"\s+", "-", s)
    s = s.strip(".-")
    return (s or "book")[:maxlen]


def save_jobs():
    data = {}
    with JOBS_LOCK:
        for jid, j in JOBS.items():
            item = dict(j)
            item.pop("_future", None)
            data[jid] = item
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(JOBS_FILE)


def load_jobs():
    if not JOBS_FILE.exists():
        return
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for jid, j in data.items():
        if j.get("status") in ("running", "pending", "queued"):
            j["status"] = "error"
            j["message"] = "服务上次退出时该任务被中断（已完成的分页结果会保留，可重新转换续跑）"
        JOBS[jid] = j
        LOG_BUFFERS[jid] = [f"[{datetime.now().strftime('%H:%M:%S')}] 已从历史记录恢复"]


def ensure_dirs():
    for d in ("uploads", "books", "data"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# PDF → 页面图片
# ---------------------------------------------------------------------------

def _pymupdf():
    """兼容新旧两种导入名（pymupdf >=1.24 推荐 import pymupdf）。"""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        import fitz
        return fitz


def render_pages(pdf_path: Path, out_dir: Path, dpi=200, page_range=None):
    """把 PDF 每页渲染成图片，返回图片路径列表。

    最长边限制为 1600px、保存为 JPEG：超大 PNG（>1.5MB）会把
    Ollama 的 llama-server 直接打崩（HTTP 500 中断连接）。
    """
    fitz = _pymupdf()
    MAX_SIDE = 1600       # OCR 用足够，且远低于 Ollama 崩溃阈值
    MAX_REUSE_BYTES = 1_500_000

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    total = doc.page_count
    idxs = list(range(total))
    if page_range:
        idxs = [i for i in idxs if page_range[0] <= i + 1 <= page_range[1]]
    paths = []
    zoom = dpi / 72.0
    for i in idxs:
        jpg = out_dir / f"page_{i + 1:04d}.jpg"
        png = out_dir / f"page_{i + 1:04d}.png"
        if jpg.exists():
            paths.append((i + 1, jpg))
            continue
        if png.exists() and png.stat().st_size < MAX_REUSE_BYTES:
            # 旧缓存里体积安全的 png 直接复用
            paths.append((i + 1, png))
            continue
        # 渲染：最长边超限时按比例降 zoom
        rect = doc[i].rect
        z = zoom
        max_side = max(rect.width, rect.height) * z
        if max_side > MAX_SIDE:
            z = z * MAX_SIDE / max_side
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(z, z))
        pix.save(str(jpg), jpg_quality=85)
        if png.exists():
            png.unlink()  # 清掉超大的旧 png
        paths.append((i + 1, jpg))
    doc.close()
    return paths, total


def pdf_page_count(pdf_path: Path):
    try:
        fitz = _pymupdf()
        doc = fitz.open(str(pdf_path))
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return None


def extract_text_layer(pdf_path: Path):
    """若 PDF 本身有文字层，直接抽取（跳过 OCR，速度极快）。"""
    try:
        fitz = _pymupdf()
        doc = fitz.open(str(pdf_path))
        texts = [doc[i].get_text("text") or "" for i in range(doc.page_count)]
        doc.close()
        return texts
    except Exception:
        return None


def text_char_count(text: str) -> int:
    return len(re.findall(rf"[A-Za-z0-9{CJK}]", text or ""))


def image_marker_md(img_path: Path, label: str = "插图") -> str:
    return f"![{label}](images/{img_path.name})"


def safe_epub_asset_name(name: str) -> str:
    base = Path(name).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
    return safe or "asset.bin"


def likely_full_page_illustration(img_path: Path) -> bool:
    """粗略判定整页大图：低留白 + 绝大多数行都被图像覆盖。"""
    try:
        fitz = _pymupdf()
        doc = fitz.open(str(img_path))
        page = doc[0]
        rect = page.rect
        scale = min(1.0, 96.0 / max(rect.width, rect.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        doc.close()
        w, h, n = pix.width, pix.height, pix.n
        if w <= 0 or h <= 0 or n <= 0:
            return False
        white = dark = 0
        active_rows = 0
        samples = pix.samples
        row_thresh = max(3, int(w * 0.12))
        for y in range(h):
            row_dark = 0
            base = y * w * n
            for x in range(w):
                idx = base + x * n
                if n >= 3:
                    lum = (samples[idx] * 299 + samples[idx + 1] * 587 + samples[idx + 2] * 114) // 1000
                else:
                    lum = samples[idx]
                if lum >= 245:
                    white += 1
                if lum <= 110:
                    dark += 1
                    row_dark += 1
            if row_dark >= row_thresh:
                active_rows += 1
        total = w * h
        white_ratio = white / max(1, total)
        dark_ratio = dark / max(1, total)
        active_ratio = active_rows / max(1, h)
        return white_ratio <= 0.76 and dark_ratio >= 0.08 and active_ratio >= 0.72
    except Exception:
        return False


def split_image_halves(img_path: Path) -> list[Path]:
    """把顽固页面裁成上下两半，降低视觉上下文复杂度。"""
    fitz = _pymupdf()
    doc = fitz.open(str(img_path))
    try:
        page = doc[0]
        rect = page.rect
        mid = rect.y0 + rect.height / 2
        clips = [
            ("top", fitz.Rect(rect.x0, rect.y0, rect.x1, mid)),
            ("bottom", fitz.Rect(rect.x0, mid, rect.x1, rect.y1)),
        ]
        parts = []
        for suffix, clip in clips:
            out = img_path.with_suffix(f".{suffix}.jpg")
            pix = page.get_pixmap(clip=clip, alpha=False)
            pix.save(str(out), jpg_quality=85)
            parts.append(out)
        return parts
    finally:
        doc.close()


def is_suspicious_ocr_text(text: str) -> bool:
    s = (text or "").strip()
    if not s or s == NO_TEXT_TOKEN:
        return True
    chars = text_char_count(s)
    if re.fullmatch(r"[\s.。·•…—\-_=~]+", s):
        return True
    if re.search(r"[.。·•…]{6,}", s) and chars < max(80, len(s) // 2):
        return True
    if re.search(r"(.)\1{15,}", s) and chars < 60:
        return True
    return False


def strip_noise_lines(text: str) -> str:
    kept = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            kept.append("")
            continue
        if re.fullmatch(r"[0-9ivxlcdmIVXLCDM]{1,8}", line):
            continue
        if re.fullmatch(r"[·•\-.。…_=\s]{4,}", line):
            continue
        compact = re.sub(r"\s+", "", line)
        if re.fullmatch(r"[\W_]+", compact) and len(compact) >= 2:
            continue
        if re.search(r"(.{1,8})\1{3,}", compact):
            continue
        meaningful = text_char_count(line)
        non_space = len(compact)
        if non_space >= 8 and meaningful / max(1, non_space) < 0.22:
            continue
        kept.append(raw)
    return "\n".join(kept).strip()


NOTE_REF_RE = re.compile(r"\[\[NOTE_REF:(\d+)\|(.+?)\]\]")
NOTE_MARKER_RE = re.compile(
    r"^\s*(\[[0-9]{1,3}\]|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|"
    r"\([0-9]{1,3}\)|（[0-9]{1,3}）|[0-9]{1,3}[、.)]|注[：:])\s*"
)


def extract_footnotes_from_page(text: str, page_no: int, next_note_id: int):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    body, notes = [], []
    half = max(1, len(paras) // 2)
    for idx, para in enumerate(paras):
        m = NOTE_MARKER_RE.match(para)
        if m and idx >= half and len(para) <= 600:
            label = m.group(1).strip()
            note_text = para[m.end():].strip() or para.strip()
            label_pat = rf"(?<![A-Za-z0-9]){re.escape(label)}(?![A-Za-z0-9])"
            body_text = "\n\n".join(body).strip()
            linked_text, n = re.subn(
                label_pat,
                f"[[NOTE_REF:{next_note_id}|{label}]]",
                body_text,
                count=1,
            )
            if n:
                body = [linked_text] if linked_text else []
                notes.append({
                    "id": next_note_id,
                    "label": label,
                    "text": note_text,
                    "page": page_no,
                    "linked": True,
                })
                next_note_id += 1
                continue
        body.append(para)
    return "\n\n".join(body).strip(), notes, next_note_id


def collapse_repeated_paragraphs(text: str, similarity=0.88) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    out = []
    recent_norms = []
    for para in paras:
        para = re.sub(r"(.{12,260}?)(?:\s*\1){1,}", r"\1", para)
        norm = re.sub(rf"[^\w{CJK}]+", "", para)
        if len(norm) >= 80:
            dup_recent = False
            for prev_norm in recent_norms[-8:]:
                if prev_norm == norm:
                    dup_recent = True
                    break
                if prev_norm and difflib.SequenceMatcher(None, prev_norm, norm).ratio() >= 0.985:
                    dup_recent = True
                    break
            if dup_recent:
                continue
        if out:
            prev = out[-1]
            score = difflib.SequenceMatcher(None, prev, para).ratio()
            shorter = min(len(prev), len(para))
            if (shorter >= 20 and score >= similarity) or (shorter >= 40 and (prev in para or para in prev)):
                continue
        out.append(para)
        recent_norms.append(norm)
    return "\n\n".join(out)


def normalized_text_for_dedup(text: str) -> str:
    return re.sub(rf"[^\w{CJK}]+", "", text or "")


def looks_like_duplicate_page(text: str, recent_norms: list[str], similarity=0.99) -> bool:
    norm = normalized_text_for_dedup(text)
    if len(norm) < 120:
        return False
    for prev in recent_norms[-4:]:
        if not prev:
            continue
        if norm == prev:
            return True
        shorter = min(len(norm), len(prev))
        if shorter >= 120 and (norm in prev or prev in norm):
            return True
        if difflib.SequenceMatcher(None, prev, norm).ratio() >= similarity:
            return True
    return False


def render_note_refs_as_text(text: str) -> str:
    return NOTE_REF_RE.sub(lambda m: m.group(2), text or "")


def collect_note_refs_for_epub(text: str, notes: list[dict]) -> list[dict]:
    by_id = {n["id"]: n for n in notes}
    chapter_notes = []
    seen = set()
    for m in NOTE_REF_RE.finditer(text or ""):
        nid = int(m.group(1))
        note = by_id.get(nid)
        if note and nid not in seen:
            chapter_notes.append(note)
            seen.add(nid)
    return chapter_notes


# ---------------------------------------------------------------------------
# OCR 后端
# ---------------------------------------------------------------------------

def ollama_generate(base_url, model, prompt, images_b64=None, options=None, timeout=300):
    """调用 Ollama 原生 /api/generate 端点（视觉请求必须走这个端点）。"""
    payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": "3h"}
    if images_b64:
        payload["images"] = images_b64
    if options:
        payload["options"] = options
    req = Request(
        base_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("response") or "").strip()


def ollama_chat(base_url, model, prompt, options=None, timeout=600):
    """调用 Ollama 原生 /api/chat 端点（纯文本模型，用于校对）。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if options:
        payload["options"] = options
    req = Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return ((data.get("message") or {}).get("content") or "").strip()


def _shrink_image(img_path: Path, max_side: int) -> Path:
    """把图片最长边缩到 max_side（用于 OCR 重试时降分辨率，绕过模型死循环）。"""
    try:
        fitz = _pymupdf()
        doc = fitz.open(str(img_path))
        pix = doc[0].get_pixmap()
        m = max(pix.width, pix.height)
        if m <= max_side:
            doc.close()
            return img_path
        z = max_side / m
        p2 = doc[0].get_pixmap(matrix=fitz.Matrix(z, z))
        tmp = img_path.with_suffix(f".r{max_side}.jpg")
        p2.save(str(tmp), jpg_quality=85)
        doc.close()
        return tmp
    except Exception:
        return img_path


def ocr_page_glmocr(cfg, img_path: Path, attempt: int = 1):
    """单页 OCR：glm-ocr 视觉模型。

    重试策略：
    - attempt 1：原版 prompt，全分辨率
    - attempt 2：降分辨率 1280（改变视觉 token 布局）
    - attempt 3：中文兜底 prompt + 短输出——整页插图会让模型在
      原 prompt 下死循环（卡 2-3 分钟后 HTTP 500），中文 prompt
      带「如无文字输出〔无文字〕」可绕过；注意它会降低正常页
      质量，故只作最后兜底。
    """
    p = img_path
    if attempt >= 2:
        p = _shrink_image(img_path, 1280 if attempt == 2 else 1024)
    b64 = base64.b64encode(p.read_bytes()).decode()
    opts = {"num_ctx": int(cfg.get("num_ctx", 16384)), "temperature": 0,
            "num_predict": 8192}
    prompt = "Text Recognition:"
    timeout = int(cfg.get("page_timeout", 300))
    if attempt >= 3:
        prompt = (
            "请逐字逐句完整提取图片中的所有文字。严禁总结，严禁省略，"
            "严禁使用“...”或“略”等符号代替原文。"
            f"如果图片中没有文字，直接输出：{NO_TEXT_TOKEN}"
        )
        opts["num_predict"] = 1024
        timeout = min(timeout, 150)
    return ollama_generate(
        cfg.get("ollama_url", "http://localhost:11434"), cfg.get("ocr_model", "glm-ocr"),
        prompt=prompt,
        images_b64=[b64],
        options=opts,
        timeout=timeout,
    )


def ocr_page_stub(cfg, img_path: Path, attempt: int = 1):
    """测试后端：不 OCR，生成占位文本（用于验证流水线是否跑通）。"""
    return f"（stub 模式）第 {img_path.stem} 页占位文本。\n\n这是用于验证流水线的示例段落。"


def ocr_page_backend(cfg, img_path: Path) -> str:
    backend = cfg.get("backend", "glm-ocr")
    if backend == "stub":
        return clean_page_text(ocr_page_stub(cfg, img_path, 1))
    if backend == "paddle-layout":
        return clean_page_text(ocr_page_paddle_glm(cfg, img_path))
    return clean_page_text(ocr_page_with_fallback(cfg, img_path))


def ocr_page_with_fallback(cfg, img_path: Path) -> str:
    """整页 OCR 策略：整页插图直出图片，其余页面分级重试 + 半页切分。"""
    if likely_full_page_illustration(img_path):
        return image_marker_md(img_path, "整页插图")

    errors = []
    for attempt in range(1, 4):
        try:
            txt = strip_noise_lines(clean_page_text(ocr_page_glmocr(cfg, img_path, attempt)))
            if is_suspicious_ocr_text(txt):
                raise RuntimeError("OCR 返回疑似无效文本")
            return txt
        except Exception as e:  # noqa
            errors.append(str(e))
            if attempt < 3:
                time.sleep(2 ** attempt)

    parts = split_image_halves(img_path)
    part_out = []
    for part in parts:
        try:
            txt = strip_noise_lines(clean_page_text(ocr_page_glmocr(cfg, part, 3)))
            if txt and txt != NO_TEXT_TOKEN and not is_suspicious_ocr_text(txt):
                part_out.append(txt)
        except Exception as e:  # noqa
            errors.append(f"{part.name}: {e}")
    if part_out:
        merged = "\n".join(part_out).strip()
        if merged and not is_suspicious_ocr_text(merged):
            return merged
    raise RuntimeError("；".join(errors[-4:]) or "OCR 失败")


# =====================================================================
# 后端 C：PP-DocLayout 版面分析 + glm-ocr 分工识别
# =====================================================================
def _find_paddle_venv_python() -> str:
    """查找含 paddleocr 的 venv python3 路径。
    优先使用 SCANLIBRARY_PADDLE_VENV 环境变量；否则扫描
    ~/superstar/superstar3.1/projects/*/venv/bin/python3。
    """
    env = os.environ.get("SCANLIBRARY_PADDLE_VENV")
    if env and Path(env).exists():
        return env
    base = Path.home() / "superstar" / "superstar3.1" / "projects"
    if base.exists():
        for venv in base.glob("*/venv/bin/python3"):
            try:
                r = subprocess.run([str(venv), "-c", "import paddleocr"],
                                    capture_output=True, text=True, timeout=8)
                if r.returncode == 0:
                    return str(venv)
            except Exception:
                continue
    return None


def paddle_layout_detect(img_path: Path):
    """调子进程跑 PP-DocLayout 版面检测，返回 box 列表 [{label, score, bbox}]。
    失败抛 RuntimeError 让上层记日志并跳过。
    """
    py = _find_paddle_venv_python()
    if not py:
        raise RuntimeError(
            "未找到 PaddleOCR venv。请安装 paddleocr 3.x，或设置 SCANLIBRARY_PADDLE_VENV=/path/to/venv/bin/python3"
        )
    worker = Path(__file__).resolve().parent / "paddle_layout_worker.py"
    if not worker.exists():
        raise RuntimeError(f"paddle_layout_worker.py 不存在: {worker}")
    proc = subprocess.run([py, str(worker), str(img_path)],
                            capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"paddle_layout 子进程失败: exit={proc.returncode}, stderr={proc.stderr[-500:]}")
    out = proc.stdout.strip().splitlines()
    if not out:
        return []
    return json.loads(out[-1])


def ocr_page_paddle_glm(cfg, img_path: Path) -> str:
    """PP-Structure 版面分析 + glm-ocr 文本识别。
    流程：
      1) PaddleOCR 检测版面区域
      2) 丢弃 header/footer/page_number/seal（规则化）；
         footnote 不丢，随正文 OCR 后由 extract_footnotes_from_page 提取为 EPUB 注释
      3) text/title 类: 裁剪后调 glm-ocr
      4) figure 类: 保留为 jpg（image_marker_md，统一资产管线）
      5) table/formula: 保留为 jpg（不强求 OCR 还原复杂排版）
      6) 按阅读顺序拼接（自上而下，自左而右）
      版面分析失败会 raise，由 run_job 兜底标记 OCR-FAILED 供重跑
    """
    if Image is None:
        return NO_TEXT_TOKEN
    try:
        boxes = paddle_layout_detect(img_path)
    except Exception as e:
        raise RuntimeError(f"版面分析失败：{e}") from e
    if not boxes:
        return NO_TEXT_TOKEN

    img = Image.open(img_path).convert("RGB")
    W, H = img.size
    parts = []
    for idx, b in enumerate(boxes):
        label = b.get("label", "")
        try:
            x1, y1, x2, y2 = (int(round(c)) for c in b["bbox"])
        except Exception:
            continue
        x1 = max(0, min(x1, W - 1)); x2 = max(0, min(x2, W))
        y1 = max(0, min(y1, H - 1)); y2 = max(0, min(y2, H))
        if x2 - x1 < 16 or y2 - y1 < 16:
            continue
        if label in PADDLE_DROP_LABELS:
            continue

        crop = img.crop((x1, y1, x2, y2))
        base_stem = f"{img_path.stem}_p{idx}"

        if label in PADDLE_FIGURE_LABELS:
            fig_path = img_path.parent / f"{base_stem}.jpg"
            try:
                crop.save(fig_path, "JPEG", quality=85)
                parts.append((y1, x1, image_marker_md(fig_path, "插图")))
            except Exception:
                pass
            continue
        if label in PADDLE_ASSET_LABELS:
            asset_path = img_path.parent / f"{base_stem}_{label}.jpg"
            try:
                crop.save(asset_path, "JPEG", quality=90)
                parts.append((y1, x1, image_marker_md(asset_path, label)))
            except Exception:
                pass
            continue
        # 文本类（含未知 label）：裁剪后调 glm-ocr
        tmp_path = img_path.parent / f"{base_stem}_crop.jpg"
        try:
            crop.save(tmp_path, "JPEG", quality=92)
            txt = clean_page_text(ocr_page_glmocr(cfg, tmp_path, attempt=1))
            if txt and txt != NO_TEXT_TOKEN:
                parts.append((y1, x1, txt))
        except Exception:
            pass
        finally:
            try: tmp_path.unlink()
            except Exception: pass

    parts.sort(key=lambda p: (p[0], p[1]))
    return "\n\n".join(p[2] for p in parts) if parts else NO_TEXT_TOKEN


def run_mineru(cfg, pdf_path: Path, work_dir: Path):
    """后端 B：调用本机 mineru CLI（Mac 上常用 -b pipeline -d mps）。"""
    out_dir = work_dir / "mineru_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        cfg.get("mineru_bin", "mineru"),
        "-p", str(pdf_path),
        "-o", str(out_dir),
        "-b", cfg.get("mineru_backend", "pipeline"),
        "-d", cfg.get("mineru_device", "mps"),
    ]
    if cfg.get("mineru_lang"):
        cmd += ["-l", cfg["mineru_lang"]]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.get("mineru_timeout", 21600))
    if proc.returncode != 0:
        raise RuntimeError(f"mineru 退出码 {proc.returncode}: {proc.stderr[-800:]}")
    mds = sorted(out_dir.rglob("*.md"))
    if not mds:
        raise RuntimeError("mineru 未产出 Markdown 文件")
    # 取最大的 md（一般就是正文）
    md = max(mds, key=lambda p: p.stat().st_size)
    return md.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# 文本后处理：页眉页脚清理 / 断行合并 / 章节切分
# ---------------------------------------------------------------------------

CJK = r"\u4e00-\u9fff\u3040-\u30ff"


def clean_page_text(text):
    """去掉模型输出的所有代码围栏行（保留内部正文）与多余空行。
    glm-ocr 在裁剪图上常重复输出 ``` 行（无内容），必须去掉。
    """
    text = re.sub(r"```[^\n]*\n?", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("\r\n", "\n")
    return text.strip()


def detect_running_titles(page_texts, min_ratio=0.35, max_len=60):
    """
    找出跨页重复的页眉/页脚：
    某段文本在 >= min_ratio 的页里出现，且出现在页首/页尾，视为页眉页脚。
    """
    heads, tails = {}, {}
    for t in page_texts:
        lines = [l.strip() for l in t.split("\n") if l.strip()]
        if not lines:
            continue
        for cand, bucket in ((lines[0], heads), (lines[-1], tails)):
            if 0 < len(cand) <= max_len:
                bucket[cand] = bucket.get(cand, 0) + 1
    n = max(1, len(page_texts))
    junk = set()
    for bucket in (heads, tails):
        for s, c in bucket.items():
            if c / n >= min_ratio:
                junk.add(s)
    return junk


def strip_junk(text, junk):
    lines = text.split("\n")
    kept = [l for l in lines if l.strip() not in junk]
    return "\n".join(kept)


def merge_wrapped_lines(text):
    """
    合并因排版产生的断行：
    中文行尾若不以句末标点结尾，则把下一行接上来。
    """
    out = []
    buf = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if buf:
                out.append(buf)
                buf = ""
            out.append("")
            continue
        if re.match(r"^[#>\-\*\|`\d]+\s?", line) or re.match(r"^\|", line) or is_chapter_line(line):
            # 标题 / 列表 / 表格 / 引用：自成一段，不与相邻行合并
            if buf:
                out.append(buf)
                buf = ""
            out.append(line)
            continue
        if not buf:
            buf = line
            continue
        prev_end = buf[-1]
        # 上一行以句末标点/引号结尾 → 视为一段结束
        if prev_end in "。！？；：”』」》…!?;:" or prev_end in ".!?;:":
            out.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return "\n".join(out)


CHAPTER_PATTERNS = [
    r"^#+\s+.*$",
    r"^第\s*[0-9一二三四五六七八九十百千零〇]+\s*[章节篇回卷部集]\s*.*$",
    r"^(?:CHAPTER|Chapter)\s+[0-9IVXLC]+\s*.*$",
    r"^[0-9]{1,3}\s*[、.\s]\s*\S.*$",
    r"^序(?:言|章)?|^前言|^后记|^附录|^引子|^楔子|^尾声$",
]


def is_chapter_line(line):
    s = line.strip()
    if not s or len(s) > 60:
        return False
    return any(re.match(p, s) for p in CHAPTER_PATTERNS)


def split_chapters(text):
    """按标题行切成 [(标题, 正文md)]。"""
    lines = text.split("\n")
    chapters = []
    cur_title, cur = None, []
    for line in lines:
        if is_chapter_line(line):
            if cur_title is not None or cur:
                body = "\n".join(cur).strip()
                if body:
                    chapters.append((cur_title or "正文", body))
            cur_title = line.strip().lstrip("#").strip()
            cur = []
        else:
            cur.append(line)
    body = "\n".join(cur).strip()
    if body or not chapters:
        chapters.append((cur_title or "正文", body))
    if len(chapters) == 1 and len(chapters[0][1]) > 30000:
        # 没切出章节：按字数均分成若干卷，避免单文件过大
        txt = chapters[0][1]
        size = 12000
        chunks = [txt[i:i + size] for i in range(0, len(txt), size)]
        chapters = [(f"第 {i + 1} 部分", c) for i, c in enumerate(chunks)]
    return chapters


# ---------------------------------------------------------------------------
# Markdown → XHTML（极简转换器，够 EPUB 用）
# ---------------------------------------------------------------------------

def inline_md(s, note_href_builder=None, note_ref_id_builder=None):
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)

    def repl_note_ref(m):
        nid = m.group(1)
        label = m.group(2)
        href = note_href_builder(nid) if note_href_builder else f"notes.xhtml#note-{nid}"
        ref_id = note_ref_id_builder(nid) if note_ref_id_builder else f"note-ref-{nid}"
        return (
            f'<a id="{html.escape(ref_id, quote=True)}" epub:type="noteref" class="noteref" '
            f'href="{html.escape(href, quote=True)}">{label}</a>'
        )

    s = re.sub(r"\[\[NOTE_REF:(\d+)\|(.+?)\]\]", repl_note_ref, s)
    s = re.sub(r"!\[(.*?)\]\((.*?)\)", r'<img alt="\1" src="\2"/>', s)
    s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
    return s


def md_to_xhtml(md, note_href_builder=None, note_ref_id_builder=None):
    lines = md.split("\n")
    out = []
    in_list = False
    in_quote = False
    para = []

    def flush_para():
        nonlocal para
        if para:
            out.append("<p>" + "<br/>".join(
                inline_md(l, note_href_builder=note_href_builder, note_ref_id_builder=note_ref_id_builder)
                for l in para
            ) + "</p>")
            para = []

    for raw in lines:
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            if in_quote:
                out.append("</blockquote>")
                in_quote = False
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            lvl = min(6, len(m.group(1)))
            out.append(
                f"<h{lvl}>{inline_md(m.group(2), note_href_builder=note_href_builder, note_ref_id_builder=note_ref_id_builder)}</h{lvl}>"
            )
            continue
        if re.match(r"^\|", s):
            flush_para()
            cells = [c.strip() for c in s.strip("|").split("|")]
            out.append("<p>" + " ｜ ".join(
                inline_md(c, note_href_builder=note_href_builder, note_ref_id_builder=note_ref_id_builder)
                for c in cells
            ) + "</p>")
            continue
        if re.match(r"^([-*+]|\d+\.)\s+", s):
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = re.sub(r"^([-*+]|\d+\.)\s+", "", s)
            out.append(f"<li>{inline_md(item, note_href_builder=note_href_builder, note_ref_id_builder=note_ref_id_builder)}</li>")
            continue
        if s.startswith(">"):
            if not in_quote:
                flush_para()
                out.append("<blockquote>")
                in_quote = True
            out.append(
                f"<p>{inline_md(s.lstrip('> '), note_href_builder=note_href_builder, note_ref_id_builder=note_ref_id_builder)}</p>"
            )
            continue
        if re.match(r"^(-{3,}|\*{3,})$", s):
            flush_para()
            out.append("<hr/>")
            continue
        para.append(s)
    flush_para()
    if in_list:
        out.append("</ul>")
    if in_quote:
        out.append("</blockquote>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# EPUB3 打包（不依赖 pandoc / Calibre）
# ---------------------------------------------------------------------------

CSS = """body{font-family:"Songti SC","Noto Serif CJK SC",serif;line-height:1.75;margin:1.2em 1em;}
h1,h2,h3{font-family:"Songti SC","Noto Serif CJK SC",serif;line-height:1.4;margin:1.2em 0 .6em;}
h1{font-size:1.5em;text-align:center;} h2{font-size:1.25em;} h3{font-size:1.1em;}
p{text-indent:2em;margin:.5em 0;text-align:justify;}
blockquote{margin:1em 2em;color:#555;}
hr{border:none;border-top:1px solid #ccc;margin:2em 0;}
code{font-family:ui-monospace,Menlo,monospace;font-size:.9em;}
img{max-width:100%;display:block;margin:1em auto;}
.illustration-page p{text-indent:0;text-align:center;}
.noteref{text-decoration:none;vertical-align:super;font-size:.8em;}
.notes p{text-indent:0;margin:.8em 0;}
.notes a.backref{text-decoration:none;margin-left:.4em;}
"""


def build_epub(epub_path: Path, title, author, chapters, lang="zh-CN", assets_dir: Path | None = None, notes=None):
    """把章节、插图、注释打成 EPUB3。注释附在各章末尾。"""
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    bookid = f"urn:uuid:{uuid.uuid4()}"
    modified = now_iso()
    nav_items, manifest, spine = [], [], []
    notes = notes or []

    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype 必须是第一个条目且不压缩
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                   '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
                   '</container>')
        z.writestr("OEBPS/style.css", CSS)

        if assets_dir and assets_dir.exists():
            for asset in sorted(assets_dir.iterdir()):
                if not asset.is_file():
                    continue
                mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                safe_name = safe_epub_asset_name(asset.name)
                href = html.escape(f"images/{safe_name}", quote=True)
                z.writestr(f"OEBPS/images/{safe_name}", asset.read_bytes())
                asset_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", safe_name)
                manifest.append(f'    <item id="asset-{asset_id}" href="{href}" media-type="{mime}"/>')

        for i, chapter in enumerate(chapters):
            ctitle = chapter["title"]
            body = chapter["body"]
            chapter_notes = chapter.get("notes") or []
            fname = f"chap_{i + 1:04d}.xhtml"
            body_for_render = body if chapter_notes else render_note_refs_as_text(body)
            note_ref_counts = {}
            note_backrefs = {}

            def note_ref_id_builder(nid: str) -> str:
                count = note_ref_counts.get(nid, 0) + 1
                note_ref_counts[nid] = count
                ref_id = f"note-ref-{nid}-{count}"
                note_backrefs.setdefault(int(nid), []).append(ref_id)
                return ref_id

            xhtml = md_to_xhtml(
                body_for_render,
                note_href_builder=(lambda nid: f"#note-{nid}"),
                note_ref_id_builder=note_ref_id_builder,
            )
            note_html = ""
            if chapter_notes:
                note_lines = ['<section class="chapter-notes" epub:type="endnotes">', '<h3>注释</h3>']
                for note in chapter_notes:
                    label = html.escape(note.get("label") or f"[{note['id']}]")
                    text = inline_md(note.get("text", ""))
                    backrefs = note_backrefs.get(note["id"], [])
                    if backrefs:
                        backref_html = " ".join(
                            f'<a class="backref" aria-label="返回正文中的注释引用" href="#{html.escape(ref_id, quote=True)}">'
                            f'返回正文{"" if idx == 1 else idx}</a>'
                            for idx, ref_id in enumerate(backrefs, 1)
                        )
                    else:
                        backref_html = f'<a class="backref" aria-label="返回本章开头" href="#chapter-{i + 1}">返回本章</a>'
                    note_lines.append(
                        f'<p id="note-{note["id"]}" epub:type="endnote"><strong>{label}</strong> {text}'
                        f'{backref_html}</p>'
                    )
                note_lines.append("</section>")
                note_html = "\n".join(note_lines)
            body_class = ' class="illustration-page"' if chapter.get("illustration_only") else ""
            doc = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="%s">\n'
                '<head><meta charset="utf-8"/><title>%s</title>'
                '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
                '<body%s>\n<h2 id="chapter-%d">%s</h2>\n%s\n%s\n</body>\n</html>\n'
            ) % (lang, html.escape(ctitle), body_class, i + 1, html.escape(ctitle), xhtml, note_html)
            z.writestr(f"OEBPS/{fname}", doc)
            manifest.append(f'    <item id="c{i + 1}" href="{fname}" media-type="application/xhtml+xml"/>')
            spine.append(f'    <itemref idref="c{i + 1}"/>')
            nav_items.append((ctitle, fname))

        nav = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<!DOCTYPE html>',
               '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">',
               '<head><meta charset="utf-8"/><title>目录</title></head><body>',
               '<nav epub:type="toc" id="toc"><h1>目录</h1><ol>']
        for t, f in nav_items:
            nav.append(f'  <li><a href="{f}">{html.escape(t)}</a></li>')
        nav += ['</ol></nav>', '</body>', '</html>', '']
        z.writestr("OEBPS/nav.xhtml", "\n".join(nav))

        # ---- EPUB2 兼容：toc.ncx（旧阅读器/Kindle 需要）----
        ncx = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">',
               f'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="{lang}">',
               '  <head>',
               f'    <meta name="dtb:uid" content="{bookid}"/>',
               '    <meta name="dtb:depth" content="1"/>',
               '    <meta name="dtb:totalPageCount" content="0"/>',
               '    <meta name="dtb:maxPageNumber" content="0"/>',
               '  </head>',
               f'  <docTitle><text>{html.escape(title)}</text></docTitle>',
               '  <navMap>']
        for i, (t, f) in enumerate(nav_items):
            ncx.append(f'    <navPoint id="np{i+1}" playOrder="{i+1}">')
            ncx.append(f'      <navLabel><text>{html.escape(t)}</text></navLabel>')
            ncx.append(f'      <content src="{f}"/>')
            ncx.append('    </navPoint>')
        ncx += ['  </navMap>', '</ncx>', '']
        z.writestr("OEBPS/toc.ncx", "\n".join(ncx))

        opf = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<package version="3.0" unique-identifier="bookid" xmlns="http://www.idpf.org/2007/opf">',
               '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
               f'    <dc:identifier id="bookid">{bookid}</dc:identifier>',
               f'    <dc:title>{html.escape(title)}</dc:title>',
               f'    <dc:creator>{html.escape(author or "未知")}</dc:creator>',
               f'    <dc:language>{lang}</dc:language>',
               f'    <meta property="dcterms:modified">{modified}</meta>',
               f'    <meta name="generator" content="{APP_NAME} {VERSION}"/>',
               '  </metadata>',
               '  <manifest>',
               '    <item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>',
               '    <item id="css" href="style.css" media-type="text/css"/>']
        opf += manifest
        opf += ['    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '  </manifest>', '  <spine>']
        opf += spine
        opf += ['  </spine>', '</package>', '']
        z.writestr("OEBPS/content.opf", "\n".join(opf))
    return epub_path


# ---------------------------------------------------------------------------
# 主流水线
# ---------------------------------------------------------------------------

PROOFREAD_PROMPT = """你是古籍/中文图书校对助手。下面是一段 OCR 得到的正文，可能存在错别字、标点错误和错误的断行。
请只做以下修正，不要改写、不要润色、不要增删内容、不要加解释：
1. 修正明显的 OCR 错字（尤其是形近字、繁简混排、标点）；
2. 修正错误的断行，让句子完整；
3. 删除明显无意义的乱码符号、重复词句与重复段落；
4. 保持原文的人名、地名、数字与专业术语不变。
直接输出修正后的正文，不要任何开头说明。

正文：
"""


def run_job(job_id):
    """后台执行一个转换任务。"""
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["started_at"] = time.time()
        cfg = dict(job["config"])
    try:
        log(job_id, "任务开始")
        pdf_path = Path(job["pdf_path"])
        slug = job["slug"]
        book_dir = ROOT / "books" / slug
        pages_dir = book_dir / "pages"
        images_dir = book_dir / "images"
        book_dir.mkdir(parents=True, exist_ok=True)
        pages_dir.mkdir(parents=True, exist_ok=True)

        backend = cfg.get("backend", "glm-ocr")
        page_texts = {}
        all_notes = []
        next_note_id = 1

        # ---- 1. 有文字层且用户选择跳过 OCR ----
        if backend == "text-layer":
            log(job_id, "直接抽取 PDF 文字层（跳过 OCR）")
            texts = extract_text_layer(pdf_path)
            if texts is None:
                raise RuntimeError("无法读取 PDF 文字层，请改用 OCR 后端")
            for i, t in enumerate(texts):
                page_texts[i + 1] = clean_page_text(t)
        elif backend == "mineru":
            log(job_id, "调用本机 mineru CLI（首次运行会下载模型，请耐心）")
            md = run_mineru(cfg, pdf_path, book_dir)
            (book_dir / "book.raw.md").write_text(md, encoding="utf-8")
            page_texts = {1: md}
        else:
            # ---- 2. 渲染页面 → 逐页 OCR ----
            log(job_id, f"渲染 PDF 页面（DPI={cfg.get('dpi', 200)}）")
            paths, total = render_pages(
                pdf_path, images_dir,
                dpi=int(cfg.get("dpi", 200)),
                page_range=cfg.get("page_range"),
            )
            job["total_pages"] = len(paths)
            log(job_id, f"共 {len(paths)} 页待处理（全书 {total} 页）")

            cached = 0
            todo = []
            for pno, p in paths:
                pf = pages_dir / f"{pno:04d}.md"
                try:
                    prev = pf.read_text(encoding="utf-8") if pf.exists() else ""
                except Exception:
                    prev = ""
                # 断点续跑只复用成功页；失败标记页重新 OCR
                if prev and not prev.startswith(OCR_FAIL_TAG) and not cfg.get("force_reocr"):
                    page_texts[pno] = prev
                    cached += 1
                else:
                    todo.append((pno, p))
            if cached:
                log(job_id, f"复用已完成页面 {cached} 页")

            ocr_fn = ocr_page_backend
            # Mac 上 Ollama 并发>1 会触发 llama-server 二次加载超时（HTTP 500），
            # 默认串行最稳；确有富余再手动调高
            concurrency = max(1, min(int(cfg.get("concurrency", 1)), 4))
            done = [cached]
            lock = threading.Lock()
            t0 = time.time()

            def work(item):
                if CANCEL_FLAGS.get(job_id):
                    return
                pno, p = item
                try:
                    txt = ocr_fn(cfg, p)
                    if not txt or is_suspicious_ocr_text(txt):
                        raise RuntimeError("空结果")
                    (pages_dir / f"{pno:04d}.md").write_text(txt, encoding="utf-8")
                    with lock:
                        page_texts[pno] = txt
                except Exception as e:  # noqa
                    log(job_id, f"第 {pno} 页失败：{e}")
                    (pages_dir / f"{pno:04d}.md").write_text(
                        OCR_FAIL_TAG + f"\n{e}", encoding="utf-8")
                with lock:
                    done[0] += 1
                    n = done[0]
                pct = 5 + int(75 * n / max(1, len(paths)))
                with JOBS_LOCK:
                    job["progress"] = pct
                elapsed = time.time() - t0
                speed = elapsed / max(1, n - cached) if n > cached else 0
                eta = speed * (len(paths) - n)
                if n % 1 == 0:
                    log(job_id, f"已完成 {n}/{len(paths)} 页" + (f"，预计剩余 {int(eta)} 秒" if eta > 1 else ""))

            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                list(ex.map(work, todo))

            # ---- 记录每页耗时到历史统计（供下次全书时长估算）----
            if todo:
                ocr_elapsed = time.time() - t0
                record_pages_sec(stats_key(cfg), len(todo), ocr_elapsed * concurrency)
                log(job_id, f"本批 OCR 速度 {ocr_elapsed / len(todo):.1f} 秒/页"
                            f"（并发 {concurrency}），已计入历史统计")

            if CANCEL_FLAGS.get(job_id):
                raise RuntimeError("已取消")

        # ---- 3. 后处理 ----
        log(job_id, "清理页眉页脚、合并断行与注释")
        ordered_keys = sorted(page_texts.keys())
        ordered = [page_texts[k] for k in ordered_keys]
        if backend not in ("mineru",):
            junk = detect_running_titles(ordered)
            if junk:
                log(job_id, f"识别到 {len(junk)} 条页眉/页脚，已移除")
            ordered = [strip_junk(t, junk) for t in ordered]
        processed = []
        recent_page_norms = []
        for pno, text in zip(ordered_keys, ordered):
            if text.startswith("!["):
                processed.append({"page": pno, "text": text, "illustration": True})
                continue
            body_text, notes, next_note_id = extract_footnotes_from_page(text, pno, next_note_id)
            body_text = strip_noise_lines(body_text)
            body_text = collapse_repeated_paragraphs(merge_wrapped_lines(body_text))
            if looks_like_duplicate_page(body_text, recent_page_norms):
                log(job_id, f"第 {pno} 页正文与前页重复，已剔除")
                continue
            if body_text:
                processed.append({"page": pno, "text": body_text, "illustration": False})
                recent_page_norms.append(normalized_text_for_dedup(body_text))
            if notes:
                all_notes.extend(notes)
        full = "\n\n".join(item["text"] for item in processed if item["text"])
        full = collapse_repeated_paragraphs(full)
        notes_md = ""
        if all_notes:
            note_lines = ["# 注释"]
            for note in all_notes:
                note_lines.append(f"{note['label']} {note['text']}".strip())
            notes_md = "\n\n".join(note_lines)
        book_md = render_note_refs_as_text(full)
        if notes_md:
            book_md = book_md.rstrip() + "\n\n" + notes_md + "\n"
        (book_dir / "book.md").write_text(book_md, encoding="utf-8")

        # ---- 4. 可选：用已装的文本模型做校对 ----
        if cfg.get("proofread"):
            model = (cfg.get("proof_model") or "").strip() or "qwen14b-pro"
            cfg["proof_model"] = model
            if not model:
                log(job_id, "未填写校对模型，跳过校对")
            else:
                log(job_id, f"使用 {model} 逐段校对（较慢，可随时取消）")
                chunks = re.split(r"\n(?=#{1,6}\s|第[一二三四五六七八九十百千0-9]+[章节])", full)
                chunks = [c for c in chunks if c.strip()]
                out = []
                for i, c in enumerate(chunks):
                    if CANCEL_FLAGS.get(job_id):
                        raise RuntimeError("已取消")
                    if len(c) > 6000:
                        out.append(c)
                        continue
                    try:
                        r = ollama_chat(
                            cfg.get("ollama_url", "http://localhost:11434"),
                            model,
                            PROOFREAD_PROMPT + c,
                            options={"temperature": 0.1},
                            timeout=int(cfg.get("proof_timeout", 900)),
                        )
                        out.append(r or c)
                    except Exception as e:  # noqa
                        log(job_id, f"第 {i + 1} 段校对失败，保留原文：{e}")
                        out.append(c)
                    with JOBS_LOCK:
                        job["progress"] = 80 + int(15 * (i + 1) / len(chunks))
                full = "\n\n".join(out)
                full = collapse_repeated_paragraphs(full)
                proofread_md = render_note_refs_as_text(full)
                if notes_md:
                    proofread_md = proofread_md.rstrip() + "\n\n" + notes_md + "\n"
                (book_dir / "book.proofread.md").write_text(proofread_md, encoding="utf-8")
                book_md = proofread_md

        # ---- 5. 切章 + 打包 EPUB ----
        log(job_id, "切分章节并生成 EPUB")
        raw_chapters = split_chapters(full)
        chapters = []
        implicit_single = len(raw_chapters) == 1 and (raw_chapters[0][0] or "").strip() == "正文"
        if implicit_single:
            body0 = raw_chapters[0][1]
            chapter_notes = collect_note_refs_for_epub(body0, all_notes)
            chapters = [{
                "title": "正文",
                "body": body0,
                "notes": chapter_notes,
                "illustration_only": bool(re.fullmatch(r"\s*!\[.*?\]\(images/.*?\)\s*", body0 or "")),
            }]
        else:
            for title0, body0 in raw_chapters:
                chapter_notes = collect_note_refs_for_epub(body0, all_notes)
                chapters.append({
                    "title": title0,
                    "body": body0,
                    "notes": chapter_notes,
                    "illustration_only": bool(re.fullmatch(r"\s*!\[.*?\]\(images/.*?\)\s*", body0 or "")),
                })
        # 测试版单独命名，不覆盖全书版 EPUB
        epub_name = f"{slug}-试读版.epub" if cfg.get("mode") == "test" else f"{slug}.epub"
        epub = book_dir / epub_name
        build_epub(epub, job["title"], job.get("author", ""), chapters,
                   lang=cfg.get("lang", "zh-CN"), assets_dir=images_dir)
        md_out = book_dir / f"{slug}.md"
        md_src = book_dir / ("book.proofread.md" if cfg.get("proofread") and (book_dir / "book.proofread.md").exists() else "book.md")
        if md_src != md_out:
            shutil.copyfile(md_src, md_out)

        with JOBS_LOCK:
            job["status"] = "done"
            job["progress"] = 100
            job["finished_at"] = time.time()
            job["epub_path"] = str(epub)
            job["epub_size"] = epub.stat().st_size
            job["chapters"] = len(chapters)
            job["message"] = f"完成：{len(chapters)} 章 / {epub.stat().st_size // 1024} KB"
        log(job_id, f"完成，EPUB 已保存到 {epub}")
    except Exception as e:  # noqa
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = str(e)
            job["message"] = f"失败：{e}"
        log(job_id, f"失败：{e}")
    finally:
        CANCEL_FLAGS.pop(job_id, None)
        save_jobs()


def worker_loop():
    while True:
        job_id = TASK_QUEUE.get()
        try:
            run_job(job_id)
        finally:
            TASK_QUEUE.task_done()


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def job_public(jid):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if not j:
            return None
        d = {k: v for k, v in j.items() if k != "_future"}
        d["id"] = jid
        if d.get("epub_path") and Path(d["epub_path"]).exists():
            d["download_url"] = f"/api/download/{jid}"
            d["reveal_url"] = f"/api/reveal/{jid}"
        return d


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---- helpers ----
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, download_name=None, mime=None):
        if not path.exists():
            self.send_error(404)
            return
        mime = mime or ("application/epub+zip" if path.suffix == ".epub" else "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            # RFC 5987：中文文件名必须用 filename*=UTF-8''<percent-encoded>，
            # 直接拼中文会让 http.server 的 latin-1 编码崩溃。
            from urllib.parse import quote
            ascii_fallback = re.sub(r"[^A-Za-z0-9._\-]", "_", download_name) or "download"
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{ascii_fallback}\"; "
                             f"filename*=UTF-8''{quote(download_name)}")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- routes ----
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path

        # 免认证：首页 / 健康检查；其余接口需要登录 token
        if p not in ("/", "/index.html", "/api/health"):
            if not token_ok(self):
                self.send_json({"error": "unauthorized"}, 401)
                return

        if p in ("/", "/index.html"):
            idx = Path(__file__).parent / "web" / "index.html"
            self.send_file(idx, mime="text/html; charset=utf-8")
            return

        if p == "/api/health":
            self.send_json({"ok": True, "root": str(ROOT), "version": VERSION})
            return

        if p == "/api/jobs":
            items = [job_public(j) for j in JOBS.keys()]
            items.sort(key=lambda d: d.get("created_at", 0), reverse=True)
            self.send_json({"jobs": items})
            return

        m = re.match(r"^/api/job/([a-f0-9\-]+)$", p)
        if m:
            j = job_public(m.group(1))
            if not j:
                self.send_json({"error": "not found"}, 404)
                return
            j["log"] = LOG_BUFFERS.get(m.group(1), [])[-120:]
            self.send_json(j)
            return

        m = re.match(r"^/api/download/([a-f0-9\-]+)$", p)
        if m:
            j = JOBS.get(m.group(1))
            if not j or not j.get("epub_path"):
                self.send_json({"error": "not ready"}, 404)
                return
            ep = Path(j["epub_path"])
            self.send_file(ep, download_name=f"{j.get('title', 'book')}.epub")
            return

        m = re.match(r"^/api/download-md/([a-f0-9\-]+)$", p)
        if m:
            j = JOBS.get(m.group(1))
            if not j:
                self.send_json({"error": "not found"}, 404)
                return
            md = Path(ROOT) / "books" / j["slug"] / "book.md"
            self.send_file(md, download_name=f"{j.get('title', 'book')}.md", mime="text/markdown; charset=utf-8")
            return

        m = re.match(r"^/api/reveal/([a-f0-9\-]+)$", p)
        if m:
            j = JOBS.get(m.group(1))
            if not j or not j.get("epub_path"):
                self.send_json({"error": "not ready"}, 404)
                return
            target = Path(j["epub_path"])
            try:
                if sys.platform == "darwin":
                    subprocess.run(["open", "-R", str(target)], check=False)
                elif sys.platform == "win32":
                    subprocess.run(["explorer", "/select,", str(target)], check=False)
                else:
                    subprocess.run(["xdg-open", str(target.parent)], check=False)
                self.send_json({"ok": True})
            except Exception as e:  # noqa
                self.send_json({"error": str(e)}, 500)
            return

        m = re.match(r"^/api/open-folder$", p)
        if m:
            try:
                if sys.platform == "darwin":
                    subprocess.run(["open", str(ROOT / "books")], check=False)
                elif sys.platform == "win32":
                    subprocess.run(["explorer", str(ROOT / "books")], check=False)
                else:
                    subprocess.run(["xdg-open", str(ROOT / "books")], check=False)
                self.send_json({"ok": True})
            except Exception as e:  # noqa
                self.send_json({"error": str(e)}, 500)
            return

        m = re.match(r"^/api/models$", p)
        if m:
            base = self.server.ollama_url
            try:
                with urlopen(base.rstrip("/") + "/api/tags", timeout=5) as r:
                    data = json.loads(r.read().decode("utf-8"))
                names = [m_.get("name") for m_ in data.get("models", [])]
                self.send_json({"ok": True, "models": names, "base": base})
            except Exception as e:  # noqa
                self.send_json({"ok": False, "error": str(e), "models": [], "base": base})
            return

        m = re.match(r"^/api/model-state$", p)
        if m:
            base = self.server.ollama_url
            loaded = ollama_loaded_models(base)
            self.send_json({"ok": True, "loaded": loaded,
                            "idle_minutes": IDLE_MINUTES})
            return

        m = re.match(r"^/api/stats$", p)
        if m:
            self.send_json({"ok": True, "stats": load_stats()})
            return

        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path

        # 登录接口免认证；其余 POST 需要登录 token
        if p != "/api/login":
            if not token_ok(self):
                self.send_json({"error": "unauthorized"}, 401)
                return

        if p == "/api/login":
            try:
                body = json.loads(self.read_body().decode("utf-8"))
            except Exception:
                body = {}
            if body.get("password") == PASSWORD[0]:
                tok = new_token()
                LAST_ACTIVITY[0] = time.time()
                # 登录即预启动大模型（异步预热，不阻塞登录响应）
                threading.Thread(target=ollama_preload,
                                 args=(self.server.ollama_url, "glm-ocr"),
                                 daemon=True).start()
                print(f"[auth] 登录成功，开始预加载 glm-ocr", flush=True)
                self.send_json({"ok": True, "token": tok})
            else:
                time.sleep(0.5)  # 减缓暴力尝试
                self.send_json({"ok": False, "error": "密码错误"}, 401)
            return

        if p == "/api/upload":
            ctype = self.headers.get("Content-Type", "")
            title = unquote(self.headers.get("X-Title", "") or "")
            author = unquote(self.headers.get("X-Author", "") or "")
            try:
                cfg_raw = unquote(self.headers.get("X-Config", "{}") or "{}")
                try:
                    cfg = json.loads(cfg_raw)
                except Exception:
                    cfg = json.loads(base64.b64decode(cfg_raw))
            except Exception:
                cfg = {}
            cfg.setdefault("backend", "paddle-layout")
            if cfg.get("proofread") and not (cfg.get("proof_model") or "").strip():
                cfg["proof_model"] = "qwen14b-pro"
            job_id = str(uuid.uuid4())

            fname = None
            pdf_path = ROOT / "uploads" / f"{job_id[:8]}_book.pdf"
            if "multipart/form-data" in ctype:
                # 极简 multipart 解析：只取第一个文件部分（一般较小，整体读入）
                raw = self.read_body()
                marker = ctype.split("boundary=")[-1].encode()
                parts = raw.split(b"--" + marker)
                filedata = None
                for part in parts:
                    if b'name="file"' not in part and b'name="pdf"' not in part:
                        continue
                    head, _, body = part.partition(b"\r\n\r\n")
                    mm = re.search(rb'filename="([^"]*)"', head)
                    if mm:
                        fname = mm.group(1).decode("utf-8", "ignore")
                    filedata = body.rstrip(b"\r\n-")
                if filedata is None:
                    self.send_json({"error": "未收到文件"}, 400)
                    return
                pdf_path.write_bytes(filedata)
            else:
                # 非 multipart（网页端默认方式）：流式落盘，不限文件大小
                fname = unquote(self.headers.get("X-Filename", "book.pdf"))
                remaining = int(self.headers.get("Content-Length") or 0)
                if remaining <= 0:
                    self.send_json({"error": "空文件"}, 400)
                    return
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                with open(pdf_path, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                if remaining > 0:
                    self.send_json({"error": "上传中断"}, 400)
                    return
                # 同步收尾：部分客户端会补 CRLF
                cl = int(self.headers.get("Content-Length") or 0)
                got = pdf_path.stat().st_size
                if got > cl >= 0:
                    with open(pdf_path, "r+b") as f:
                        f.truncate(cl)

            fname = os.path.basename(fname or "book.pdf")
            if not fname.lower().endswith(".pdf"):
                fname += ".pdf"
            base_title = title or Path(fname).stem

            total_pages = pdf_page_count(pdf_path)
            # ---- 转换模式 ----
            mode = cfg.get("mode") or ("test" if cfg.get("page_range") else "full")
            display_title = base_title
            if mode == "test":
                # 模式①：测试版 —— 只跑前 10 页（页数不足则全跑）
                cfg["page_range"] = [1, min(10, total_pages or 10)]
                if not base_title.endswith("（试读版）"):
                    display_title = base_title + "（试读版）"
            elif mode == "full":
                # 模式②：全书 —— 忽略页面范围
                cfg.pop("page_range", None)

            # ---- 全书耗时估算（基于历史每页速度统计）----
            estimate_str = None
            if mode == "full" and total_pages:
                est = estimate_seconds(total_pages, cfg)
                if est:
                    estimate_str = f"预计耗时 {fmt_minutes(est)}（基于历史 {load_stats().get(stats_key(cfg), {}).get('pages', '?')} 页经验）"
                elif cfg.get("backend") == "glm-ocr":
                    est_lo = total_pages * 10 / 60 / max(1, int(cfg.get("concurrency", 1) or 1))
                    est_hi = total_pages * 40 / 60 / max(1, int(cfg.get("concurrency", 1) or 1))
                    estimate_str = f"尚无历史速度，参考区间 {int(est_lo)}–{int(est_hi)} 分钟"
            cfg["mode"] = mode

            safe = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", fname)
            if pdf_path.name == f"{job_id[:8]}_book.pdf":
                pdf_path = pdf_path.with_name(f"{job_id[:8]}_{safe}")
                os.replace(ROOT / "uploads" / f"{job_id[:8]}_book.pdf", pdf_path)
            # slug 基于原始书名（不含试读版后缀）：先试读再跑全书时共享分页缓存
            slug = slugify(base_title)
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "status": "queued", "progress": 0, "title": display_title,
                    "author": author, "slug": slug, "pdf_path": str(pdf_path),
                    "filename": fname, "config": cfg,
                    "created_at": time.time(), "message": "排队中",
                    "total_pages": total_pages, "mode": mode,
                    "estimate": estimate_str,
                }
            TASK_QUEUE.put(job_id)
            save_jobs()
            self.send_json({"ok": True, "job": job_public(job_id)})
            return

        m = re.match(r"^/api/cancel/([a-f0-9\-]+)$", p)
        if m:
            CANCEL_FLAGS[m.group(1)] = True
            with JOBS_LOCK:
                j = JOBS.get(m.group(1))
                if j:
                    j["message"] = "正在取消…"
            self.send_json({"ok": True})
            return

        m = re.match(r"^/api/delete/([a-f0-9\-]+)$", p)
        if m:
            jid = m.group(1)
            with JOBS_LOCK:
                j = JOBS.pop(jid, None)
            LOG_BUFFERS.pop(jid, None)
            if j:
                try:
                    Path(j["pdf_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    shutil.rmtree(ROOT / "books" / j["slug"], ignore_errors=True)
                except Exception:
                    pass
            save_jobs()
            self.send_json({"ok": True})
            return

        m = re.match(r"^/api/retry/([a-f0-9\-]+)$", p)
        if m:
            jid = m.group(1)
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if not j:
                    self.send_json({"error": "not found"}, 404)
                    return
                if j["status"] == "running":
                    self.send_json({"error": "任务正在运行"}, 400)
                    return
                j.update({"status": "queued", "progress": 0, "message": "重新排队"})
                cfg = dict(j.get("config", {}))
            TASK_QUEUE.put(jid)
            save_jobs()
            self.send_json({"ok": True})
            return

        self.send_error(404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    ollama_url = "http://localhost:11434"


def main():
    global ROOT, JOBS_FILE, AUTH_FILE, STATS_FILE, IDLE_MINUTES
    ap = argparse.ArgumentParser(description=f"{APP_NAME} - 本地扫描书转 EPUB 工作台")
    ap.add_argument("--root", default=str(Path.home() / "ScanLibrary"), help="数据根目录（上传/成书/索引）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--idle-minutes", type=int, default=30,
                    help="空闲多少分钟且无任务时卸载大模型并退出登录（默认 30）")
    ap.add_argument("--password", default=None, help="登录密码（默认随机生成并保存到 data/auth.json）")
    args = ap.parse_args()

    ROOT = Path(args.root).expanduser().resolve()
    ensure_dirs()
    JOBS_FILE = ROOT / "data" / "jobs.json"
    AUTH_FILE = ROOT / "data" / "auth.json"
    STATS_FILE = ROOT / "data" / "stats.json"
    IDLE_MINUTES = max(1, args.idle_minutes)
    PASSWORD[0] = args.password or load_or_create_password()
    load_jobs()

    Server.ollama_url = args.ollama
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=idle_watchdog, daemon=True).start()

    url = f"http://{args.host}:{args.port}"
    print(f"""
  {APP_NAME} v{VERSION}
  ────────────────────────────────────────
  页面地址 : {url}
  登录密码 : {('*' * 4) if args.password else '见 ' + str(AUTH_FILE)}
  数据目录 : {ROOT}
    ├─ uploads/  上传的 PDF
    ├─ books/    成书目录（EPUB / Markdown / 分页结果）
    └─ data/     任务索引 / 密码 / 历史速度统计
  Ollama   : {args.ollama}
  空闲保护 : {IDLE_MINUTES} 分钟无活动且无任务时自动卸载模型并退出登录
  ────────────────────────────────────────
  按 Ctrl+C 停止
""")
    if args.open:
        threading.Timer(1.0, lambda: subprocess.run(
            ["open", url] if sys.platform == "darwin" else ["xdg-open", url], check=False)).start()

    srv = Server((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
