#!/usr/bin/env python3
"""独立测试脚本：MD 多级清洗 + 本地千问精洗。

架构（多级过滤）:
    1) 字数异常拦截    —— 空白页标记 / 复读机页识别
    2) 规则强力去重    —— 围栏、LaTeX 圈码、段内复读、段落级去重、跨页页眉
    3) 文本分块        —— 按段落/句子边界智能切块, 防上下文溢出
    4) 本地千问精洗    —— 强约束 prompt + 质量护栏 + 失败重试/回退

用法:
    # 逐页清洗
    python3 tools/clean_md_test.py --in test_md_input --out test_md_output

    # 合并成一本
    python3 tools/clean_md_test.py --in test_md_input --out /tmp/book.md

    # 跳过千问（纯规则模式, 秒级）
    python3 tools/clean_md_test.py --in test_md_input --out test_md_output --no-qwen
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import statistics
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ================= 配置区域 =================
OLLAMA_URL = "http://localhost:11434/api/chat"   # Ollama chat 端点
MODEL_NAME = "qwen14b-pro"                       # 本地实际模型（qwen14b-pro:latest）
CHUNK_SIZE = 800                                 # 千问单次处理上限（字符）
CHUNK_OVERLAP = 0                                 # 块间重叠（按句子边界切, 不需要重叠）
QWEN_TIMEOUT = 900                                # 单块超时（秒）—— 14B 模型很慢, 勿设 30
SIMILARITY_THRESHOLD = 0.90                       # 段落去重阈值（0.75 会误删正文）
HEADER_SIM_THRESHOLD = 0.92                       # 跨页页眉判定阈值
MIN_ACCEPT_RATIO = 0.60                           # 千问输出/输入长度比下限
MAX_ACCEPT_RATIO = 1.60                           # 千问输出/输入长度比上限
MIN_ACCEPT_SIM = 0.55                             # 千问输出与输入相似度下限（低于判为改写）
# ============================================


# ---------------------------------------------------------------------------
# 阶段 1: 规则化清理（不依赖任何模型）
# ---------------------------------------------------------------------------

CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")
# LaTeX 圈码: $\textcircled{1}$ / $\textcircled {1}$ / 无 $ 包裹
LATEX_CIRCLED_RE = re.compile(r"\$?\s*\\textcircled\s*\{\s*(\d{1,2})\s*\}\s*\$?")
# LaTeX 其他包装: $...$ 内只含 \命令 与数字/空格 → 视为 OCR 噪声
LATEX_ORPHAN_RE = re.compile(r"\$\s*\\[a-zA-Z]+\s*\{?\d*\}?\s*\$")
# 配对围栏 ```lang ... ```
FENCE_PAIR_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*$\n[\s\S]*?^\s*```\s*$", re.MULTILINE)
# 孤立围栏行（实测 book.md 有 50+ 处）
FENCE_BARE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*$", re.MULTILINE)
# 同行连续相同标点: 。。。 → 。   ，，， → ，
REPEAT_PUNCT_RE = re.compile(r"([。，、；：！？…．,.;:!?])\1{1,}")
# 段内复读机: 重复片段 8~400 字符, 重复 2 次以上
INLINE_DUP_RE = re.compile(r"(.{8,400}?)(?:\s*\1){2,}", re.DOTALL)
# 短 token 复读: 同一词反复 4 次以上（LLM 死循环特征）
TOKEN_DUP_RE = re.compile(r"\b([A-Za-z\u4e00-\u9fff]{2,})\b(?:\s*\1\b){3,}")
# 残缺行: 以不可能合法结尾的标点结束（复读被截断的残片, 如 "塞缪尔·"）
BROKEN_LINE_RE = re.compile(r"[·、，：；/\\—–－]\s*$")
# 孤立虚词行: 「的/和/与/或/了/着/吗/呢/而」独占一行（断行残片）
DANGLING_WORD_RE = re.compile(r"^[\u7684\u548c\u4e0e\u6216\u4e86\u7740\u5417\u5462\u800c]$")

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def unicode_circled(n: int) -> str:
    return CIRCLED[n - 1] if 1 <= n <= 20 else f"({n})"


def normalize_text(text: str) -> str:
    """基础规范化: BOM/控制字符/行内多余空白."""
    text = text.lstrip("\ufeff")
    text = CTRL_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text


def strip_fences(text: str) -> tuple[str, int]:
    """删除配对围栏与孤立围栏行."""
    paired = len(FENCE_PAIR_RE.findall(text))
    text = FENCE_PAIR_RE.sub("", text)
    bare = len(FENCE_BARE_RE.findall(text))
    text = FENCE_BARE_RE.sub("", text)
    return text, paired + bare


def fix_latex(text: str) -> tuple[str, int]:
    """LaTeX 圈码 → Unicode 圈码; 清除孤立 LaTeX 片段."""
    n = [0]

    def _sub(m: re.Match) -> str:
        n[0] += 1
        return unicode_circled(int(m.group(1)))

    text = LATEX_CIRCLED_RE.sub(_sub, text)
    text = LATEX_ORPHAN_RE.sub("", text)
    return text, n[0]


def collapse_punct(text: str) -> tuple[str, int]:
    """折叠连续相同标点（只折叠相同的, 保留 ？！ 这类合法组合）."""
    cnt = [0]

    def _sub(m: re.Match) -> str:
        cnt[0] += 1
        return m.group(1)

    text = REPEAT_PUNCT_RE.sub(_sub, text)
    return text, cnt[0]


def collapse_inline_repeats(text: str) -> tuple[str, int]:
    """段内复读机折叠."""
    cnt = [0]

    def _sub(m: re.Match) -> str:
        cnt[0] += 1
        return m.group(1)

    text = TOKEN_DUP_RE.sub(lambda m: m.group(1), text)
    text = INLINE_DUP_RE.sub(_sub, text)
    return text, cnt[0]


def strip_broken_lines(text: str) -> tuple[str, int]:
    """删除断行残片行: 以 ·、，：； 等不可能合法结尾的标点结束的行, 以及孤立虚词行."""
    kept, removed = [], 0
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            kept.append(raw)
            continue
        # 纯标点/超短且以悬挂标点结尾 → 残片
        if BROKEN_LINE_RE.search(line) and len(line) <= 30:
            removed += 1
            continue
        if DANGLING_WORD_RE.match(line):
            removed += 1
            continue
        kept.append(raw)
    return "\n".join(kept), removed


def clean_by_rules(text: str) -> tuple[str, dict]:
    """规则清洗主链（用户脚本 clean_by_rules 的强化版）."""
    st = {}
    text = normalize_text(text)
    text, st["fence"] = strip_fences(text)
    text, st["latex"] = fix_latex(text)
    text, st["punct"] = collapse_punct(text)
    text, st["inline_dup"] = collapse_inline_repeats(text)
    text, st["broken"] = strip_broken_lines(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), st


def norm_for_dedup(s: str) -> str:
    """归一化用于去重比对: 只保留汉字/字母/数字."""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "")


def deduplicate_paragraphs(paragraphs: list[str], threshold: float = SIMILARITY_THRESHOLD,
                           window: int = 8, min_len: int = 20,
                           exact_min: int = 6) -> tuple[list[str], int]:
    """段落级去重（滑动窗口）.

    相比用户版:
      - 阈值 0.90（0.75 会误删同话题正文）
      - 窗口 8 段（只比前 2 段检测不到跨页重复）
      - 包含式判重门槛降至 20 字（40 会漏掉书名页这类短段包含）
      - 极短段(<exact_min)只做精确匹配
    """
    kept: list[str] = []
    kept_norms: list[str] = []
    removed = 0
    for raw in paragraphs:
        p = raw.strip()
        if not p:
            continue
        norm = norm_for_dedup(p)

        dup = False
        if not norm:
            dup = True
        elif len(norm) < exact_min:
            # 极短段: 仅当纯数字/罗马数字（页码）或与前面完全重复才删, 保护 "序"/"一" 这类短标题
            if norm in kept_norms[-window:] or re.fullmatch(r"[0-9ivxlcdmIVXLCDM]+", p):
                dup = True
        elif norm in kept_norms[-window:]:
            dup = True
        else:
            for prev_norm in kept_norms[-window:]:
                if not prev_norm:
                    continue
                if prev_norm == norm:
                    dup = True
                    break
                shorter = min(len(prev_norm), len(norm))
                if shorter >= min_len and (prev_norm in norm or norm in prev_norm):
                    dup = True
                    break
                if difflib.SequenceMatcher(None, prev_norm, norm).ratio() >= threshold:
                    dup = True
                    break
        if dup:
            removed += 1
            continue
        kept.append(p)
        kept_norms.append(norm)
    return kept, removed


def strip_repeated_headers(pages: list[str], sim_threshold: float = HEADER_SIM_THRESHOLD,
                           max_lines: int = 3) -> tuple[list[str], int]:
    """删除跨页重复页眉/页脚（取每页首/尾最多 max_lines 行, 与最近 3 页比对）."""
    seen: list[tuple[str, str]] = []   # (head_norm, tail_norm)
    out: list[str] = []
    removed = 0

    for page in pages:
        lines = page.split("\n")
        nonempty = [i for i, l in enumerate(lines) if l.strip()]
        if len(nonempty) < 2 * max_lines + 1:
            out.append(page)
            seen.append(("", ""))
            continue

        for n in range(max_lines, 0, -1):
            head = "\n".join(lines[i] for i in nonempty[:n])
            tail = "\n".join(lines[i] for i in nonempty[-n:])
            head_n = norm_for_dedup(head)
            tail_n = norm_for_dedup(tail)
            if len(head_n) < 8 and len(tail_n) < 8:
                continue

            hit = False
            for prev_head, prev_tail in seen[-3:]:
                if len(head_n) >= 8 and prev_head and \
                        difflib.SequenceMatcher(None, prev_head, head_n).ratio() >= sim_threshold:
                    for i in reversed(nonempty[:n]):
                        lines[i] = None  # type: ignore[call-overload]
                    removed += n
                    hit = True
                if len(tail_n) >= 8 and prev_tail and \
                        difflib.SequenceMatcher(None, prev_tail, tail_n).ratio() >= sim_threshold:
                    for i in reversed(nonempty[-n:]):
                        lines[i] = None  # type: ignore[call-overload]
                    removed += n
                    hit = True
            if hit:
                page = "\n".join(l for l in lines if l is not None)
                nonempty = [i for i, l in enumerate(page.split("\n")) if l.strip()]
                lines = page.split("\n")
                break

        out.append(re.sub(r"\n{3,}", "\n\n", page).strip())
        p_lines = [i for i, l in enumerate(page.split("\n")) if l.strip()]
        seen.append((
            norm_for_dedup("\n".join(page.split("\n")[i] for i in p_lines[:2])),
            norm_for_dedup("\n".join(page.split("\n")[i] for i in p_lines[-2:])),
        ))
    return out, removed


# ---------------------------------------------------------------------------
# 阶段 1.5: 字数异常拦截（复读机页识别）
# ---------------------------------------------------------------------------

def is_blank_page(text: str, min_chars: int = 50) -> bool:
    return len((text or "").strip()) < min_chars


def is_repeater(text: str, median_len: float, factor: float = 2.0) -> tuple[bool, str]:
    """复读机判定: 字数爆炸 且 行重复率高.

    仅靠字数会误伤正常长页, 因此结合"唯一行占比"与"归一化后唯一字符占比".
    """
    n = len(text or "")
    if median_len <= 0 or n <= max(median_len * factor, 1200):
        return False, "length_ok"
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    if len(lines) >= 4:
        uniq_ratio = len(set(lines)) / len(lines)
        if uniq_ratio < 0.6:
            return True, f"line_repeat(uniq={uniq_ratio:.2f})"
    norm = norm_for_dedup(text)
    if norm:
        char_ratio = len(set(norm)) / len(norm)
        if char_ratio < 0.06:
            return True, f"char_repeat(uniq={char_ratio:.3f})"
    return False, "length_warn"


# ---------------------------------------------------------------------------
# 阶段 2: 智能分块（按段落/句子边界, 不腰斩句子）
# ---------------------------------------------------------------------------

SENTENCE_END_RE = re.compile(r"(?<=[。！？；.!?;])\s*")


def smart_chunk(text: str, max_chars: int = CHUNK_SIZE) -> list[str]:
    """按段落边界切块; 超长段落再按句子边界切; 绝不硬切字符."""
    chunks: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            if len(buf) + len(para) + 2 > max_chars:
                flush()
            buf = (buf + "\n\n" + para) if buf else para
            continue
        # 超长段落: 按句子切
        flush()
        for sent in SENTENCE_END_RE.split(para):
            if not sent:
                continue
            if len(sent) > max_chars:
                # 罕见: 单句超长, 按逗号/空格再切
                for piece in re.split(r"(?<=[，,、])\s*", sent):
                    if len(buf) + len(piece) > max_chars:
                        flush()
                    buf = (buf + piece) if buf else piece
                flush()
                continue
            if len(buf) + len(sent) > max_chars:
                flush()
            buf = (buf + sent) if buf else sent
        flush()
    flush()
    return chunks


# ---------------------------------------------------------------------------
# 阶段 3: 本地千问精洗（质量护栏 + 重试 + 回退）
# ---------------------------------------------------------------------------

QWEN_SYSTEM = (
    "你是一个专业的文本校对助手。请去除以下OCR文本中的多余符号、乱码，并修正错别字。"
    "要求：1. 必须保留原文的所有含义和正文；2. 严禁自行缩写、总结或省略正文；"
    "3. 保持原有段落结构与换行；"
    "4. 直接输出修正后的文本，不要输出“好的”、“这是修正后的文本”等任何废话。"
)

PREFIX_JUNK_RE = re.compile(
    r"^\s*(好的|以下是|这是)?[^\n]{0,30}(修正后|校对后|清理后|结果)[^\n]{0,10}[:：]\s*\n?"
)


def call_local_qwen(text_chunk: str, model: str = MODEL_NAME, url: str = OLLAMA_URL,
                    timeout: int = QWEN_TIMEOUT) -> str | None:
    """调用本地千问（urllib, 零外部依赖）."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": QWEN_SYSTEM},
            {"role": "user", "content": text_chunk},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ((data.get("message") or {}).get("content") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        print(f"    [千问失败] {type(e).__name__}: {e}", file=sys.stderr)
        return None


def accept_qwen_output(src: str, out: str) -> tuple[bool, str]:
    """质量护栏: 防止 14B 模型改写/省略/发散."""
    if not out or len(out) < 10:
        return False, "too_short"
    ratio = len(out) / max(1, len(src))
    if ratio < MIN_ACCEPT_RATIO:
        return False, f"over_omitted({ratio:.2f})"
    if ratio > MAX_ACCEPT_RATIO:
        return False, f"over_expanded({ratio:.2f})"
    sim = difflib.SequenceMatcher(None, src, out).ratio()
    if sim < MIN_ACCEPT_SIM:
        return False, f"rewritten(sim={sim:.2f})"
    return True, f"ok(ratio={ratio:.2f},sim={sim:.2f})"


def qwen_clean_chunk(chunk: str, model: str, url: str, timeout: int,
                     retries: int = 1) -> tuple[str, str]:
    """清洗单块, 带护栏与重试. 返回 (文本, 状态说明)."""
    for attempt in range(retries + 1):
        out = call_local_qwen(chunk, model, url, timeout=timeout)
        if out is None:
            continue
        out = PREFIX_JUNK_RE.sub("", out).strip()
        ok, why = accept_qwen_output(chunk, out)
        if ok:
            return out, "qwen_" + why
        print(f"    [护栏拦截] {why}, 第 {attempt + 1} 次尝试", file=sys.stderr)
    # 全部失败 → 回退规则结果
    return chunk, "fallback_rule_only"


def process_text_pipeline(text: str, model: str, url: str, timeout: int,
                          use_qwen: bool = True, heavy_clean: bool = False
                          ) -> tuple[str, dict]:
    """核心清洗流水线: 规则 → (可选) 千问分块精洗."""
    stats: dict = {"chunks": 0, "qwen_ok": 0, "fallback": 0}
    rule_cleaned, rule_stats = clean_by_rules(text)
    stats.update({f"rule_{k}": v for k, v in rule_stats.items()})

    if not use_qwen:
        return rule_cleaned, stats

    if heavy_clean:
        # 复读机页: 规则折叠后仍异常 → 只对规则结果调千问（不截断正文）
        stats["heavy"] = True

    chunks = smart_chunk(rule_cleaned)
    stats["chunks"] = len(chunks)
    out_blocks = []
    for i, c in enumerate(chunks):
        cleaned, status = qwen_clean_chunk(c, model, url, timeout)
        if status.startswith("qwen_"):
            stats["qwen_ok"] += 1
        else:
            stats["fallback"] += 1
        out_blocks.append(cleaned)
        print(f"    chunk {i + 1}/{len(chunks)} ({len(c)}字) -> {status}")
    return "\n\n".join(out_blocks), stats


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="MD 多级清洗 + 本地千问精洗")
    ap.add_argument("--in", dest="inp", required=True, help="输入 md 目录")
    ap.add_argument("--out", required=True, help="输出目录, 或以 .md 结尾表示合并输出")
    ap.add_argument("--no-qwen", action="store_true", help="跳过千问（纯规则, 秒级）")
    ap.add_argument("--model", default=MODEL_NAME)
    ap.add_argument("--url", default=OLLAMA_URL)
    ap.add_argument("--timeout", type=int, default=QWEN_TIMEOUT)
    ap.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    ap.add_argument("--keep-headers", action="store_true", help="保留跨页重复页眉")
    ap.add_argument("--diff", action="store_true", help="打印清洗前后 diff 片段")
    args = ap.parse_args()

    in_dir = Path(args.inp).expanduser()
    if not in_dir.is_dir():
        print(f"输入目录不存在: {in_dir}", file=sys.stderr)
        return 1
    md_files = sorted(in_dir.glob("*.md"))
    if not md_files:
        print(f"{in_dir} 下没有 md 文件", file=sys.stderr)
        return 1

    page_raw: dict[str, str] = {}
    for f in md_files:
        page_raw[f.name] = f.read_text(encoding="utf-8")
    lengths = {k: len(v) for k, v in page_raw.items()}
    median_len = statistics.median(list(lengths.values()))
    print(f"[基准统计] {len(md_files)} 页, 字数中位数 {median_len:.0f}")
    print(f"[基准统计] 字数分布 min={min(lengths.values())} max={max(lengths.values())}\n")

    out_target = Path(args.out).expanduser()
    merged = out_target.suffix == ".md"
    if not merged:
        out_target.mkdir(parents=True, exist_ok=True)
        for old in out_target.glob("*.md"):
            old.unlink()

    report: list[dict] = []
    cleaned_pages: list[tuple[str, str]] = []   # (name, text)

    # ---- 逐页: 阶段1 规则 + 阶段1.5 拦截 + 阶段2/3 千问 ----
    for name in sorted(page_raw):
        raw = page_raw[name]
        n = lengths[name]
        flags = []
        if is_blank_page(raw):
            flags.append("blank_page")
            markers = "<!-- [异常标记] 本页有效字数过少, 疑似空白页 -->\n"
            cleaned = markers + raw.strip()
            stats = {"skipped": "blank"}
        else:
            heavy, why = is_repeater(raw, median_len)
            flags.append(why)
            if heavy:
                print(f"{name}: [拦截] 复读机特征 {why} (字数 {n})")
            cleaned, stats = process_text_pipeline(
                raw, args.model, args.url, args.timeout,
                use_qwen=not args.no_qwen, heavy_clean=heavy,
            )
        stats["flag"] = ",".join(flags)
        stats["in_len"] = n
        stats["out_len"] = len(cleaned)
        report.append({"file": name, **stats})
        cleaned_pages.append((name, cleaned))
        print(f"{name}: {n} → {len(cleaned)} 字符  [{stats['flag']}]")

    # ---- 阶段 2.5: 跨页重复页眉 ----
    if not args.keep_headers:
        page_texts = [t for _, t in cleaned_pages]
        stripped, hdr_removed = strip_repeated_headers(page_texts)
        cleaned_pages = [(cleaned_pages[i][0], stripped[i]) for i in range(len(stripped))]
        print(f"\n跨页重复页眉/页脚删除: {hdr_removed} 行")
    else:
        hdr_removed = 0

    # ---- 阶段 2.6: 段落级去重（全局） ----
    dedup_removed = 0
    new_pages = []
    for name, t in cleaned_pages:
        paras = re.split(r"\n\s*\n", t)
        kept, rm = deduplicate_paragraphs(paras)
        dedup_removed += rm
        new_pages.append((name, "\n\n".join(kept)))
    cleaned_pages = new_pages
    print(f"段落级去重删除: {dedup_removed} 段")

    # ---- 输出 ----
    if merged:
        body = "\n\n".join(t for _, t in cleaned_pages)
        body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
        out_target.parent.mkdir(parents=True, exist_ok=True)
        out_target.write_text(body, encoding="utf-8")
        print(f"\n合并输出: {out_target} ({len(body)} 字符)")
    else:
        for name, t in cleaned_pages:
            (out_target / name).write_text(t if t.endswith("\n") else t + "\n",
                                           encoding="utf-8")
        print(f"\n逐页输出: {out_target}/ ({len(cleaned_pages)} 个文件)")

    # ---- 汇总 ----
    in_total = sum(lengths.values())
    out_total = (len(out_target.read_text(encoding="utf-8")) if merged
                 else sum((out_target / n).stat().st_size for n, _ in cleaned_pages))
    print("\n=== 清理汇总 ===")
    print(f"输入 {len(page_raw)} 页 / {in_total} 字符")
    print(f"输出 {'1 个合并文件' if merged else f'{len(cleaned_pages)} 个文件'} / {out_total} 字符")
    print(f"净减少 {in_total - out_total} 字符 ({(in_total - out_total) / max(1, in_total):.1%})")
    print(f"跨页页眉删除 {hdr_removed} 行, 段落去重 {dedup_removed} 段")
    if not args.no_qwen:
        ok = sum(r.get("qwen_ok", 0) for r in report)
        fb = sum(r.get("fallback", 0) for r in report)
        print(f"千问成功 {ok} 块, 回退 {fb} 块")
    rule_sum: dict[str, int] = {}
    for r in report:
        for k, v in r.items():
            if k.startswith("rule_") and isinstance(v, int):
                rule_sum[k] = rule_sum.get(k, 0) + v
    if rule_sum:
        print("规则清理明细: " + ", ".join(f"{k.replace('rule_', '')}={v}"
                                            for k, v in sorted(rule_sum.items()) if v))

    if args.diff:
        print("\n=== diff 片段（前 120 行）===")
        orig = "\n\n".join(page_raw[n] for n in sorted(page_raw))
        new = "\n\n".join(t for _, t in cleaned_pages)
        d = list(difflib.unified_diff(
            orig.splitlines(), new.splitlines(),
            fromfile="original", tofile="cleaned", lineterm="", n=3,
        ))
        print("\n".join(d[:120]))
        if len(d) > 120:
            print(f"... （共 {len(d)} 行 diff）")

    return 0


if __name__ == "__main__":
    sys.exit(main())