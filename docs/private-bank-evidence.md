# 参股民营银行主题：披露证据录入规范

文件：`data/private_bank_equity_evidence.json`

此文件只接受人工复核过的上市公司原始披露，不接受 THS/东方财富概念标签、新闻转载或未经核实的候选名单。空的 `records` 数组是有效状态，表示目前没有可核验成分，不代表主题没有相关公司。

每条记录必须包含：

- `listed_company_code`：6位上市公司证券代码
- `investee_bank_name`：被投资的民营银行名称
- `relationship_type`：`equity_investment`、`shareholding`、`股权投资` 或 `持股`
- `holding_pct`：大于0且不超过100的持股比例
- `disclosure_date`：披露文件日期，ISO `YYYY-MM-DD`
- `as_of_trade_date`：该证据纳入研究成分的行情交易日，ISO `YYYY-MM-DD`
- `source_url`：年报、交易所公告或巨潮资讯原始披露的 URL
- `source_type`：`annual_report`、`exchange_announcement` 或 `cninfo_filing`
- `source_excerpt`：足以支持股权关系的原文摘录（至少30字符）

可选字段：`listed_company_name`、`reporting_period`、`shareholder_name`、`notes`、`cross_check_urls`。

`as_of_trade_date` 是本次人工复核并纳入研究的行情交易日，不等同于披露文件日期。若最新明确披露早于该交易日，必须在 `notes` 中说明所用报告期、最近一次明确披露日期，以及后续定期报告是否更新；不得把旧披露伪装成当日披露。

采集器会拒绝证券代码格式错误、持股比例非法、披露日期晚于 `as_of_trade_date`、交易日不匹配、来源类型不在允许列表或缺少原文摘录的记录。同一上市公司与同一被投资银行的重复记录会去重。

录入人应核对原文、主体名称和持股口径。比例应是上市公司自身持有被投资银行的股权比例；不能把间接持股、关联方持股或新闻中的泛化表述自动折算为直接持股。
