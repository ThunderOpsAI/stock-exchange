import re

with open('tests/test_backtester.py', 'r') as f:
    content = f.read()

# Find the block starting at "    # Run the gap-stop detection block" and ending before "    # Assertions"
start_str = "    # Run the gap-stop detection block (same logic as tier2_replay.py:156-223)"
end_str = "    # ------------------------------------------------------------------\n    # Assertions"

before = content.split(start_str)[0]
after = end_str + content.split(end_str)[1]

new_block = """    # ------------------------------------------------------------------
    # Run the replay engine so it hits the code in tier2_replay.py
    # ------------------------------------------------------------------
    engine = Tier2HistoricalReplayEngine(db=backtest_db, initial_capital=100.0)
    engine.broker = broker
    # The risk engine needs the updated broker
    engine.risk_engine.broker = broker
    
    report = engine.run_replay({"GAPPER": gapper_df})
    trade_records = report.trades
    equity_end = engine.broker.get_account_balance().equity

"""

with open('tests/test_backtester.py', 'w') as f:
    f.write(before + new_block + after)
