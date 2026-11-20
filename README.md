# Anew — A股公开研究数据与 A-Share Alpha Radar 公共展示

This repository runs the public research collector and static dashboard entirely on GitHub Actions. It does not require a NAS, private repository token, or external deployment credential.

## Cloud runtime

- Workflow: `.github/workflows/cloud-public.yml`
- Schedule: weekdays at 19:35 China Standard Time, after the close; manual dispatch is also supported.
- Runtime: ephemeral GitHub-hosted Ubuntu runner; Python dependencies installed per run.
- Market collection: Sina-first AKShare A-share snapshot, with EastMoney as a last-resort source; historical bars prefer Sina/Tencent alternatives before EastMoney.
- Research collection after a validated live close: daily industry snapshots, public sector-flow estimates, and exchange ETF-share snapshots.
- Durable public history: daily CSV snapshots committed to this public repository; industry momentum backtest begins after sufficient same-source history accumulates.
- Output: public JSON dashboard data and a 12-section research note in `site/data/` and `reports/`.
- Deployment: same-repository `GITHUB_TOKEN` publishes `site/` to `gh-pages`.
- Dashboard: https://yeesir99.github.io/Anew/

## Privacy boundary

This repository and Pages site are public. Only public-market data, public announcements/news, and public research reports belong here. Personal holdings, quantities, cost basis, account data, private notes, manual evidence records, and portfolio-derived reports must remain in the private repository/private storage and must never be committed here. Public collectors do not read the private repository or private database.

## Data quality and limitations

The workflow refuses to publish a market snapshot with fewer than 2,500 valid stocks or missing required fields. Snapshot freshness is displayed explicitly; stale snapshots are labeled stale. The public archive's upstream is EastMoney and is not an independent data source. Sina/Tencent alternatives reduce dependence on EastMoney Push2, but public providers can throttle or change fields.

Industry, capital-flow and ETF modules only refresh after a live market snapshot passes date and coverage checks. Public main-flow values are estimates, not institutional account-level flows. ETF share changes are not institutional net purchases. Industry rotation uses daily snapshots accumulated after this cloud pipeline is enabled. The momentum backtest uses next-session returns, a stated 20bp round-trip cost assumption, and a minimum-history gate; it is a research experiment, not a prediction or executable strategy. Until enough history exists, the result is explicitly marked unavailable/warming up.

The 12-section report is rendered from available evidence. Missing policy, industry, institutional, earnings-consensus or macro evidence is marked unavailable rather than filled with synthetic values. A stale fallback produces a clearly labeled historical report, not a current market view.

## NAS retirement and private-data boundary

The NAS has been retired by the operator. No NAS database, portfolio records, evidence records, or private files were migrated into this public repository. The private repository's PostgreSQL service is temporary and is destroyed after each Actions run; it is not durable personal-data storage. Any future personal portfolio/evidence records require a separate authenticated, encrypted, durable private-storage design.

Research support only; not investment advice.
