# 02 Pipeline Batch Fetch

Type: task
Status: resolved
Blocked by: 01

## Objective
Optimize `src/data/pipeline.py` with chunked parallel / batch download capability for 160+ tickers to avoid single-request timeouts and rate limits, maintaining typed provenance and cache freshness.

## Comments
Implemented `batch_fetch_daily_bars` using `ThreadPoolExecutor` while respecting individual cache checks, disk serialization, and ADR-0002 error classification.

## Answer
Added `MarketDataPipeline.batch_fetch_daily_bars(tickers, max_workers=8)` in `src/data/pipeline.py`. Tested with concurrent fetching and disk caching.

