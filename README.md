# Market Digest

A self-running scan of the S&P 500 and a set of ETFs. Three times each trading day
GitHub Actions runs `digest.py`, which pulls free market data and Benzinga headlines
from Alpaca and rewrites `docs/data.json`. GitHub Pages serves `docs/index.html`.
No Claude, server, or paid API is needed to keep it running.

## What it shows

- **Market:** index, sector and a few leveraged ETFs, plus how many names are up vs down.
- **Unusual moves:** stocks moving at least 2.5x their normal daily swing
  (average absolute daily % change over the last 20 days) and at least 1.5%.
- **Big swingers:** stocks that normally move 2.5%+ a day (TSLA-type names),
  ranked by today's move, tagged "Bigger than usual" or "Normal day for it".
- Each row opens to today's range, a spike-and-fade read, and linked headlines.

## Setup (one time)

1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   add `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY` from your Alpaca **paper** dashboard.
2. **Settings → Pages:** Source "Deploy from a branch", branch `main`, folder `/docs`.
3. **Actions → Market digest → Run workflow** (tick "force" if the market is closed) to test.

## Tuning

- Change the thresholds at the top of `digest.py`.
- Add or remove ETFs in `data/etfs.csv`.
- Refresh `data/sp500.csv` from
  https://github.com/datasets/s-and-p-500-companies when the index changes.
- `python digest.py --demo` writes fake data for previewing the page without keys.

Prices come from Alpaca's free IEX feed, so volumes look lower than on full-market
apps. Not investment advice.
