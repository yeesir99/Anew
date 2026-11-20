# Anew — A股公开行情数据与 A-Share Alpha Radar 公共展示

This repository includes the public market-data companion and Pages site for A-Share Alpha Radar.

## Cloud public data pipeline

- GitHub Actions runs manually and at 17:35 China time on weekdays.
- `scripts/collect_public.py` downloads the current monthly CSV from `modi-hu/stock-data`.
- The collector validates mandatory columns, A-share ticker format, latest trade date, freshness (maximum 7 calendar days), and at least 2,500 valid securities before producing a JSON snapshot.
- Only the static dashboard and sanitized market JSON are published to the `gh-pages` branch. The collector workflow uses the repository's built-in GitHub token; no cross-repository PAT is required.
- Enable GitHub Pages once: **Settings → Pages → Deploy from a branch → gh-pages → /(root)**.
- Site content: daily market breadth, advance/decline counts, median change, aggregate turnover, leaders and most-active names.

## Privacy boundary

This is a public repository. Do not store personal holdings, cost basis, share counts, account details, personal research notes, private tokens, or secrets here. The public collector does not read the private A-Share Alpha Radar database or any portfolio data. Private source code and personal data remain in the private `yeesir99/a-share-alpha-radar` repository.

## Data-source limitations

The upstream provider README states that the CSV archive is sourced from EastMoney and collected by `tr1v3r/skr`. This is a cached third-party feed, not an independent market-data source. The collector fails closed on stale or incomplete data. A green Actions result means that the checks passed for this snapshot; it is not a guarantee of an independent or uninterrupted market-data feed.

Turnover is trading activity, not net capital inflow. Public main-flow estimates are not institutional account-level flows. The public dashboard does not claim to provide verified institutional flows, industry/theme scores, the full 12-section research report, or transaction-level backtests until those data and methods are implemented and validated.

Research support only; not investment advice.
