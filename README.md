# 股迹 · StockTrail

> 工程代号 **GRGPZS**（目录、包名与历史文档中的代号沿用，不影响产品名）。
> 面向个人 A 股投资者的**本地优先**工具：把「信息收集 → 汇总摘要 → 决策参考 → 交易复盘」串成一个闭环。
> 每天一份**晚间主报** + 一页**盘前增量**；建议是「信号 + 条件」，不是买入指令；每笔交易**事前留痕、事后归因**。

> ⚠️ 本项目为**开发中**状态：M0「两周自用版」已完成并通过评审，M0 后的功能在按价值密度逐步推进（详见 [开发进度](#3-开发进度)）。
> 所有输出仅供个人研究，不构成任何投资建议。

---

## 1. 产品是什么

| 维度 | 说明 |
|---|---|
| 形态 | 本地 CLI + 本地网页（FastAPI，只监听 127.0.0.1）；日报以 Markdown 输出，可直接用 Obsidian 阅读 |
| 目标用户 | 单用户（作者本人）：无多租户、无云端账号 |
| 核心问题 | 信息分散、噪音多、决策凭感觉、复盘缺失 |
| 核心交付 | ① 晚间主报（市场概况 / 账户体检 / 自选异动 / 昨日验证 / 条件式明日计划）② 盘前增量页 ③ 交易留痕与纪律指标 |
| 明确不做 | 自动交易、荐股、指令式买卖、盘中能力（唯一例外：持仓跌破事前设定的止损位时提示） |

产品设计的硬约束（企划 v1.0 冻结基线）：

1. **行为护栏优先**：优化目标是「改善交易行为」（按计划执行率↑、非计划交易↓、持股天数↑），不是使用频率；
2. **事前记录是复盘可信的前提**：买入理由盘前锁定，T+1 后不可改，事后补录会从归因样本中剔除；
3. **不做无法验证的计算**：无加权总分、无星级权重；
4. **前瞻结论必须可验证**：任何进入日报的预测都必须带 `prediction` 与事前写入的判据；
5. **机器负责计算，LLM 只做可溯源压缩**。

---

## 2. 产品结构

### 2.1 五个功能模块

| 模块 | 职责 | 主要实现 |
|---|---|---|
| M1 信息收集 | 行情、指数、涨跌家数、板块、新闻、公告采集 | `app/collectors/`（akshare / 同花顺 / 财联社 / 全球指数） |
| M2 质量层 | 复权处理、分板块涨跌停阈值、交易日历、字段稳定性 | `app/quality/checks.py`、`config/quality_rules.yaml`、`data/calendar/` |
| M3 汇总摘要 | 生成晚间主报与盘前增量页 | `app/summarize/render_daily.py` + `templates/daily.md.j2`、`scripts/run_daily.py`、`daily_increment.py` |
| M4 决策参考 | 信号清单（无加权总分）、热点/异动/板块观察、账户体检 | `app/analyzer/`（hotspots / anomalies / sectors / positions / portfolio_health / news） |
| M5 交易复盘 | 盘前承诺、理由锁定、昨日验证、价值验收、纪律指标 | `scripts/note.py`、`verify_yesterday.py`、`read.py`、`report_read.py`、`app/review/stats.py` |

### 2.2 本地网页界面（P-13）

`scripts/web_ui.py` 启动后打开 `http://127.0.0.1:8000`，页面包括：

- 日报（按日期查看；盘中 9:30–15:00 拒绝生成）
- `/watchlist` 自选池增删改（代码↔名称自动补全）
- `/positions` 持仓入口（交易记录推算 + 手动补充）
- `/record` 交易操作记录（复盘信息 + 股票信息含「未买卖」+ 历史记录折叠）
- `/strategy` 交易策略库
- `/news` 新闻快讯（财联社，只存标题/时间/链接）
- `/account` 账户投入与仓位占比
- 设置页（LLM 开关与模型）

### 2.3 目录结构

```
stocktrail/
├── README.md                          # 本文件
├── GRGPZS-...-企划与设计总览.md        # 单文件版企划（605 行，v1.0 冻结基线）
├── requirements.txt
├── app/
│   ├── collectors/                    # base / akshare_src / ths_src / cls_src / global_src
│   ├── quality/checks.py              # 质量检查（分板块阈值 + 复权豁免）
│   ├── analyzer/                      # hotspots / anomalies / sectors / positions /
│   │                                  # portfolio_health / news
│   ├── llm/client.py                  # LLM 调用（可关）
│   ├── summarize/                     # render_daily.py + templates/daily.md.j2
│   └── review/stats.py                # 纪律指标
├── scripts/
│   ├── run_daily.py                   # 晚间主报
│   ├── daily_increment.py             # 盘前增量页
│   ├── init_db.py                     # 三张表初始化
│   ├── note.py                        # 盘前承诺 / 成交录入 / 补理由 / 验证
│   ├── read.py / report_read.py       # 价值验收与跳过率报告
│   ├── verify_yesterday.py            # 昨日验证流水线
│   ├── news_fetch.py                  # 财联社快讯抓取（只存标题/链接）
│   ├── hotspot_conclusions.py         # 热点结论
│   ├── report_hits.py                 # 命中率统计
│   ├── import_csv.py                  # 成交对账导入
│   ├── data_test.py                   # 数据源可用性 + 字段稳定性
│   ├── setup_schedule.py              # 注册 Windows 计划任务
│   ├── stop_watch.py                  # 盘中止损提醒（P-01）
│   ├── web_ui.py                      # 本地网页界面
│   ├── notify_failure.ps1             # 计划任务失败通知
│   └── _selftest_*.py                 # 各功能点自测脚本
├── config/
│   ├── data_sources.yaml              # 数据源与抓取范围
│   ├── quality_rules.yaml             # 质量规则
│   ├── signal_rules.yaml              # 信号规则
│   ├── verify_rules.yaml              # 验证判据
│   ├── strategy.yaml                  # 策略配置
│   ├── llm.yaml                       # LLM 开关/模型/地址
│   └── secrets.example.yaml           # ★ 凭据模板（复制为 secrets.yaml 后填）
├── data/
│   ├── inputs/                        # watchlist / positions / trades / conclusions / account
│   ├── strategies/                    # 策略库（价值投资、趋势跟踪等）
│   └── calendar/                      # 交易日历
└── docs/
    ├── 01-产品开发企划.md
    ├── 02-开源方案调研.md
    ├── 03-技术架构设计.md
    ├── 04-开发路线图.md
    ├── 05-移动端发展规划.md
    ├── 06-M0数据积累使用指南.md
    ├── 07-新闻与异动信息规划.md
    ├── 00-README-原始版.md             # 早期 README（保留作历史）
    └── dev/                            # 开发文档空间
        ├── 01-双模型开发评审工作流.md
        ├── 02-M0功能点开发规格与评审检验.md
        ├── 03-M0后功能点开发规格与评审检验.md
        ├── 04-评审报告模板.md
        ├── 变更申请.md
        └── reviews/                    # 每个功能点的自测 + 评审报告（30+ 份）
```

### 2.4 数据流

```
数据源（同花顺 / akshare / 财联社 / 巨潮）
      ↓ 采集 + 质量检查（复权、阈值、交易日历）
   SQLite（三张表）+ 本地缓存
      ↓ 信号引擎 / 账户体检 / 板块与异动
   daily.md（晚间主报）+ increment.md（盘前增量）
      ↓ Obsidian / 本地网页阅读
   交易留痕（note / record）→ 昨日验证 → 纪律指标
      ↓
   行为反馈（按计划执行率、非计划交易、持股天数）
```

---

## 3. 开发进度

> 状态口径：`done` = 完成并通过评审；`conditional` = 带条件通过；`in_review` = 已实现、评审中；`pending` = 未开始。

### 3.1 功能进度（M0 里程碑）

| ID | 功能 | 状态 |
|---|---|---|
| F-01 | 仓库骨架与配置（四件 config + 三表 init_db） | done |
| F-02 | akshare 采集（后复权/指数/涨跌家数/成交额） | done（带条件：东财恢复后复核主路径） |
| F-03 | 质量检查三条（分板块阈值 + 复权豁免） | done |
| F-04 | data_test.py + 字段稳定性清单 | done |
| F-05 | 账户体检计算 | done |
| F-06 | daily.md 渲染（行为护栏三要素 + 五段） | done |
| F-07 | note.py 盘前预先承诺（T+1 锁定 / post_hoc） | done |
| F-08 | read.py 价值验收记录 | done |
| F-09 | 昨日验证流水线（prediction 必填 / 判据入 config） | done |
| F-10 | 盘前增量附页（全球接口 + T-1 降级） | done |
| F-11 | 财联社接入（白名单 + 只存标题/链接） | done（CHG-014） |

### 3.2 功能进度（M0 之后，按价值密度推进）

| ID | 功能 | 状态 |
|---|---|---|
| P-01 | 盘中止损位提醒（C10 唯一例外） | done |
| P-02 | 热点段 + 完整验证流水线 | done |
| P-06 | Windows 计划任务自动化 | done |
| P-12 | 同花顺行情数据源（默认日线主源） | done |
| P-13 | 本地网页界面（日报/记录/账户/LLM/策略库/自选） | in_review（带条件通过，待用户首用验证） |
| P-03 起其余功能点 | 周报、移动端、归因等（见 `docs/dev/03` 价值清单） | pending（排队，不预先排期） |

### 3.3 代码进度

- Python：**42 个文件，约 5,300 行**（`app/` + `scripts/`，不含测试与文档）
- 分层：采集 / 质量 / 分析 / 摘要 / 复盘 / LLM 六层已落地，模块边界清晰
- 网页层：单文件 `web_ui.py`（约 1,400 行）承载 7 个页面，属于 M0 阶段的务实实现；后续如继续演进，建议拆分路由与模板
- 已知代码债：
  - 全部为同步 urllib 调用，无统一 HTTP 客户端与重试中间件
  - `data/processed` 与 `data/inputs` 是文件 + SQLite 混合存储，无迁移机制
  - LLM 调用只在 P-13 使用，失败时降级为规则摘要

### 3.4 测试进度

| 测试类型 | 现状 |
|---|---|
| 自动化单测（pytest） | **无** |
| 功能点自测脚本 | 有 5 个：`scripts/_selftest_*.py`（F-02、CHG-010/012/014、P-13 等） |
| 双模型评审 | 有完整落档：`docs/dev/reviews/` 下 30+ 份「自测 + 评审」报告，逐条检查代码/数据/行为/一致性 |
| 端到端验收 | M0 退出评审**未完成**：需连续 20 个交易日生成成功率 ≥ 90%、read 数据与段落裁剪验证 |
| 数据源契约测试 | `scripts/data_test.py` + `field_stability`（人工核对字段） |

**结论：测试以「脚本自测 + 人工评审」为主，缺少可重复执行的自动化测试套件**，这是当前最大的工程质量缺口。

---

## 4. 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置凭据（见第 5 节）
copy config\secrets.example.yaml config\secrets.yaml   # Linux/macOS: cp

# 3. 初始化三张表
python scripts/init_db.py

# 4. 生成晚间主报（默认同花顺主源，失败自动降级 akshare）
python scripts/run_daily.py
#    指定免费源：python scripts/run_daily.py --source akshare

# 5. 盘前增量页
python scripts/daily_increment.py

# 6. 本地网页界面
python scripts/web_ui.py    # 浏览器打开 http://127.0.0.1:8000
```

常用命令：

```bash
python scripts/note.py buy 600519 --reason 趋势 --target 1700 --stop 1500   # 盘前承诺
python scripts/note.py buy 600519 --price 1510 --shares 100 --reason 趋势   # 成交录入
python scripts/note.py verify --date 2026-08-31                             # 计划 vs 成交
python scripts/read.py 市场概况 有用                                        # 价值验收
python scripts/verify_yesterday.py --date 2026-08-27                        # 昨日验证
python scripts/data_test.py                                                 # 数据源体检
python scripts/setup_schedule.py                                            # 注册计划任务
```

---

## 5. 密钥与配置（重要）

仓库**不包含任何真实密钥**。首次使用需自行申请并填入 `config/secrets.yaml`（该文件已被 `.gitignore` 忽略）。

### 5.1 同花顺 Fuyao（行情主源，可选）

1. 打开 <https://fuyao.aicubes.cn/>，用**同花顺账号**登录；
2. 进入「API Key 管理」页：<https://fuyao.aicubes.cn/admin>；
3. 点击 **创建 API Key**，填一个别名（例如 `grgpzs`）；
4. 弹窗里的完整 Key **只显示一次**，立刻保存；
5. 填入 `config/secrets.yaml`：

```yaml
ths:
  api_key: "sk-fuyao-你的Key"
  base_url: "https://fuyao.aicubes.cn"
  auth_header: "X-api-key"
```

> 不填也能跑：`config/data_sources.yaml` 里把 `ths.enabled` 设为 `false`，会走免费的 akshare。
> 已知限制：当前 Key 权限不含资金流接口（返回 2004）；资讯事件库不对外开放。

### 5.2 DeepSeek（可选，仅 P-13 的 LLM 分析段使用）

1. 打开 <https://platform.deepseek.com/api_keys>；
2. 创建 API Key 并保存；
3. 填入 `config/secrets.yaml` 的 `llm.api_key`，或在 `config/llm.yaml` 里把 `enabled` 设为 `false` 关闭。

### 5.3 财联社（可选）

`app/collectors/cls_src.py` 只抓取**标题 / 时间 / 链接**，不落正文。该源属灰色区域：**仅限个人本地自用，不要分发或二次发布**。不使用可关闭。

### 5.4 网页端

`web_ui.py` 只监听 `127.0.0.1`，不要暴露到公网；无鉴权设计。

---

## 6. 数据与隐私

- `data/inputs/` 中的 `watchlist.yaml` / `positions.yaml` / `trades.csv` 是**示例**，请换成自己的数据；
- 真实持仓、成交、日报不要提交到公开仓库（`.gitignore` 已忽略 `data/processed/` 与 `data/raw/`）；
- 仓库不含任何 API Key、账户金额、真实交易记录。

---

## 7. 已知限制 / 未完成项

1. **M0 退出评审未做**：连续 20 交易日成功率 ≥ 90% 尚未验证；
2. **无自动化测试**：目前依赖自测脚本 + 人工评审；
3. **东财主路径待复核**：F-02 的带条件通过项，需东财恢复后补做；
4. **移动端未开始**：`docs/05` 只有规划（Capacitor 套壳方案）；
5. **单用户假设**：无登录、无多账户、无并发控制；
6. **Windows 优先**：计划任务、路径分隔符等按 Windows 编写，跨平台未验证；
7. **LLM 段为可选增强**：关闭后日报仍可正常生成（规则摘要）。

---

## 8. Roadmap（按价值密度，不预先排期）

- [ ] 补齐 M0 退出评审数据（20 交易日）
- [ ] 引入 pytest 单测（质量规则、验证判据、note 状态机优先）
- [ ] 网页层拆分：路由 / 模板 / 静态资源分离
- [ ] 移动端 MVP（只读日报 + 记录，不做盘中）
- [ ] 归因模块（样本 ≥ 150 笔后开启）

---

## 9. 文档索引

| 文档 | 内容 |
|---|---|
| [企划与设计总览](GRGPZS-A股信息聚合与交易决策辅助工具-企划与设计总览.md) | 单文件版全量企划（v1.0 冻结基线） |
| [docs/01-产品开发企划.md](docs/01-产品开发企划.md) | 背景、定位、行为护栏、5 大模块、M0 范围、验收标准 |
| [docs/02-开源方案调研.md](docs/02-开源方案调研.md) | GitHub 开源方案调研与借鉴点对照 |
| [docs/03-技术架构设计.md](docs/03-技术架构设计.md) | 技术架构、信号清单引擎、质量规则、Schema |
| [docs/04-开发路线图.md](docs/04-开发路线图.md) | M0 → 按价值密度推进 |
| [docs/05-移动端发展规划.md](docs/05-移动端发展规划.md) | 安卓/iOS 渐进路线（提案留档） |
| [docs/06-M0数据积累使用指南.md](docs/06-M0数据积累使用指南.md) | 每日操作流程（盘前/盘中/盘后/周月） |
| [docs/07-新闻与异动信息规划.md](docs/07-新闻与异动信息规划.md) | 新闻与异动信息的设计 |
| [docs/dev/](docs/dev/) | 双模型评审工作流、功能点规格、评审报告 |

---

## 10. 许可

个人项目，默认 MIT。第三方数据源请遵守各自 License 与使用条款；财联社内容仅限个人本地自用。
