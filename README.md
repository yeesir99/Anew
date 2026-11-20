# Anew — A股公开研究数据与 A-Share Alpha Radar 公共展示

This repository runs the public research collector and static dashboard entirely on GitHub Actions. It does not require a NAS, private repository token, or external deployment credential.

## Cloud runtime

- Workflow: `.github/workflows/cloud-public.yml`
- Schedule: weekdays at 19:35 China Standard Time, after the close; manual dispatch and relevant source pushes are also supported.
- Runtime: ephemeral GitHub-hosted Ubuntu runner; Python dependencies installed per run.
- Collection: AKShare public A-share spot data and optional liquid-stock historical technical indicators.
- Output: versioned public CSV/Markdown history in `data/` and `reports/`; static JSON in `site/data/`.
- Deployment: same-repository `GITHUB_TOKEN` publishes `site/` to `gh-pages`; no cross-repository token is required.
- Dashboard: https://yeesir99.github.io/Anew/

## Privacy boundary

This repository and Pages site are public. Only public-market data, public announcements/news, and public research reports belong here. Personal holdings, quantities, cost basis, account data, private notes, manual evidence records, and portfolio-derived reports must remain in the private repository/private storage and must never be committed to this repository. The site builder reads only `data/latest_market.csv`; it does not read portfolio or private database tables.

## Data quality and limitations

The workflow fails rather than publishing if the full-market snapshot has fewer than 2,500 valid stocks or required fields are absent. Snapshot freshness is displayed explicitly; stale snapshots are labeled stale rather than silently represented as current. AKShare's spot endpoint is a public EastMoney-backed endpoint and is not an independent market-data source. Provider failures and rate limits can delay collection.

Turnover is trading activity, not net capital inflow. Public flow estimates are not institutional account-level flows. ETF share changes are not institutional net purchases. Technical indicators on the liquid-stock subset are not full-market historical coverage.

## Product scope and unfinished research modules

Market breadth, leaders/laggards, turnover, and selected technical indicators can be produced from the current public feed. Industry taxonomy/history, evidence-backed theme scoring, multi-period industry rotation, independently sourced capital-flow verification, and a complete 12-section research report require additional validated data sources and tests. The dashboard deliberately displays these as unavailable rather than inventing values.

The private repository's temporary PostgreSQL service is ephemeral and is not a persistent personal-data store. Personal portfolio/evidence migration and encrypted private dashboard storage remain separate tasks; preserve the NAS database and backups until a secure private-data migration is completed and verified. Do not delete or shut down the NAS as part of this public pipeline change.

Research support only; not investment advice.
