# Research Report: Quantitative Swing Screening Strategies for $100 Capital (Ticket 02)

## 1. Executive Summary & Problem Formulation
Operating a quantitative swing trading system with a **$100 capital base** imposes strict microstructure and portfolio constraints:
1. **Fractional Share Execution**: Every entry requires purchasing fractional shares ($0.01 to $0.1 share increments on high-priced assets like SPY, QQQ, NVDA, AAPL).
2. **Spread & Slippage Drag**: Round-trip transaction costs are dominated by the **bid-ask spread** and broker execution markups. On a $25-$30 position, a 0.50% spread represents $0.15 drag; a 10-trade weekly turnover would bleed ~5-10% of portfolio value per month in frictional decay alone.
3. **Holding Period Horizon**: 1 to 14 trading days (typical swing sweet spot: 3 to 8 days). This bypasses intraday noise, avoids PDT restrictions on standard US brokerages, reduces execution turnover, and captures multi-day institutional liquidity rebalancing.
4. **Target Universe**: Highly liquid, high-dollar-volume US Equities (S&P 500 & Nasdaq 100 constituents) and liquid Sector ETFs (SPY, QQQ, IWM, XLK, XLE, XLF, XLI, XLV, XLP, XLU, XLY, SMH).

---

## 2. Asset Universe & Microstructure Liquidity Filters
The screener enforces a hard 3-layer pre-filter before calculating technical indicators:

### Universe Pre-Filters (Daily Bar Calculation)
1. **Average Daily Dollar Volume (ADDV)**:
   $$\text{ADDV}_{20}(t) = \frac{1}{20} \sum_{i=0}^{19} \text{Close}_{t-i} \times \text{Volume}_{t-i} \ge \$25,000,000$$
2. **Relative Bid-Ask Spread Filter**:
   $$\text{Spread}_{\text{rel}} = \frac{\text{Ask} - \text{Bid}}{\frac{\text{Ask} + \text{Bid}}{2}} \le 0.0006 \quad (0.06\% \text{ or } 6 \text{ bps})$$
3. **Price Threshold**:
   $$\text{Close}_t \ge \$15.00$$

---

## 3. Quantitative Edge: Dual-Strategy Complementarity
Rather than relying solely on breakouts (which suffer frequent false breaks in choppy regimes), we deploy two distinct quantitative setups:
- **Strategy Alpha (Primary - 60% weight)**: **Trend-Leader 20 EMA Pullback** (captures multi-day momentum continuations in market-leading stocks).
- **Strategy Beta (Secondary - 40% weight)**: **Regime-Filtered 14-Day RSI Oversold Mean-Reversion** (captures rapid 2-4 day rubber-band bounces in high-quality large caps).

---

## 4. Mathematical Indicator Suite & Formulations

### A. Market Regime & Dual Moving Averages
- **Macro Market Filter (SPY Benchmark)**:
  $$\text{Regime}_{\text{Bull}} = \left( \text{Close}_{\text{SPY}, t} > \text{SMA}_{200}(\text{SPY}, t) \right) \land \left( \text{SMA}_{50}(\text{SPY}, t) \ge \text{SMA}_{50}(\text{SPY}, t-5) \right)$$
- **Stock Trend Alignment**:
  $$\text{Trend}_{\text{Stock}} = \left( \text{Close}_t > \text{SMA}_{200}(t) \right) \land \left( \text{SMA}_{50}(t) > \text{SMA}_{200}(t) \right)$$

### B. Average True Range (ATR) & Volatility Sizing
- **Wilder’s Smoothed ATR ($ATR_n$)**:
  $$ATR_n(t) = \frac{ATR_n(t-1) \times (n - 1) + TR_t}{n}, \quad n = 14$$
- **Normalized ATR Percentage ($ATR\%$)**:
  $$ATR\%_t = \frac{ATR_{14}(t)}{\text{Close}_t} \times 100$$
  *Constraint: $1.2\% \le ATR\%_t \le 4.5\%$.*

### C. Relative Strength vs. S&P 500 ($RS_{SPY}$)
- **63-Day (Quarterly) Momentum Ratio**:
  $$RS_{\text{ratio}}(t) = \frac{\text{Close}_{\text{Stock}, t} / \text{Close}_{\text{Stock}, t-63}}{\text{Close}_{\text{SPY}, t} / \text{Close}_{\text{SPY}, t-63}}$$
  *Filter: $RS_{\text{ratio}}(t) \ge 1.05$.*

### D. Wilder's Relative Strength Index ($RSI_{14}$) & RVOL
- **Wilder's RSI 14**: Standard 14-period RSI; Bullish divergence condition across 10-20 bars.
- **Relative Volume**:
  $$RVOL_{20}(t) = \frac{\text{Volume}_t}{\frac{1}{20} \sum_{i=1}^{20} \text{Volume}_{t-i}} \ge 1.20$$

---

## 5. Specific Strategy Setups & Trigger Rules

### Strategy 1: Trend-Leader 20 EMA Pullback (Momentum Continuation)
- **Regime**: $Close_{SPY} > SMA_{200}(SPY)$ and stock $Close > SMA_{200}$ and $SMA_{50} > SMA_{200}$.
- **Leader Filter**: $RS_{\text{ratio}} \ge 1.05$.
- **Pullback Condition**: $\min(Low_{t-2 \dots t}) \le EMA_{20}(t) \times 1.01$ and $Low_t > SMA_{50}(t)$.
- **Reversal Trigger**: $Close_t > Open_t$ AND $Close_t > High_{t-1}$; $RVOL_{20}(t) \ge 1.20$.
- **Stop-Loss ($SL$)**: $SL = \min(\text{Low}_{t-1}, \text{Low}_t) - 0.5 \times ATR_{14}(t)$.
- **Take-Profit ($TP$)**: $TP = \text{Entry} + 2.0 \times R$.
- **Time Stop**: Exit at market on Close of day 10.

### Strategy 2: Regime-Filtered Oversold Mean-Reversion (Statistical Dip)
- **Regime**: $Close_{SPY} > SMA_{200}(SPY)$ and stock $Close > SMA_{200}$.
- **Oversold Setup**: $RSI_{14}(t) \le 34$ or lower Keltner touch ($Low_t \le EMA_{20}(t) - 1.8 \times ATR_{14}(t)$).
- **Reversal Trigger**: Hammer candle $(Close_t - Low_t) \ge 0.6 \times (High_t - Low_t)$ and break of prior high.
- **Stop-Loss ($SL$)**: $SL = Low_t - 1.0 \times ATR_{14}(t)$.
- **Take-Profit ($TP$)**: Mean reversion to $EMA_{10}(t)$ or $1.5R$.
- **Time Stop**: Exit after 4 trading days max.

---

## 6. $100 Capital Engineering & Microstructure Constraints
- **Slot Allocation**: $100 total; 3 active positions at $28-$30 each; $10 cash buffer (10%).
- **Fractional Shares**: Rounded down to 4 decimal places.
- **Turnover Control**: Max 2 to 3 entries per week; expected holding 4 to 7 days; friction drag kept below 0.25%/week.
- **Ranking Score**:
  $$\text{Rank Score} = 0.40 \cdot Z(RS_{\text{ratio}}) + 0.30 \cdot Z(RVOL_{20}) + 0.15 \cdot Z(ATR\%) - 0.15 \cdot Z(\text{Spread}_{\text{rel}})$$
