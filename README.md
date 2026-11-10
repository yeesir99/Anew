# Anew — A股每日量化研究与风险监测（免 Token 数据源版）

面向 A 股的每日收盘数据采集、全球财经新闻与公告整理、技术分析、市场异动扫描和风险提示。通过 GitHub Actions 自动运行，报告以 Markdown 保存到 `reports/`，数据保存到 `data/`。

> 本项目是研究工具，不构成投资建议。免费公开接口可能限流、变更或短暂不可用；关键行情与公告应交叉核验。

## 免费数据源

- **A股全市场日终快照**：AKShare 调用东方财富公开行情接口，无需 Tushare Token。覆盖全市场快照字段（价格、涨跌幅、成交量/额、换手率、量比等）。
- **技术指标历史数据**：对成交额排名靠前的股票调用 AKShare 历史日线接口，计算 MA5/10/20/60、RSI14、MACD、20日年化波动率。默认前 300 只，避免对免费端点发起数千次请求。
- **全球财经新闻**：GDELT DOC API，无需 API Key。
- **公告索引**：AKShare 公告接口尽力采集；接口不可用时会记录告警。公告原文请以巨潮资讯和交易所披露页面为准。

## 免 Token 运行

行情、新闻和公告采集不需要 Tushare Token，也不需要设置任何行情 API Key。

可选配置 AI 解读：
- GitHub **Settings → Secrets and variables → Actions → New repository secret** 添加 `OPENAI_API_KEY`。
- 可在 Actions Variables 添加 `OPENAI_MODEL`（默认 `gpt-4.1-mini`）。
- 不配置 AI Key 时仍生成规则化量化报告。

## 技术指标覆盖范围

全市场快照用于市场宽度、涨跌幅、成交额榜和单日异动分析。历史均线、RSI、MACD 等技术指标默认只对成交额排名前 300 的股票计算；其余股票的历史技术字段为空，不会伪造信号。

可通过 workflow 环境变量 `MAX_HISTORY_STOCKS` 调整：
- `300`：默认，较适合免费接口及 Actions 运行时长。
- `0`：尝试覆盖全部股票，耗时较长，可能触发数据源限流。
- `HISTORY_WORKERS`：并发数，默认 4；不建议大幅提高。

## 自动运行时间（北京时间）

- 收盘采集：工作日 19:30（UTC 11:30），交易日历检查后执行。
- 早间报告：每天 08:10（UTC 00:10），读取最近一次收盘数据。
- GitHub Actions 可能排队延迟；可在 Actions 页面手动运行 workflow。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.main close
python -m src.main morning
```

## 数据与报告

- `data/latest_market.csv`：最近一次行情快照。
- `data/latest_news.json`：全球财经新闻。
- `data/latest_announcements.json`：公告索引（可能为空）。
- `reports/YYYY-MM-DD-close.md`：收盘报告。
- `reports/YYYY-MM-DD-morning.md`：晨报。

## 限制与风险

1. AKShare 是数据接口封装库，底层依赖东方财富等公开网页/API；免费服务没有生产级 SLA，接口可能改版、限流或在 GitHub Actions 云环境不可用。
2. 行情快照的“最新交易日”应由接口返回时间及市场状态核验；若在非交易时段/节假日运行，不应将旧快照误认为当天行情。
3. 历史技术指标默认只覆盖成交额前 300 只，不能把该子集的指标覆盖率理解为全市场覆盖。
4. 免费公告接口可用性不保证；公告原文以巨潮资讯（CNINFO）及上交所、深交所、北交所官网为准。
5. GDELT 英文新闻覆盖面不等于完整全球财经新闻数据库，新闻结论应阅读原文核验。
6. 本系统不发交易指令、不自动下单；指标和风险规则仅供研究。

## 参考资料

- AKShare 文档：https://akshare.akfamily.xyz/
- BaoStock 文档（可作为后续历史行情备选）：https://www.baostock.com/
- GDELT DOC API：https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/

