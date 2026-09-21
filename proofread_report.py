# -*- coding: utf-8 -*-
"""校对对照表渲染 + 过程文件备份/回退。

纯标准库，不依赖 server.py，便于单独单测。
"""
from __future__ import annotations

import difflib
import html
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

# 复制到备份目录的文件模式（书籍根目录下的成品/过程文件）
BACKUP_PATTERNS = ("*.md", "*.epub")
BACKUP_EXTRA = ("notes.json",)
# 校对产物：回退时备份里没有就必须删除，避免残留过期结果
PRODUCTS = ("book.proofread.md", "proofread-report.html",
            "proofread-report.md", "proofread-report.json")
KEEP_BACKUPS = 3


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 改动比对
# ---------------------------------------------------------------------------

def _is_cjk(s: str) -> bool:
    return bool(s) and all("\u3400" <= ch <= "\u9fff" for ch in s)


def _is_punct(s: str) -> bool:
    return bool(s) and all(not ch.isalnum() and not ("\u3400" <= ch <= "\u9fff") for ch in s)


def classify_change(old: str, new: str):
    """给一处改动打标签与风险级别。

    级别 warn 表示值得人工重点核对（增删、大段改写），safe 表示常规错字/标点。
    """
    if old == new:
        return "无变化", "safe"
    if not old.strip() and not new.strip():
        return "空白", "safe"
    if not old.strip():
        return "增补", "warn"
    if not new.strip():
        return "删除", "warn"
    if _is_punct(old) and _is_punct(new):
        return "标点", "safe"
    if _is_cjk(old) and _is_cjk(new) and len(old) <= 3 and len(new) <= 3:
        return "错字", "safe"
    if len(old) > 6 or len(new) > 6:
        return "大段改写", "warn"
    return "词句调整", "safe"


def change_pairs(orig: str, fixed: str, max_pairs: int = 500):
    """取出两段文本之间的最小替换对 [(旧, 新), ...]。"""
    if orig == fixed:
        return []
    sm = difflib.SequenceMatcher(None, orig or "", fixed or "", autojunk=False)
    pairs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        pairs.append((orig[i1:i2], fixed[j1:j2]))
        if len(pairs) >= max_pairs:
            break
    return pairs


def _context(text: str, needle: str, width: int = 12) -> str:
    """取 needle 在 text 中的上下文，便于人工定位。"""
    if not needle:
        return ""
    idx = text.find(needle)
    if idx < 0:
        return ""
    lo = max(0, idx - width)
    hi = min(len(text), idx + len(needle) + width)
    return (("…" if lo > 0 else "") + text[lo:hi].replace("\n", "⏎")
            + ("…" if hi < len(text) else ""))


def summarize_changes(records):
    """汇总所有已验证块的改动为对照表行。

    返回 [{old, new, count, label, level, ctx, chunks}, ...]，风险高的排前面。
    """
    agg = {}
    for rec in records or []:
        if rec.get("status") != "ok":
            continue
        orig, fixed = rec.get("orig", ""), rec.get("fixed", "")
        for old, new in change_pairs(orig, fixed):
            if not old.strip() and not new.strip():
                continue                      # 纯换行/空格差异不列入对照表
            item = agg.get((old, new))
            if item is None:
                label, level = classify_change(old, new)
                agg[(old, new)] = {
                    "old": old, "new": new, "count": 1, "label": label,
                    "level": level, "ctx": _context(orig, old),
                    "chunks": [rec["i"]],
                }
            else:
                item["count"] += 1
                if rec["i"] not in item["chunks"]:
                    item["chunks"].append(rec["i"])
    order = {"warn": 0, "safe": 1}
    return sorted(agg.values(), key=lambda d: (order.get(d["level"], 2), -d["count"]))


def _html_diff(orig: str, fixed: str):
    """返回 (原文高亮, 校对后高亮) 两段 HTML，删除/新增分别用 del/ins 包裹。"""
    sm = difflib.SequenceMatcher(None, orig or "", fixed or "", autojunk=False)
    left, right = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            left.append(html.escape(orig[i1:i2]))
            right.append(html.escape(fixed[j1:j2]))
            continue
        if i2 > i1:
            left.append("<del>" + html.escape(orig[i1:i2]) + "</del>")
        if j2 > j1:
            right.append("<ins>" + html.escape(fixed[j1:j2]) + "</ins>")
    return "".join(left), "".join(right)


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#f6f5f1;--card:#fff;--ink:#23201c;--muted:#7a736a;--line:#e5e1d8;
      --accent:#8a5a2b;--ok:#3f7d4e;--err:#b3453a;--warn:#b8860b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.75;
     font-family:-apple-system,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 24px 70px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;color:var(--accent);margin:26px 0 10px;letter-spacing:.5px}
.meta{color:var(--muted);font-size:12.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:#faf8f4;font-weight:600;color:#5a534a;font-size:12.5px;white-space:nowrap}
td.num{text-align:right;color:var(--muted);white-space:nowrap}
td.old{color:#8a3a30;font-weight:600;white-space:pre-wrap}
td.new{color:#2f6a3e;font-weight:600;white-space:pre-wrap}
td.ctx{color:var(--muted);font-size:12px;white-space:pre-wrap}
tr.warn td.old,tr.warn td.new{background:#fdf7e8}
.kv td:first-child{color:var(--muted);width:130px;white-space:nowrap}
.blk{border:1px solid var(--line);border-radius:8px;margin-bottom:9px;background:#fffdf9}
.blk>summary{cursor:pointer;padding:8px 12px;font-size:13px}
.blk>summary::-webkit-details-marker{color:var(--muted)}
.st{font-size:11.5px;padding:1px 7px;border-radius:20px;border:1px solid var(--line);margin-left:6px}
.st.ok{color:var(--ok);border-color:#bcd9c4;background:#f0f8f2}
.st.fb{color:var(--err);border-color:#e6c2be;background:#fdf2f1}
.st.same{color:var(--muted)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid var(--line)}
.col{min-width:0;padding:8px 10px}
.col+.col{border-left:1px solid var(--line)}
.lh{font-size:11.5px;color:var(--muted);margin-bottom:4px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:13px;
    font-family:inherit}
del{background:#fbe3e0;color:#8a3a30;text-decoration:line-through}
ins{background:#e3f3e7;color:#2f6a3e;text-decoration:none}
.hint{color:var(--muted);font-size:12.5px;margin:0 0 10px}
.tag{display:inline-block;font-size:11.5px;padding:1px 6px;border-radius:4px;background:#f2efe8;color:#5a534a}
@media (max-width:820px){.cols{grid-template-columns:1fr}.col+.col{border-left:0;border-top:1px solid var(--line)}}
"""


def render_report_html(title, records, meta):
    records = list(records or [])
    meta = dict(meta or {})
    changes = summarize_changes(records)
    n_warn = sum(1 for c in changes if c["level"] == "warn")
    n_edits = sum(c["count"] for c in changes)

    kv = [
        ("书名", title or "（未命名）"),
        ("校对模型", meta.get("model") or "-"),
        ("校对时间", meta.get("created_at") or "-"),
        ("校对源", meta.get("source") or "-"),
        ("分块", f"{meta.get('total', len(records))} 块 / 每块约 {meta.get('chunk_chars', '-')} 字"),
        ("结果", f"{meta.get('n_ok', 0)} 块通过 · {meta.get('n_fallback', 0)} 块回退原文 · "
                 f"{meta.get('n_changed', 0)} 块有改动"),
        ("改动处数", f"{n_edits} 处（其中 {n_warn} 处建议重点核对）"),
    ]
    kv_html = "".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
                      for k, v in kv)

    rows = []
    for k, c in enumerate(changes, 1):
        old = html.escape(c["old"]) or "<i>（空）</i>"
        new = html.escape(c["new"]) or "<i>（删除）</i>"
        rows.append(
            f'<tr class="{c["level"]}"><td class="num">{k}</td>'
            f'<td class="old">{old}</td><td class="new">{new}</td>'
            f'<td><span class="tag">{html.escape(c["label"])}</span></td>'
            f'<td class="num">{c["count"]}</td>'
            f'<td class="num">{",".join(str(x) for x in c["chunks"])}</td>'
            f'<td class="ctx">{html.escape(c["ctx"])}</td></tr>')
    changes_html = ("".join(rows) if rows else
                    '<tr><td colspan="7" style="color:#7a736a">本次校对没有产生任何字词改动</td></tr>')

    blocks = []
    for rec in records:
        left, right = _html_diff(rec.get("orig", ""), rec.get("fixed", ""))
        fb = rec.get("status") != "ok"
        changed = rec.get("status") == "ok" and rec.get("orig") != rec.get("fixed")
        if fb:
            st, cls = "回退原文（" + (rec.get("reason") or "未通过校验") + "）", "fb"
        elif changed:
            st, cls = "已采纳", "ok"
        else:
            st, cls = "无改动", "same"
        delta = ""
        if changed:
            delta = f' · {len(rec.get("orig", ""))}→{len(rec.get("fixed", ""))} 字'
        blocks.append(
            f'<details class="blk"{" open" if (changed or fb) else ""}>'
            f'<summary><b>块 {rec.get("i", "?")}/{rec.get("total", "?")}</b>'
            f'<span class="st {cls}">{html.escape(st)}</span>{delta}</summary>'
            f'<div class="cols">'
            f'<div class="col"><div class="lh">OCR 原文</div><pre>{left}</pre></div>'
            f'<div class="col"><div class="lh">校对后</div><pre>{right}</pre></div>'
            f'</div></details>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>校对对照表 · {html.escape(title or "")}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<h1>校对对照表</h1>
<div class="meta">逐处列出千问对 OCR 文本做过的全部改动，供人工确认；最后一节是逐块原文／校对后对照。</div>

<h2>① 概况</h2>
<div class="card"><table class="kv">{kv_html}</table></div>

<h2>② 改动明细（人工确认重点）</h2>
<p class="hint">黄色行 = 增删或大段改写，建议逐条确认；「错字」「标点」属于常规修正。
「块」列是出现该改动的分块编号，可在下一节定位原文。</p>
<div class="card"><table>
<thead><tr><th>#</th><th>原文</th><th>改为</th><th>类型</th><th>次数</th><th>块</th><th>上下文</th></tr></thead>
<tbody>{changes_html}</tbody></table></div>

<h2>③ 逐块对照</h2>
<p class="hint">默认展开「有改动」与「回退原文」的块；回退表示该块未通过兜底校验，已原样保留 OCR 文本。</p>
{''.join(blocks)}
</div></body></html>
"""


def render_report_md(title, records, meta):
    records = list(records or [])
    meta = dict(meta or {})
    changes = summarize_changes(records)
    n_warn = sum(1 for c in changes if c["level"] == "warn")

    def esc(s):
        return (s or "").replace("|", "\\|").replace("\n", "⏎") or "（空）"

    out = [f"# 校对对照表 · {title or '（未命名）'}", ""]
    out.append(f"- 校对模型：{meta.get('model') or '-'}")
    out.append(f"- 校对时间：{meta.get('created_at') or '-'}")
    out.append(f"- 校对源：{meta.get('source') or '-'}")
    out.append(f"- 分块：{meta.get('total', len(records))} 块 / 每块约 {meta.get('chunk_chars', '-')} 字")
    out.append(f"- 结果：{meta.get('n_ok', 0)} 块通过 · {meta.get('n_fallback', 0)} 块回退原文 · "
               f"{meta.get('n_changed', 0)} 块有改动")
    out.append(f"- 改动处数：{sum(c['count'] for c in changes)} 处（{n_warn} 处建议重点核对）")
    out += ["", "## 一、改动明细", "",
            "| # | 原文 | 改为 | 类型 | 次数 | 块 | 上下文 |",
            "|---|---|---|---|---|---|---|"]
    if changes:
        for k, c in enumerate(changes, 1):
            out.append(f"| {k} | {esc(c['old'])} | {esc(c['new'])} | {c['label']} | "
                       f"{c['count']} | {','.join(str(x) for x in c['chunks'])} | {esc(c['ctx'])} |")
    else:
        out.append("| - | - | - | 本次没有产生任何字词改动 | - | - | - |")

    out += ["", "## 二、逐块对照", ""]
    for rec in records:
        fb = rec.get("status") != "ok"
        changed = rec.get("status") == "ok" and rec.get("orig") != rec.get("fixed")
        if fb:
            st = "回退原文（" + (rec.get("reason") or "未通过校验") + "）"
        elif changed:
            st = "已采纳"
        else:
            st = "无改动"
        out.append(f"### 块 {rec.get('i', '?')}/{rec.get('total', '?')} —— {st}")
        out.append("")
        if changed:
            for old, new in change_pairs(rec.get("orig", ""), rec.get("fixed", "")):
                out.append(f"- `{esc(old)}` → `{esc(new)}`")
            out.append("")
        if changed or fb:
            out += ["原　文：", "", "```", (rec.get("orig") or "").strip(), "```", "",
                    "校对后：", "", "```", (rec.get("fixed") or "").strip(), "```", ""]
    return "\n".join(out)


def write_report(book_dir, title, records, meta):
    """把对照表写进成书目录，返回 {"html":..., "md":..., "json":...}。"""
    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    meta = dict(meta or {})
    meta.setdefault("created_at", _now())
    meta.setdefault("total", len(records or []))
    meta.setdefault("n_ok", sum(1 for r in records or [] if r.get("status") == "ok"))
    meta.setdefault("n_fallback", sum(1 for r in records or [] if r.get("status") != "ok"))
    meta.setdefault("n_changed", sum(1 for r in records or []
                                     if r.get("status") == "ok" and r.get("orig") != r.get("fixed")))
    files = {
        "html": book_dir / "proofread-report.html",
        "md": book_dir / "proofread-report.md",
        "json": book_dir / "proofread-report.json",
    }
    files["html"].write_text(render_report_html(title, records, meta), encoding="utf-8")
    files["md"].write_text(render_report_md(title, records, meta), encoding="utf-8")
    files["json"].write_text(json.dumps({"title": title, "meta": meta,
                                         "records": list(records or [])},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: str(v) for k, v in files.items()}


# ---------------------------------------------------------------------------
# 备份 / 回退
# ---------------------------------------------------------------------------

def _backup_root(book_dir):
    return Path(book_dir) / "backup"


def _snapshot_names(book_dir):
    names = []
    for pat in BACKUP_PATTERNS:
        names += [p.name for p in sorted(Path(book_dir).glob(pat)) if p.is_file()]
    for name in BACKUP_EXTRA:
        if (Path(book_dir) / name).is_file():
            names.append(name)
    return sorted(set(names))


def backup_artifacts(book_dir, tag=None, keep=KEEP_BACKUPS, note=""):
    """校对前把成书目录里的过程文件/成品整体快照一份，供一键回退。"""
    book_dir = Path(book_dir)
    tag = tag or time.strftime("%Y%m%d-%H%M%S")
    dest = _backup_root(book_dir) / tag
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for name in _snapshot_names(book_dir):
        shutil.copy2(book_dir / name, dest / name)
        saved.append(name)
    manifest = {"tag": tag, "created_at": _now(), "note": note, "files": saved}
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _prune(book_dir, keep)
    return dest


def _prune(book_dir, keep):
    root = _backup_root(book_dir)
    if not root.is_dir():
        return
    dirs = sorted([d for d in root.iterdir() if d.is_dir()], key=lambda d: d.name)
    for old in dirs[:max(0, len(dirs) - int(keep))]:
        shutil.rmtree(old, ignore_errors=True)


def list_backups(book_dir):
    root = _backup_root(book_dir)
    if not root.is_dir():
        return []
    out = []
    for d in sorted([d for d in root.iterdir() if d.is_dir()], key=lambda d: d.name, reverse=True):
        info = {"tag": d.name, "created_at": "", "note": "", "files": []}
        man = d / "manifest.json"
        if man.is_file():
            try:
                info.update(json.loads(man.read_text(encoding="utf-8")))
            except Exception:  # noqa
                pass
        out.append(info)
    return out


def restore_backup(book_dir, tag="latest"):
    """把成书目录恢复为备份时的状态。

    快照里有的文件覆盖回来；快照里没有的校对产物（book.proofread.md、
    对照表）与多余的 epub 一律删除，保证不留过期结果。
    """
    book_dir = Path(book_dir)
    root = _backup_root(book_dir)
    dirs = sorted([d for d in root.iterdir() if d.is_dir()], key=lambda d: d.name, reverse=True) \
        if root.is_dir() else []
    if not dirs:
        raise RuntimeError("没有可回退的备份")
    if tag in ("latest", "", None):
        src = dirs[0]
    else:
        src = next((d for d in dirs if d.name == tag), None)
        if src is None:
            raise RuntimeError(f"找不到备份 {tag}")

    names = {p.name for p in src.iterdir() if p.name != "manifest.json"}
    restored, removed = [], []
    for name in sorted(names):
        shutil.copy2(src / name, book_dir / name)
        restored.append(name)
    for name in PRODUCTS:
        if name not in names and (book_dir / name).is_file():
            (book_dir / name).unlink()
            removed.append(name)
    for pat in BACKUP_PATTERNS:
        for p in sorted(book_dir.glob(pat)):
            if p.name not in names:
                p.unlink()
                removed.append(p.name)
    return {"tag": src.name, "restored": restored, "removed": removed}
