# 验证材料清单

本目录用于存放可核对的证据材料。所有截图应为本次 Qoder 会话或本地实际运行结果，隐去与赛题无关的个人信息（手机号、邮箱、令牌等）。

| 文件名 | 内容 | 获取方式 |
| --- | --- | --- |
| `qoder-account-uid.png` | Qoder 个人资料中的「身份 → 账号 ID」，用于核对站点归属 | 已包含 |
| `01-requirement-decomposition.png` | Qoder 中拆解赛题需求、确定“收录笔记 → 混合检索 → 出处回溯 → 复习卡”方案的对话 | 在 Qoder 会话中截图 |
| `02-frontend-development.png` | Qoder 中实现检索台、出处卡与复习卡界面的对话（含一次类型报错修复） | 在 Qoder 会话中截图 |
| `03-seekdb-backend.png` | Qoder 中实现 pyseekdb 仓储、API 合同与 smoke 脚本的对话 | 在 Qoder 会话中截图 |
| `04-seekdb-verification.png` | smoke 真实运行输出：语义检索、关键词混合检索、卡片引用边界、重启持久化 | 在终端运行 `python backend/scripts/smoke.py` 后截图 |
| `05-sites-publish.png` | Qoder Sites 中准备、发布并设置为“任何人都可访问”的过程 | 在 Sites 面板中截图 |
| `06-site-analytics.png` | 站点「访问统计」中的访问次数与独立访客 | 发布一段时间后在 Sites 面板中截图 |

补充说明：

- `04-seekdb-verification.png` 的预期输出见 `../QODER.md` 第 5 节，可直接对照。
- 站点地址：https://litseek-research-p9tfgvczz96.qoder.zone 。
- 访问统计接口在发布当日返回不可用，因此本文档不预先填写访问数字，请以 `06-site-analytics.png` 中的真实数据为准。
