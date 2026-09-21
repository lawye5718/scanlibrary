#!/bin/bash
# Mac 双击即可启动 ScanLibrary（也可在终端里 bash start.command）
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3)
echo "使用 Python：$($PY -V 2>&1)"

# 1) 依赖检查
if ! $PY -c "import fitz" >/dev/null 2>&1; then
  echo "正在安装唯一依赖 pymupdf …"
  $PY -m pip install --upgrade pymupdf || exit 1
fi

# 2) Ollama 检查
if ! curl -s -m 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "⚠️  未检测到 Ollama 服务。请先安装并启动 Ollama（brew install ollama && brew services start ollama）"
fi
if ! curl -s -m 3 http://localhost:11434/api/tags | grep -q "glm-ocr" 2>/dev/null; then
  echo "⚠️  未检测到 glm-ocr 模型，OCR 会失败。请在另一个终端执行：ollama pull glm-ocr"
fi

# 3) 启动
exec $PY server.py --open
