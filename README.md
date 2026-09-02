# carbon-factor-canary

以真实用户视角持续运行 [carbon-factor-matcher](https://pypi.org/project/carbon-factor-matcher/)，在付费用户报障之前发现"用户端才有的问题"。

**背景**：两次线上事故（v2.3.3 macOS 只有 `python3` 没有 `python`；v2.3.4 之后 mcp SDK 2.x 移除 `mcp.server.fastmcp` 导致新装用户全部启动即崩）都是同一个盲区——**开发机（Windows + 长期不重建的 venv）永远执行不到用户的首跑路径**。本仓库就是那条路径的常驻哨兵。

## 两档监控

| | fresh-install-canary | user-journey-smoke |
|---|---|---|
| 频率 | 每 6 小时 | 每日 + 手动触发 |
| 系统 | ubuntu | ubuntu + macos + windows |
| 动作 | 全新 venv 装 PyPI 最新版 → import | npm 启动器拉起 → MCP stdio 握手 → 真实工具调用 |
| 抓什么 | 依赖解析破坏、上游 SDK breaking change（当天发现） | 启动崩溃、启动器回归、API 故障、**Pro 功能静默降级** |

## Pro 断言（防"付费功能悄悄变差"）

user-journey 用真实 Pro license 调 `factor_match`，断言：

- 返回含 `candidates`（而非 Free 的 `factors`）——混合检索真的在工作
- 候选含 `hybrid_score` / `final_score`——混合评分没退化
- 无 `upgrade_hint`——Pro 身份被正确识别

## 告警

GitHub Actions 定时任务失败会自动给仓库所有者发邮件。建议仓库保持 **public**（无源码、只有监控脚本；license key 走 Secrets）——public 仓库 Actions 分钟数免费。

## 初次设置

```bash
# 1. 在 GitHub 建仓库并推送
git remote add origin git@github.com:nikeandocean/carbon-factor-canary.git
git push -u origin main

# 2. Settings → Secrets and variables → Actions
#    添加 CARBON_FACTOR_LICENSE_KEY = PRO-xxxx（真实生产 key）

# 3. Actions 页手动 Run workflow 两个工作流各跑一次验证
```

## 本地运行

```bash
# 档 1：全新安装冒烟（~5 分钟，会装 torch）
python scripts/fresh_install_check.py

# 档 2：用户旅程（-- 后面是任意"用户会怎么启动"的命令）
python scripts/smoke_user_journey.py -- npx -y @nikeandocean/carbon-factor-matcher
# 或指向本地已有环境：
python scripts/smoke_user_journey.py -- python -m carbon_factor_matcher
```

脚本仅用标准库，不依赖 MCP SDK——canary 必须独立于它所监控的生态。

**License 说明**：key 校验是远程的（查 R2 key 注册表），假 key 会让服务器启动即崩（脚本会 fail-fast 并打印崩溃栈）。因此本地不设 `CARBON_FACTOR_LICENSE_KEY` 时只验证 Free 路径并跳过 Pro 断言；完整 Pro 验证在 GitHub Actions 上用 Secrets 里的真实 key 跑，或本地 `export CARBON_FACTOR_LICENSE_KEY=PRO-xxxx` 后再跑。

CN 网络下本地运行提示：模型加载默认会联网到 HuggingFace 做版本检查，无代理会长时间挂起。本地调试可加 `HF_HUB_OFFLINE=1`（模型已在本地缓存时）或给进程配代理；Actions 海外机器不受影响。
