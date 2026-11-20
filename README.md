# Anew — A股公开研究数据与 A-Share Alpha Radar 公共展示

This repository hosts the public static dashboard for A-Share Alpha Radar.

## Publishing pipeline

- The private source repository `yeesir99/a-share-alpha-radar` runs the GitHub Actions cloud batch at 17:35 China time on weekdays and on demand.
- The private workflow publishes only an explicitly allow-listed public JSON snapshot and static HTML to this repository's `gh-pages` branch.
- The former duplicate public collector workflow has been disabled so it cannot overwrite the private workflow's newer research snapshot.
- GitHub Pages: **Settings → Pages → Deploy from a branch → gh-pages → /(root)**.
- Dashboard: https://yeesir99.github.io/Anew/

## Privacy boundary

This repository and its Pages website are public. Personal holdings, costs, quantities, market values, account details, private notes, manual evidence records, portfolio-derived reports, and secrets must never be committed here. The publisher excludes portfolio and evidence endpoints and withholds legacy Markdown reports pending a separate public/private review.

## Data-source limitations

The cloud batch uses public industry/market snapshots, sampled stock technicals, public sector-flow estimates, ETF share snapshots, public announcements/RSS, industry valuation snapshots, and a third-party monthly stock archive when each source is available. Providers can fail independently; the generated JSON includes per-job outcomes and source status.

The `modi-hu/stock-data` archive declares EastMoney as its upstream. It is a third-party cache, not an independent market-data source. Turnover is trading activity, not net capital inflow. Public main-flow estimates are not institution-specific account flows. ETF share changes are not institutional net purchases.

The dashboard is not yet the complete legacy product: private portfolio functions, private evidence records, and the fully reviewed 12-section report are not published. Historical signal observations are not a transaction-level backtest.

Research support only; not investment advice.
