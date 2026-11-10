# Anew — A股每日量化研究与风险监测

面向 A 股的每日收盘数据采集、全球财经新闻与交易所公告整理、技术分析、市场热点/异动扫描、量化指标和风险提示。通过 GitHub Actions 自动运行，报告以 Markdown 保存到 `reports/`，结构化数据保存到 `data/`。

> 本项目是研究与信息整理工具，不构成投资建议。指标和规则信号不代表收益承诺；使用者应独立核验数据、公告和交易规则。

## 当前版本能力

- Tushare Pro：交易日历、股票基础资料、A 股日线行情；尝试获取每日基本面指标（接口权限不足时会记录告警并继续）。
- GDELT DOC：全球财经相关新闻检索。
- Tushare `anns_d`：在账号权限允许时获取公告索引；公告原文应以巨潮资讯/交易所披露页面为准。
- 技术指标：收益率、均线、RSI、MACD、成交额变化、波动率、区间高低点。
- 市场扫描：涨跌幅榜、成交额榜、放量异动、行业/概念字段（源数据提供时）。
- 风险规则：大幅波动、放量下跌、价格偏离均线、数据缺失等提示。
- 可选 OpenAI API：生成中文新闻与行情解读；未配置时仍生成规则化报告。

## 初始化配置

在 GitHub 仓库 **Settings → Secrets and variables → Actions → New repository secret** 添加：

| Secret | 必需 | 用途 |
|---|---:|---|
| `TUSHARE_TOKEN` | 是 | A 股行情与交易日历 |
| `OPENAI_API_KEY` | 否 | AI 解读（未配置则跳过） |
| `OPENAI_MODEL` | 否 | 模型名；默认 `gpt-4.1-mini` |

Tushare 不同接口有积分/权限要求。若 `daily_basic` 或 `anns_d` 无权限，程序会给出提示；请在 Tushare 账户开通相应权限。不要把 Token 写入代码、提交到仓库或放入普通变量。

## 自动运行时间（北京时间）

- **收盘采集**：工作日 19:30（UTC 11:30），自动检查交易日历，非交易日跳过。
- **早间报告**：每天 08:10（UTC 00:10），读取最近一次收盘数据并生成晨报。

GitHub Actions 的 cron 使用 UTC。定时任务可能因 GitHub Actions 排队而延迟。首次配置后可在 Actions 页面手动运行 workflow 验证。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export TUSHARE_TOKEN="你的token"
python -m src.main close
python -m src.main morning
```

## 报告与数据

- `data/latest_market.csv`：最近一次成功采集的日线行情。
- `data/latest_news.json`：新闻条目。
- `data/latest_announcements.json`：公告索引（可能为空，取决于权限）。
- `reports/YYYY-MM-DD-close.md`：收盘分析。
- `reports/YYYY-MM-DD-morning.md`：晨报。

Workflow 会把数据与报告提交回 `main`，便于下一次运行读取。请注意这些提交会使仓库持续增长；后续可迁移到对象存储或数据库。

## 数据口径与限制

1. 日线数据以 Tushare 返回为准，交易日历决定是否采集。Tushare 行情通常在收盘后延迟入库，19:30 是预留缓冲时间，不保证数据源准时完成。
2. GDELT 新闻覆盖面和语言质量不均；搜索结果不是完整新闻数据库，需阅读原文核验。
3. 公告索引依赖 Tushare 权限；公告内容应跳转巨潮资讯或交易所官网核对。本项目不绕过登录、验证码或付费权限。
4. 技术指标基于日线历史数据；当前版本不做盘中实时行情、不发交易指令，也不自动下单。
5. 行业/概念聚合取决于源数据字段可用性；没有可靠分类字段时不会伪造行业结论。

## 项目结构

```
.github/workflows/       定时任务
src/
  collectors/            行情、新闻、公告采集
  processors/            指标、扫描与风险规则
  reports/               报告生成
  main.py                命令入口
data/                    持久化数据
reports/                 Markdown 报告
```
