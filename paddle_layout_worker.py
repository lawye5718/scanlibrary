#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ScanLibrary 子进程 worker：PP-DocLayout 版面检测。

设计：
- 被 server.py 通过子进程调用，输入为单个图像路径（jpg / png），
  输出为一行 JSON：[{label, score, bbox:[x1,y1,x2,y2]}, ...]
- 故意只做版面检测（PP-DocLayout_plus-L，~123MB 模型），
  不跑 OCR；这样与 server 端 glm-ocr 分工，达成
  「Paddle 做布局，glm-ocr 做识别」的分工。

模型权重在首次启动时自动下载（modelscope 缓存到
~/.cache/modelscope/hub/），后续运行命中本地缓存。
"""
import sys
import json
import logging
from pathlib import Path


def _silence_logs():
    for name in ("paddle", "modelscope", "paddleocr", "paddlex"):
        logging.getLogger(name).setLevel(logging.ERROR)


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: paddle_layout_worker.py <image_path>\n")
        sys.exit(1)
    img_path = Path(sys.argv[1]).resolve()
    if not img_path.exists():
        sys.stderr.write(f"image not found: {img_path}\n")
        sys.exit(2)

    _silence_logs()
    from paddleocr import LayoutDetection
    det = LayoutDetection(device="cpu")
    res = det.predict(str(img_path))
    if not res:
        sys.stdout.write(json.dumps([], ensure_ascii=False))
        return

    r = res[0]
    boxes = r.get("boxes", []) if isinstance(r, dict) else []
    out = []
    for b in boxes:
        try:
            out.append({
                "label": str(b.get("label", "")),
                "score": float(b.get("score", 0.0)),
                "bbox": [float(x) for x in b["coordinate"]],
            })
        except Exception:
            continue
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()