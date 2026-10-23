# Market Digest

A self-running scan of the S&P 500 and a set of ETFs. Every 15 minutes while the market is open,
GitHub Actions runs `digest.py`, which pulls free market data and Benzinga headlines
from Alpaca and rewrites `docs/data.json`. GitHub Pages serves `docs/index.html`.
No Claude, server, or paid API is needed to keep it running.

## What it shows

- **Market:** index, sector and a few leveraged ETFs, plus how many names are up vs down.
- **Unusual moves:** stocks moving at least 2.5x their normal daily swing
  (average absolute daily % change over the last 20 days) and at least 1.5%.
- **Big swingers:** stocks that normally move 2.5%+ a day (TSLA-type names),
  ranked by today's move, tagged "Bigger than usual" or "Normal day for it".
- **Pre-market gaps:** from 7am ET, scans every 15 minutes write `docs/premarket.json`: names whose latest
  pre-market trade is at least 1% (and 1.5x their normal move) away from yesterday's close, with pre-market
  volume and a **Thin** tag under 2,000 shares. Alpaca's free IEX feed sees only a slice of pre-market trading,
  so many names have no print at all and thin ones can show odd prices. Treat it as a prompt to look, not a quote.
- **New** flag on Unusual moves for the first 15 minutes after a stock joins the list, plus an
  **Alerts** button (chime and desktop pop-up; the first look of the day alerts on nothing).
- **Earnings tags** ("Earnings after close", "Reported today") come from Finnhub's free calendar. Get a free key at finnhub.io and add it as the repo secret `FINNHUB_KEY`; without it the tags are simply skipped.
- **vol 2.1×** on each row: today's volume against a normal day's pace at this time.
- **Trade ↗** on every row and market tile opens the stock on Robinhood.
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

The page re-checks for new data every 3 minutes, so it can stay open all day.
Scheduled runs can start a few minutes late when GitHub is busy.

## Near-live (optional, free)

The page updates every 15 minutes by default. To get about one-minute updates, deploy the
Cloudflare Worker in `worker/`, which rebuilds the lists from fresh Alpaca prices:

1. Make a free Cloudflare account. Create an API token (**My Profile → API Tokens → Create Token →
   "Edit Cloudflare Workers" template**) and copy your **Account ID** from the dashboard.
2. Add repo secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.
3. **Actions → Deploy live worker → Run workflow.** It deploys the worker, copies your Alpaca keys
   into it, and writes `docs/live.json` so the page uses it.

If the worker is down or the market is closed, the page falls back to the 15-minute digest.
The worker needs a one-time `digest.py` run first, which publishes `docs/baseline.json`.
If you fork the repo, change `BASELINE_URL` in `worker/wrangler.toml`.
