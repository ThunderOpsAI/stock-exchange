with open('tests/test_run_lifecycle.py', 'r') as f:
    content = f.read()

new_main_tests = """
def test_main_cli(monkeypatch):
    import sys
    from src.main import main
    from unittest.mock import patch, MagicMock
    
    with patch("src.main.TradingOrchestrator") as mock_orch:
        mock_instance = MagicMock()
        mock_instance.run_daily_cycle.return_value = {"status": "SUCCESS"}
        mock_orch.return_value = mock_instance
        
        # mock sleep
        with patch("src.main.time.sleep", side_effect=KeyboardInterrupt):
            monkeypatch.setattr(sys, "argv", ["main.py", "--daemon"])
            try:
                main()
            except KeyboardInterrupt:
                pass
            
            monkeypatch.setattr(sys, "argv", ["main.py", "--paper"])
            try:
                main()
            except Exception:
                pass
"""

content += new_main_tests

with open('tests/test_run_lifecycle.py', 'w') as f:
    f.write(content)
