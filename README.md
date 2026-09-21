# ScanLibrary · 扫描书转 EPUB 工作台

把扫描版 PDF 扔进浏览器，自动变成可调字号、可搜索、可划线的 EPUB，落到本机指定目录。
**全本地、零 API 费用**：OCR 走你机器上已装好的 Ollama，产物只写在本机磁盘。

---

## 项目简介

ScanLibrary 是一个**单机扫描书数字化工作台**：浏览器里拖入 PDF，后端逐页渲染 → OCR → 清洗文本 → 切章 → 打包 EPUB，全程在本机完成。

- **零外部服务**：后端只用 Python 标准库 `http.server`，EPUB 自己打包，不需要 pandoc / Calibre / Sigil，也不需要任何在线 API。
- **零 API 费用**：OCR 用本机 Ollama 里的视觉模型（默认 `glm-ocr`，约 2.2GB）；校对复用你已装的文本模型。
- **代码极简**：一个 `server.py` + 一个 `web/index.html`，无构建步骤、无前端框架，改完重启即生效。
- **只监听本机**：服务绑定 `127.0.0.1`，上传的 PDF 和产出物都不出本机。

| 项目 | 值 |
|---|---|
| 当前版本 | `0.1.0` |
| 运行环境 | macOS / Linux / Windows 均可，已在 Apple Silicon macOS 端到端跑通 |
| Python 依赖 | 仅 `pymupdf`（渲染 PDF 页为图片） |
| 外部依赖 | Ollama + 一个视觉模型（仅 `glm-ocr` 后端需要） |
| 默认监听 | `http://127.0.0.1:8765` |

## 已实现功能（v0.1.0）

### 1. 上传与任务管理
- 浏览器**拖拽或点选**上传 PDF，支持**多文件批量**；请求体流式落盘，不把整本书读进内存
- 两种转换模式：**测试版**（只跑前 10 页出试读版）与**全书**；测试版结果在跑全书时自动复用，不重复计算
- **页面范围**可自定义（`1-10`、`35`、`1-300`），留空即全书
- 任务卡片实时**进度条**、阶段消息、**预计剩余时间**（基于历史页速统计；无历史数据时给参考区间）
- 任务可**取消**、失败可**重试（续跑）**、可**删除**（连同产物）
- 每个任务带**日志面板**，可在页面上直接看后端流水日志

### 2. 五种 OCR 后端
| 后端 | 说明 |
|---|---|
| `glm-ocr` | 默认。调 Ollama 视觉模型逐页 OCR，本地小模型、免费用 |
| `paddle-layout` | **最稳的扫描件方案**：PaddleOCR PP-DocLayout 做版面分析（识别正文/图/页眉页脚/页码/印章），文字区裁剪后调 glm-ocr 识别，图区保留为插图。500 风险近乎为零、插图保留、页眉页脚/页码规则化剥离 |
| `mineru` | 调本机 MinerU CLI（需自行安装），中文精度更高，产物为 Markdown 后由本程序接手切章打包 |
| `text-layer` | 直接抽取 PDF 自带文字层，**秒出、不需要任何模型**；适合本来就带文字层的"伪扫描件" |
| `stub` | 空跑占位，只验证整条流水线，不烧算力 |

**关于 `paddle-layout` 后端**

- 需本机已装 PaddleOCR 3.x（CPU 即可），并指明其 venv 的 python3 路径
- 设置环境变量 `SCANLIBRARY_PADDLE_VENV=/path/to/venv/bin/python3`；未设置时会自动扫描 `~/superstar/superstar3.1/projects/*/venv/bin/python3`
- 首次跑会自动下载 `PP-DocLayout_plus-L` 模型（~123MB，存到 modelscope 缓存）
- **何时用它**：看到 glm-ocr 频繁 500、插图消失、页眉页脚混进正文时换它；古籍竖排、混排图文的书首选它
- 完整流程：每页先由 PP-DocLayout 标出区域 → 文字区裁剪后给 glm-ocr（单张图通常只有页面的 1/4~1/8，几乎不会触发大图 500）→ 图区直接存为 jpg，markdown 嵌入 EPUB

### 3. OCR 稳定性与画质（踩坑后加固的部分）
- **渲染 DPI 可调**（72–600），**并发页数可调**（内存小填 1–2）
- **大图自动降采样重试**：渲染出的超大图（约 2MB 以上）会先缩到安全边长再送模型，规避 llama-server 因图像过大直接返回 500 的问题
- **失败页标记**：识别失败的页写入 `【OCR-FAILED】` 标记，重跑时自动识别并重新 OCR，不会把空页当成功结果混进正文
- **断点续跑**：每页结果单独落盘，任何中断（关窗口 / Ctrl+C / 断电）后重跑都自动跳过已完成页
- 可选**忽略已有进度全部重跑**，用于单页明显识别错误时的定点重来

### 4. 文本后处理
- **跨页重复的页眉/页脚**自动识别并剔除（按出现比例判定）
- **中文断行合并**：把 OCR 产生的硬换行还原成正常段落
- **正则切章**（`CHAPTER_PATTERNS` 可自定义），**单章过长自动分卷**
- 可选**校对开关**：用本机文本模型（如 `qwen14b-pro`）逐段改错字、标点、断行，结果另存 `book.proofread.md`
- **失败块自动补救**：校对块若被判定为偷懒/幻觉，会自动对半切开重试一次；仍不合格才回退原文，覆盖率更高

### 5. 产物与导出
- 自己打包 **EPUB3**（含 `nav.xhtml` 目录 + `toc.ncx`，老阅读器与 Kindle 也能识别目录），带正文字体栈与首行缩进样式
- 同时输出 **Markdown**（`book.md` 全文合并、`pages/` 逐页原文）
- 浏览器直接**下载 EPUB / Markdown**；中文文件名按 RFC 5987 编码，不会乱码
- **「在访达中显示」**与**「打开成书目录」**，产物可立即拖进微信读书等阅读器

### 6. 安全与资源管理
- **访问密码登录**（`--password` 指定，或首次启动随机生成并写入 `data/auth.json`）
- Bearer token 认证，**8 小时滑动续期**；未认证请求一律 401
- 登录时**异步预热** `glm-ocr`，省去首本书的模型加载等待
- **空闲自动回收**：30 分钟无活动且无任务时卸载模型并清除登录态，释放内存
- 前端 token 存 `localStorage`，失效自动弹回登录页

### 7. 自测脚本
| 脚本 | 作用 |
|---|---|
| `selftest.py` | 端到端：上传 → OCR(stub) → 构建 EPUB → 下载校验 |
| `selftest_ollama.py` | 用假 Ollama 服务验证请求参数、重试与降采样逻辑 |
| `sl_local_e2e.py` | 本地真实链路（含真实模型）的连通性验证 |

---

## 一、30 秒上手（Mac）

```bash
# 1) 唯一依赖：把 PDF 页渲染成图片
python3 -m pip install --upgrade pymupdf

# 2) OCR 视觉模型（约 2.2GB，只需拉一次；你已装的其他模型不受影响）
ollama pull glm-ocr

# 3) 启动（会自动打开浏览器）
cd scanlibrary
python3 server.py --open
```

打开 http://127.0.0.1:8765 → 拖入 PDF → 等进度条 → 点「下载 EPUB」或「在访达中显示」。

> 也可以直接**双击 `start.command`**，它会自动检查依赖、检查 Ollama 与 glm-ocr 是否就位，然后启动。

## 二、为什么这样选（结合你这台机器）

你本地已装的 `qwen14b-pro`、MLX 里的 `Qwen2.5-32B` 都是**纯文本模型，没有视觉能力**，做不了 OCR——这不是配置问题，是模型本身没有图像编码器。所以扫描书的 OCR 这一步必须有一个视觉模型，绕不过去。

本工作台因此走两条腿：

| 环节 | 用谁 | 要不要下载 |
|---|---|---|
| **OCR（看页面）** | `glm-ocr`（0.9B 视觉模型，2.2GB） | 需要 `ollama pull glm-ocr`，**只有 2.2GB** |
| **校对（改错字/标点/断行）** | 你**已装的** `qwen14b-pro` | **不用下载**，填进「校对模型」即可 |
| 目录重建、章节切分 | 程序本地正则处理 | 不用 |

这个组合的性价比在于：OCR 只用一个 2.2GB 的小视觉模型，而真正耗时的"润色校对"交给你已经跑熟的文本模型。校对是**可选开关**，默认关闭——先跑十页看效果，觉得错字多再开。

**为什么不直接用已装模型做 OCR**：试了都不行。真正能"零下载"只有一种情况——你的 PDF **本身带文字层**（不是扫描件），那就选后端「直接抽文字层」，秒出，完全不需要模型。

## 三、目录结构

```
~/ScanLibrary/                 # 默认数据目录，可用 --root 改
├── uploads/                   # 上传的 PDF 原件
├── books/
│   └── 万历十五年/             # 每个书名一个目录
│       ├── 万历十五年.epub     # ← 成品，直接拖进微信读书
│       ├── 万历十五年.md       # Markdown 版，方便校对/检索
│       ├── book.md            # 合并后的全文
│       ├── book.proofread.md  # 校对过的全文（开了校对才有）
│       ├── images/            # 每页渲染图
│       └── pages/             # 每页 OCR 结果（断点续跑靠它）
└── data/jobs.json             # 任务索引，重启不丢
```

**代码与数据的关系（容易踩的坑）**

上图画的是**数据目录**，代码目录默认就是同一个目录（`~/ScanLibrary/`）。macOS 文件系统大小写不敏感，所以 `~/scanlibrary` 与 `~/ScanLibrary` 实际指向同一个文件夹——这是同一个目录，不是两个。

- 仓库里**只包含代码与文档**：`server.py`、`selftest*.py`、`sl_local_e2e.py`、`web/`、`README.md`、`requirements.txt`、`start.command`、`.gitignore`。
- `data/`、`uploads/`、`books/` 已在 `.gitignore` 中忽略：**登录密码、你上传的 PDF 原件、OCR 产物都不会进 git**，克隆下来是一份干净代码。
- 想把代码和书库分开，用 `--root` 指定数据目录，例如 `python3 server.py --root ~/Books`（`uploads/`、`books/`、`data/` 会建在该目录下）。

## 四、必看：先跑十页

**不要一上来就扔一本 400 页的书。** 正确姿势：

1. 页面范围填 `1-10`，后端选 `glm-ocr`，点上传；
2. 十几分钟后看产出的 EPUB：目录对不对、段落有没有断错、生僻字准不准；
3. 满意了，把同一本书**再传一次**，页面范围留空 → 前 10 页直接复用缓存，只跑剩下的。

任何时候中断（关窗口、Ctrl+C、断电），分页结果都在 `pages/` 里，重跑自动跳过已完成页。

## 五、页面上的选项怎么填

| 选项 | 建议值 | 说明 |
|---|---|---|
| OCR 后端 | `paddle-layout` | 大多数扫描书直接用它最稳；`glm-ocr` 适合干净整页；`text-layer` 只适用于本来就可选中文字的 PDF |
| OCR 模型 | `glm-ocr` | 也可换成 Ollama 里其他视觉模型（如 `qwen2.5vl`） |
| Ollama 地址 | `http://localhost:11434` | 默认 |
| 渲染 DPI | **250**（默认）／300（字小、发虚） | 大多数扫描书 250 已够用；越高越准越慢 |
| 并发页数 | **1**（默认） | 最稳；16GB 内存通常保持 1，机器更富余再调到 2–3 |
| 页面范围 | 试跑填 `1-10` | 留空=全书 |
| 分页/分章方案 | **自动识别（默认）** | 自动识别封面、封底、扉页、版权页、目录页、序言；留空时沿用自动识别，填写页码可覆盖；也可切到“全部人工指定” |
| 校对（勾选项）+ 校对模型 | `qwen14b-pro` | **用你已装的模型**，逐段改错字、标点、断行；300 页可能再加 1–2 小时，按需开 |
| 忽略已有进度重跑 | 一般不勾 | 某页明显识别错了，删掉 `pages/00XX.md` 再勾它重跑那一页即可 |

## 六、速度预期（务必有心理准备）

`glm-ocr` 在 Apple Silicon 上大约 **10–40 秒/页**（视芯片与 DPI）。所以：

| 书 | 仅 OCR | OCR + 校对 |
|---|---|---|
| 100 页 | 约 30–60 分钟 | 再加约 40 分钟 |
| 300 页 | 约 1.5–4 小时 | 再加约 2 小时 |
| 600 页 | 建议分批跑（如 1-300、301-600） | — |

放着让它跑就行，进度条和剩余时间在页面上实时更新，中途可以取消。

## 七、常见问题

**Q：`ollama pull glm-ocr` 报错 412 / requires a newer version**
Ollama 版本太老，先 `brew upgrade ollama`（或从 ollama.com 重装），再 pull。

**Q：首页右上角显示「缺少 glm-ocr」**
说明没拉模型。另开终端执行 `ollama pull glm-ocr`，页面刷新即可。

**Q：OCR 出来的字错得离谱**
- DPI 调到 300 重试几页；
- 原书扫描件本身发虚／倾斜的话，先做去歪、提对比度；
- 打开「校对」开关，让 `qwen14b-pro` 兜一层；
- 古籍竖排、繁体、异体字：本项目目前只按横排优化，竖排请接受人工校勘，或改用 MinerU 后端试试。

**Q：想追求更高精度**
页面后端选 `mineru`（前提：已按 `uv pip install -U "mineru[core,mlx]"` 装好），中文书精度通常更好，代价是要装约 20GB 模型、且它只出 Markdown——本程序会自动接手后面的切章与 EPUB 打包。

**Q：能不能换别的视觉模型**
能。只要 Ollama 里有带视觉能力的模型（如 `qwen2.5vl`），把模型名填进「OCR 模型」即可。纯文本模型填进去会得到空结果。

**Q：EPUB 进微信读书排版怪**
本程序生成的是标准 EPUB3（带 nav 目录、正文样式、首行缩进）。若章节切得不合意，改 `server.py` 里的 `CHAPTER_PATTERNS` 正则即可，改完重启服务。

## 八、技术说明

- 后端仅用 Python 标准库 `http.server`，EPUB 由本程序自己打包，**不需要 pandoc / Calibre / Sigil**；
- 服务只监听 `127.0.0.1`，不对外暴露，上传文件不出本机；
- 后处理包含：跨页重复的页眉页脚自动识别并剔除、中文断行合并、按标题正则切章、单章过长自动分卷；
- 自测脚本：`python3 selftest.py`（端到端流程）、`python3 selftest_ollama.py`（用假 Ollama 验证调用参数）。

## 九、许可与提醒

代码可自由使用修改。请只数字化你自己合法拥有的图书，产出物不要外传。

## 十、代码结构与 HTTP 接口

### 文件构成

```
server.py           # 全部后端：渲染 / OCR / 后处理 / 切章 / EPUB 打包 / HTTP 服务
web/index.html      # 单页前端：登录、上传、任务列表、进度与日志（原生 JS，无框架）
selftest.py         # 端到端自测（stub 后端，不需要模型）
selftest_ollama.py  # 假 Ollama，验证请求参数与重试逻辑
sl_local_e2e.py     # 本地真实链路验证
start.command       # macOS 双击启动（检查依赖与模型后拉起服务）
requirements.txt    # 依赖清单（pymupdf）
```

`server.py` 内部分层：

| 区块 | 关键函数 | 职责 |
|---|---|---|
| 配置与状态 | `load_or_create_password`、`save_jobs`、`load_stats` | 密码、任务索引、速度统计持久化 |
| 页面渲染 | `render_pages`、`pdf_page_count`、`extract_text_layer` | 渲染页面为图片 / 取页数 / 抽文字层 |
| OCR | `ollama_generate`、`ocr_page_glmocr`、`_shrink_image`、`run_mineru` | 调 Ollama、大图降采样、失败重试、MinerU |
| 后处理 | `detect_running_titles`、`strip_junk`、`merge_wrapped_lines` | 页眉页脚剔除、断行合并 |
| 结构 | `split_chapters`、`CHAPTER_PATTERNS` | 按标题正则切章、超长自动分卷 |
| 打包 | `md_to_xhtml`、`build_epub`、`CSS` | Markdown→XHTML、EPUB3 + `toc.ncx` |
| 调度 | `run_job`、`worker_loop`、`job_public` | 任务队列、进度、取消、续跑 |
| HTTP | `Handler.do_GET`、`Handler.do_POST` | 路由、认证、上传、下载 |
| 维护 | `idle_watchdog`、`ollama_preload`、`ollama_unload` | 空闲卸载模型、登录预热 |

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端页面（免认证） |
| GET | `/api/health` | 健康检查，返回数据目录与版本（免认证） |
| POST | `/api/login` | 密码换 token（免认证） |
| GET | `/api/jobs` | 任务列表 |
| GET | `/api/job/<id>` | 单任务详情 + 最近 120 行日志 |
| GET | `/api/download/<id>` | 下载 EPUB |
| GET | `/api/download-md/<id>` | 下载全书 Markdown |
| GET | `/api/reveal/<id>` | 在文件管理器中定位产物 |
| GET | `/api/open-folder` | 打开成书根目录 |
| GET | `/api/models` | 列出 Ollama 已装模型 |
| GET | `/api/model-state` | 当前已载入内存的模型 |
| GET | `/api/stats` | 历史页速统计 |
| POST | `/api/upload` | 上传 PDF 建任务；配置走 `X-Config` 头，另用 `X-Filename` / `X-Title` / `X-Author` 传元数据 |
| POST | `/api/cancel/<id>`、`/api/retry/<id>`、`/api/delete/<id>` | 取消 / 续跑重试 / 删除任务 |

除登录与健康检查外，所有接口都要求 `Authorization: Bearer <token>`。

## 十一、版本管理与备份

本目录已初始化为 git 仓库，代码托管在 <https://github.com/lawye5718/scanlibrary>。

```bash
git add -A
git commit -m "说明这次改了什么"
git push
```

`.gitignore` 的取舍原则是**只有代码和文档进仓库**：

| 忽略项 | 原因 |
|---|---|
| `data/` | 内含 `auth.json`（登录密码明文）、任务索引、运行统计 |
| `uploads/` | 上传的 PDF 原件，体积大且多受版权保护 |
| `books/` | 转换产物（EPUB / Markdown / 逐页图 / OCR 缓存），可重新生成 |
| `__pycache__/`、`.venv/`、`.DS_Store` | 缓存与本地环境垃圾 |

> 本机 22 端口被网络工具占用，推送改走 **443 端口 SSH**，因此远程地址是
> `ssh://git@ssh.github.com:443/lawye5718/scanlibrary.git`（GitHub 官方备用通道）。
> 换机器时直接 `git clone` 这条地址即可。
