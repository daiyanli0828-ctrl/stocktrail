# GRGPZS · A股信息聚合与交易决策辅助工具

> 项目代号 **GRGPZS**（工作目录名），中文产品名待定（方向改为"留痕/复盘簿"类，候选：「股迹」/「交易留痕簿」/「复盘手账」，见企划文档 §13）。

## 一句话定位

面向个人 A 股投资者的 **「信息收集 → 汇总摘要 → 决策参考 → 交易复盘」** 本地优先工具：每天一份**晚间主报 + 一页盘前增量**（市场概况 / 账户体检 / 自选异动 / 昨日验证 / 条件式明日计划），阅读与组织用 **Markdown + Obsidian**；建议是"信号 + 条件"，不是买入指令；每笔交易**事前留痕、事后归因**（纪律指标优先）——帮用户建立 **信息 → 判断 → 行动 → 复盘 → 改进** 的个人投资闭环，目标是**改善交易行为**，不是提高使用频率。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/01-产品开发企划.md](docs/01-产品开发企划.md) | 主企划（v0.6）：背景、定位、行为护栏、5 大模块、M0 范围、验收标准、待确认事项 |
| [docs/02-开源方案调研.md](docs/02-开源方案调研.md) | GitHub 开源方案调研：数据源 / 舆情 / 日报聚合 / 复盘 / 量化框架，含借鉴点对照表 |
| [docs/03-技术架构设计.md](docs/03-技术架构设计.md) | 技术架构（v0.6）：M0 单脚本、信号清单引擎、质量规则、Schema、Obsidian 展示 |
| [docs/04-开发路线图.md](docs/04-开发路线图.md) | 开发路线图（v0.6）：M0「两周自用版」→ 按价值密度推进（不预先排期） |
| [docs/05-移动端发展规划.md](docs/05-移动端发展规划.md) | 移动端（安卓/iOS）发展规划（提案留档）：云端服务 + 多端前端渐进路线 |
| [docs/06-M0数据积累使用指南.md](docs/06-M0数据积累使用指南.md) | **每日操作流程**（盘前/盘中/盘后/周月）：note/read/验证用法 + 数据积累目标 |
| [docs/dev/README.md](docs/dev/README.md) | **开发文档空间**：双模型开发评审工作流（v4-flash 开发 / v4-pro 评审）、M0 功能点开发规格与评审检验逻辑、评审报告模板 |

## 当前状态

- [x] 产品开发企划（**v1.0 冻结基线**）+ 开源方案调研
- [x] 开发文档空间建立（docs/dev：双模型工作流 + 功能点规格与评审检验）
- [x] **M0「两周自用版」开发完成（F-01~F-10 全部评审通过）**：akshare 单源 → daily.md + 盘前增量页 + note/read/verify
  - 功能点状态看板见 [docs/dev/README.md](docs/dev/README.md)；评审报告见 [docs/dev/reviews/](docs/dev/reviews/)
- [ ] M0 退出评审（第 3 周）：连续 20 交易日生成成功率 ≥ 90% + read 数据 + 段落裁剪（F-02 东财主路径核对待东财恢复后补做）
- [ ] F-11 财联社接入：**用户已拍板暂缓**，按 read 数据第二周末定夺

## 快速开始（M0）

```powershell
# 0. 最省事：本地网页界面（看日报 / 录交易理由 / 记价值验收，全部网页操作）
python scripts/web_ui.py            # 启动后浏览器打开 http://127.0.0.1:8000

# 1. 初始化三张表
python scripts/init_db.py

# 2. 生成晚间主报（读 watchlist.yaml / positions.yaml，写 data/processed/vault/daily-YYYY-MM-DD.md）
python scripts/run_daily.py
#    默认日线/指数/广度主源 = 同花顺官方服务（--source ths，需 config/secrets.yaml 配 key）；
#    失败自动降级 akshare；--source akshare 可显式切回免费开源源

# 3. 盘前增量附页（隔夜美股/A50/日经 + T-1 降级标注）
python scripts/daily_increment.py

# 4. 盘前承诺 / 交易理由（T+1 锁定；也可用网页界面录入）
python scripts/note.py buy 600519 --reason 趋势 --target 1700 --stop 1500   # 盘前承诺
python scripts/note.py buy 600519 --price 1510 --shares 100 --reason 趋势     # 成交录入
python scripts/note.py reason 1 --reason 趋势                                 # 补录理由（>24h 标 post_hoc）
python scripts/note.py verify --date 2026-08-31                               # 计划 vs 成交比对

# 5. 价值验收（每天读完后记录"哪段有用"）
python scripts/read.py 市场概况 有用
python scripts/report_read.py --days 20     # 跳过率报告（>50% 段落删除候选）

# 6. 昨日验证（结论写入 data/inputs/conclusions.yaml，prediction 必填）
python scripts/verify_yesterday.py --date 2026-08-27

# 7. 数据源可用性测试 + 字段稳定性清单
python scripts/data_test.py
```

输入文件（`data/inputs/`）：`watchlist.yaml`（自选池）/ `positions.yaml`（持仓快照）/ `trades.csv`（对账用，理由不由 CSV 导入）/ `conclusions.yaml`（前瞻结论）。

## 数据合规（重要，C15）

- 本项目默认数据源为 **akshare**（免费开源接口）；
- **同花顺官方数据服务（fuyao.aicubes.cn，HiThink-Tech/Financial-API）**：用户自备 API key，`--source ths` 可切换日线主源（凭证存 `config/secrets.yaml`，`.gitignore` 已忽略，勿提交公开仓库）；
- **财联社源属灰色区域**：仅限个人本地自用，**不分发、不二次发布、公开仓库中不包含抓取到的正文**；落库只存标题 / 时间 / 链接 / 自生成摘要；
- 所有第三方数据遵守各自 License 与使用条款，请求频率受限（日频）。

## 目录结构（规划，M0）

```
GRGPZS/
├── README.md
├── docs/
├── config/                # data_sources / quality_rules / verify_rules / signal_rules
├── app/
│   ├── collectors/        #   DataSourceAdapter + akshare_src（全球/财联社可选）
│   ├── quality/           #   质量检查（分板块阈值 + 复权豁免）
│   ├── analyzer/          #   信号引擎 + 账户体检
│   ├── summarize/         #   daily.md 模板渲染
│   └── review/            #   纪律指标统计
├── scripts/               # run_daily / note / read / init_db / data_test
└── data/
    ├── inputs/            # watchlist.yaml / positions.yaml / trades.csv
    ├── processed/         # app.db + daily.md + vault/
    └── calendar/          # 交易日历本地缓存
```

## 免责声明

本项目所有输出（日报、信号、建议、复盘报告）**仅供个人研究与参考，不构成任何投资建议**。项目不做自动交易、不荐股、不输出指令式买卖；**盘中能力永久不做**（唯一例外：持仓跌破你事前设定的止损位时提示）。股市有风险，决策需自行负责。
