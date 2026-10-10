import re

with open('tests/test_backtester.py', 'r') as f:
    content = f.read()

start_str = "    # ------------------------------------------------------------------\n    # Run the replay engine so it hits the code in tier2_replay.py\n    # ------------------------------------------------------------------"
end_str = "    # ------------------------------------------------------------------\n    # Assertions"

before = content.split(start_str)[0]
after = end_str + content.split(end_str)[1]

new_block = """    # ------------------------------------------------------------------
    # Run the replay engine so it hits the code in tier2_replay.py
    # ------------------------------------------------------------------
    engine = Tier2HistoricalReplayEngine(db=backtest_db, initial_capital=100.0)
    
    import unittest.mock
    with unittest.mock.patch("src.backtest.tier2_replay.SimulatedPaperBroker", return_value=broker):
        report = engine.run_replay({"GAPPER": gapper_df})
        
    trade_records = report.trades
    equity_end = broker.get_account_balance().equity

"""

with open('tests/test_backtester.py', 'w') as f:
    f.write(before + new_block + after)
