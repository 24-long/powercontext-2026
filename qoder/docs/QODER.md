# Qoder 开发记录

- 项目：页间 LitSeek
- 开发工具：Qoder Desktop
- 开发日期：2026-10-26
- Qoder 账号 UID：**请参赛者在提测前填写**

## 1. 需求拆解与方案设计

在 Qoder 中输入完整赛题要求，由 Qoder 协助将题目拆成可运行项目、指定技术接入、验证材料、开发记录和 Sites 发布五部分。

采用“文献复盘助手”方向，确定最小完整闭环：

> 收录论文笔记 → seekdb 混合检索 → 查看原文出处 → 生成有引用的复习卡

通过 Qoder 的架构规划 Agent 比较 seekdb 与 PowerContext 的接入方式，选择嵌入式 pyseekdb，避免把“安装组件或测试连接”当作核心功能。确定 Qoder Sites 负责静态展示网站，Ubuntu / WSL2 中的 FastAPI 负责真实检索。

## 2. 前端主要开发

Qoder 初始化 Sites React starter，并完成以下主要工作：

- 设计墨绿、暖白和橙色的“研究台纸张”视觉方向。
- 实现检索台、文献筛选、证据列表、出处卡、复习卡和项目验证页。
- 增加连接状态：真实服务可用时显示“seekdb 已连接”，否则明确标注“样例演示”。
- 实现笔记收录表单、复制引用、空结果和请求异常状态。
- 添加响应式布局、键盘焦点和中文页面元信息。

关键需求提示摘录：

> 首页直接就是检索工作台，不做只有宣传内容的落地页。线上没有后端时必须明确区分内置样例与真实检索，不能把静态数据冒充 seekdb 结果。

## 3. seekdb 后端开发

通过 Qoder Agent 独立实现后端模块，主对话负责集成与复核。后端工作包括：

- 使用 pyseekdb 嵌入式 Client 和 `literature_notes` Collection。
- 封装笔记持久化、向量检索、全文过滤和 RRF 混合排序。
- 提供健康检查、文献列表、检索、收录和复习卡接口。
- 为每次检索返回 trace ID、实际耗时、检索模式和证据 ID。
- 内置 3 篇论文的 9 个人工转述知识块，避免首次运行无数据。
- 编写不依赖 pytest 的 smoke 脚本，验证真实检索和重启持久化。

官方接口核对内容包括 `Client(path=...)`、`collection.add(...)` 和 `collection.hybrid_search(query=..., knn=..., rank={"rrf": {}}, ...)`。

## 4. 调试与验证记录

| 环节 | Qoder 参与内容 | 结果 |
| --- | --- | --- |
| 前端依赖 | 执行 `npm ci`，同步项目名与 lockfile | 通过 |
| 类型检查 | 执行 `npm run build`，发现当前 Lucide 版本不提供 GitHub 品牌图标 | 改用通用代码图标后构建通过 |
| 页面结构 | 使用 Qoder 浏览器检查标题、检索表单、证据列表和出处卡 | 通过 |
| 复习卡交互 | 在浏览器点击复习卡并检查翻面前后的内容 | 通过 |
| 样例透明度 | 检查未连接后端时的顶部标识和提示 | 明确显示非实时检索 |
| seekdb 验证 | 安装后端依赖并执行 smoke | 以本文下方“最终验证结果”为准 |
| 视觉截图 | 尝试调用浏览器截图 | 浏览器表面隐藏，未将截图标记为成功 |
| 依赖安全 | 升级 Zod、Vite 和 source-map-js 后执行构建与 `npm audit` | 构建通过，0 个已知漏洞 |
| Qoder Sites | 准备生产产物、发布并修改公开访问策略 | 已发布；公开策略为 `public` |
| 未登录访问 | 直接请求线上地址并检查浏览器 DOM | 页面标题、检索台及样例标识正常 |

## 5. 最终验证结果

Ubuntu / WSL2 + Python 3.12 + `pyseekdb 1.4.0.post1` 环境下执行 `backend/scripts/smoke.py` 的真实输出：

```text
PASS persistence: reopened in a second Python process; count=10, seed reimport=0
PASS import: 3 papers, 9 notes
PASS semantic hybrid_search: ids=['attention-note-03', 'bert-note-01', 'attention-note-02', 'rag-note-02'], duration_ms=4390.777
PASS keyword hybrid_search: ids=['attention-note-01']
PASS keyword exclusion: unmatched keyword returns no evidence
PASS cards: citations=['attention-note-03', 'bert-note-01']; foreign evidence rejected
{"status": "PASS", "database_path": "/home/miniob/litseek-db-smoke", "document_count": 10, "database_retained": true}
```

浏览器端真实链路（Windows 浏览器 → Vite 代理 → WSL FastAPI → 嵌入式 seekdb）：

| 检查 | 结果 |
| --- | --- |
| 顶部状态 | 显示“seekdb 已连接”，提示语为真实检索模式 |
| 语义检索 | 命中 `attention-note-01`、`bert-note-02` 等真实笔记块，返回耗时与 trace ID |
| 关键词混合检索 | 关键词“自注意力”只返回 `attention-note-01`，其余不符证据被排除 |
| 复习卡 | 由 `/api/cards` 返回，答案与 `attention-note-01` 正文逐字一致，引用含论文、章节与 `p.1` |
| 依赖检查 | `pip check` 为 `No broken requirements found.` |

### 关键缺陷修复记录

联调时发现 `pyseekdb 1.4.0` 接受 `where_document` 参数却不真正按关键词过滤，关键词形同虚设。修复方式不是掩盖问题，而是：向量检索与 RRF 排序仍由 seekdb 完成，关键词约束改在服务层强制校验（先取更宽候选集再按正文包含过滤），并在 smoke 中加入“不匹配关键词必须返回 0 条”的回归断言。README 与网站说明同步写明这一实现事实。

## 6. 对话截图或录屏清单

请参赛者使用同一 Qoder 会话补充以下材料，文件名可放在本目录的 `evidence/` 下：

1. 需求拆解或架构规划对话。
2. 前端主要界面生成与调试对话。
3. seekdb 后端实现及 smoke 运行对话。
4. Qoder Sites 发布过程和账号 UID 页面。

截图仅用于证明开发过程，不替代可运行源码与复现步骤。不要在截图中暴露 API Key、密码或个人联系方式。
