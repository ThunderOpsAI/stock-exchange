# Quantitative Swing Screening Strategies for $100 Capital

Type: research
Status: resolved
Blocked by: none

## Question

What exact quantitative screening algorithms, indicators, and threshold parameters (e.g. dual SMA/EMA trend filters, RSI oversold/divergence, ATR volatility breakouts, volume surge filters) provide a statistically verifiable edge for multi-day swing positions (1-14 day hold) in liquid US equities & sector ETFs, specifically accounting for fractional share execution and minimizing spread churn on $100 capital?

## Answer

Established a dual-strategy quantitative screener tailored for $100 fractional capital:
1. **Microstructure & Liquidity**: Screened against S&P 500 / Nasdaq 100 leaders and liquid Sector ETFs; ADDV20 >= $25M; relative spread <= 0.06% (6 bps); Close >= $15.00 to avoid penny stock adverse selection.
2. **Dual Strategy Setups**:
   - **Trend-Leader 20 EMA Pullback (60% weight)**: Macro SPY > 200 SMA; stock > 200 SMA & 50 > 200 SMA; 63-day RS vs SPY >= 1.05; 3-day pullback to 20 EMA with declining volume; reversal bar above prior high with RVOL >= 1.20; 2.0R take-profit, trailing/structural stop.
   - **Regime-Filtered 14-day RSI Oversold Mean-Reversion (40% weight)**: Stock in macro uptrend (> 200 SMA); RSI14 <= 34 or lower Keltner touch; hammer rejection candle; exit at 10 EMA or 1.5R.
3. **$100 Portfolio Constraints**: Fixed 3-position slot model ($28-$30 per position) + $10 (10%) cash reserve; max 2-3 trades/week; holding period 4-7 days; composite z-score ranking.

Full research report captured on branch `research/02-quantitative-swing-screening` in `docs/research/02-quantitative-swing-screening.md`.

## Comments
