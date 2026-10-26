# 页间 LitSeek

面向研究生与知识工作者的文献复盘助手。把人工整理论文笔记按“论文—章节—页码”收录到 seekdb，通过向量语义检索与全文条件过滤找到证据，再生成只使用命中笔记的复习卡。

## 解决的问题

“记得读过，却找不到出处。”页间不只返回相似文本，还保留证据 ID、论文、作者、章节和页码。复习卡只能使用本次检索结果，不补写没有出处的结论。

## 核心功能

- 论文笔记收录：将正文和出处作为同一个知识块持久化写入 seekdb。
- 混合检索：`hybrid_search` 同时执行向量检索、可选全文过滤与 RRF 融合排序。
- 证据回溯：每条结果返回稳定 ID、原文笔记、章节、页码、检索模式和 trace ID。
- 复习卡：以当前问题和命中证据组成卡片，引用可回到本次检索结果。
- 可核验样例：首次启动自动收录 3 篇经典论文的 9 个中文人工转述笔记块，重启不重复导入。

## 技术栈与分工

| 技术 | 实际作用 |
| --- | --- |
| Qoder | 需求拆解、前后端开发、调试、文档编写和 Qoder Sites 发布 |
| seekdb / pyseekdb | 笔记持久化、向量索引、全文过滤和 RRF 混合检索，是核心检索底座 |
| FastAPI | 本地 API、数据校验、检索 trace 和静态应用托管 |
| React + TypeScript + Vite | 检索台、出处卡和复习卡界面 |

## 目录

```text
qoder/
├── backend/
│   ├── app/              # FastAPI、seekdb 仓储与内置笔记
│   └── scripts/smoke.py  # 无 pytest 依赖的端到端验证
├── docs/
│   └── QODER.md          # Qoder 开发记录
├── src/                  # React 工作台
├── requirements.txt
├── .env.example
└── package.json
```

## 运行环境

- Ubuntu 22.04+ 或 Windows 11 的 Ubuntu / WSL2（seekdb 后端）
- Python 3.11+
- Node.js `^22.13.0 || >=24.0.0`
- npm 10+
- 首次安装 seekdb 或其嵌入模型时可能需要联网；模型下载与初始化耗时取决于网络

以下命令均在 `qoder` 目录执行。Windows 用户在 Ubuntu / WSL2 终端中进入对应目录。

## 从安装到启动

### 1. 安装前端依赖

```bash
npm ci
```

### 2. 创建独立 Python 环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

若系统 Python 缺少 `venv`/`pip`（部分精简版 Ubuntu），可用 Miniconda 代替：

```bash
~/miniconda3/bin/conda create -y -n litseek python=3.12
~/miniconda3/envs/litseek/bin/python -m pip install -r requirements.txt
```

如需指定持久化目录，可设置 `SEEKDB_PATH`；默认位置见 `.env.example`。**在 WSL 中该目录必须使用原生 Linux 路径（如 `/home/<user>/litseek-seekdb`），不能放在 `/mnt/c` 上**，否则嵌入式 SeekDB 无法打开数据文件。本项目默认不需要任何 API Key。

### 3. 启动真实检索后端

```bash
source .venv/bin/activate            # 或使用上面的 conda 环境
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787
```

首次启动会创建 Collection 并写入样例笔记。另开终端检查：

```bash
curl http://127.0.0.1:8787/api/health
```

预期返回包含 `"engine":"seekdb"`，且笔记数量不少于 9。

### 4. 启动前端

```bash
npm run dev
```

打开 Vite 输出的本地地址（默认 `http://127.0.0.1:5173`）。页面顶部应从“样例演示”切换为“seekdb 已连接”。Vite 将 `/api` 转发到 `127.0.0.1:8787`。

也可以先执行 `npm run build`，再仅启动 FastAPI，由后端托管 `dist/` 中的静态应用。

## 接口合同

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 返回 `engine="seekdb"` 与集合名 |
| `GET /api/stats` | 集合名、知识块数、论文数、内置笔记数 |
| `GET /api/papers` | 按论文分组的笔记、作者、年份与来源 |
| `POST /api/search` | 入参 `query`、`mode`（`hybrid`/`semantic`）、`keyword`、`n_results`；返回 `trace_id`、`duration_ms`、`engine`、`mode` 与带出处的 `results` |
| `POST /api/documents` | 入参 `title`、`authors[]`、`year`、`section`、`page`、`body`；成功返回 201 与新增知识块 |
| `POST /api/cards` | 入参 `trace_id`、`question`、可选 `evidence_ids`；只使用该次检索保存在服务端的证据，生成抽取式复习卡并附可回溯引用 |

`POST /api/cards` 会校验引用是否属于该次检索：过期或未知 `trace_id` 返回 404，引用外部证据 ID 返回 400。

### 关于“混合检索”的实现说明

实测中 `pyseekdb 1.4.0` 接受 `hybrid_search` 的 `where_document` 参数，但并不真正按关键词过滤结果。因此本项目没有把它当作已生效的能力，而是：

1. 向量检索与 RRF 排序完全交给 seekdb 的 `hybrid_search`；
2. 关键词约束在服务层强制校验——先取更宽的候选集（`n_results × 5`，上限 50），再只保留正文包含关键词的知识块，最后截断到 `n_results`；
3. 不匹配时如实返回 0 条证据，不放宽条件凑数。

`backend/scripts/smoke.py` 对该行为有明确断言：不匹配关键词必须返回空结果。

## 验证样例

### 样例 A：语义检索与出处

1. 保持关键词辅助为空。
2. 输入“Transformer 为什么能并行训练？”并点击“检索证据”。
3. 预期命中 `Attention Is All You Need` 的模型结构笔记。
4. 检查返回模式为语义检索，结果包含证据 ID、章节、页码、trace ID 和耗时。
5. 点击结果，在出处卡中核对同一篇论文及对应笔记。

### 样例 B：全文过滤与 RRF 混合检索

1. 输入“怎样并行建立序列中任意位置的联系？”。
2. 关键词辅助填写“自注意力”。
3. 预期命中 Attention 论文的摘要与顺序计算笔记，模式为 `hybrid`，且结果中包含 `attention-note-01`。
4. 不含“自注意力”的知识块不应通过全文过滤条件。

### 样例 C：复习卡引用边界

1. 在样例 A 或 B 的结果页点击“生成复习卡”。
2. 翻开卡片，核对正文确实来自本次结果。
3. 核对论文、章节和页码，没有新增无出处事实。

### 样例 D：收录与持久化

1. 在真实模式下点击“收录论文笔记”，填写完整出处和正文。
2. 提交后使用正文中的独特词作为关键词检索，应能找到新知识块。
3. 重启后端，再次检索；数据应仍存在，内置样例不应重复。

### 一键 smoke 验证

后端依赖安装完成后运行：

```bash
python backend/scripts/smoke.py
```

脚本覆盖初始化、真实语义检索、混合检索、复习卡引用和重启后的持久化，输出每项检查及检索 trace。任何检查失败都会以非零状态退出。

## Qoder Sites 与线上展示

项目网站：https://litseek-research-p9tfgvczz96.qoder.zone （已设置任何人都可访问）。

代码仓库：https://github.com/24-long/powercontext-2026/tree/2026-competition-ecosystem/qoder
（分支 `2026-competition-ecosystem`，提测时填写该分支的最新 commit hash。仓库需对评审开放访问权限。）

Qoder Sites 托管静态项目网站。由于嵌入式 seekdb 需要 Python 和持久化本地数据目录，线上静态站点默认使用**明确标注的内置样例**，不把它冒充实时检索。评审按上述步骤即可在本地体验真实 seekdb 核心链路。

如另行将 FastAPI 部署为 HTTPS 服务，可在构建时设置：

```bash
VITE_API_BASE_URL=https://你的检索服务域名 npm run build
VITE_REPOSITORY_URL=https://你的代码仓库地址 npm run build
```

前端只保存公开服务地址，不在浏览器中存放密钥。公开写入接口前应补充访问控制。

## 数据与开源说明

- 内置内容是针对 `Attention Is All You Need`、`BERT` 和 `Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks` 的人工中文学习笔记，不是论文全文或官方译文。
- Qoder Sites React starter、Shadcn UI 和 Tailwind 来源及许可说明见 `PROVENANCE.md` 与 `vendor/`。
- 其他依赖保留各自许可证。

## 提测前仍需由参赛者补充

- 公开网站地址、代码仓库地址与 Qoder 账号 UID。
- `docs/QODER.md` 中关键开发环节的对话截图或录屏。
- 如提交真实使用效果，附上可核对来源并隐去个人信息。
