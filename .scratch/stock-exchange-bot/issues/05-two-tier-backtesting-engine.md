# Two-Tier Backtesting Engine & Historical Replay Architecture

Type: research
Status: open
Blocked by: 02

## Question

What is the optimal architectural design and event loop for a Two-Tier backtesting harness (Tier 1: high-speed vectorized backtesting over 5-10 years of daily price data to validate quant signal edges; Tier 2: event-driven historical replay of volatility stress periods with realistic slippage modeling, fee structures, and LLM deliberation response caching)?

## Comments
