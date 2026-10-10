import re

with open('tests/test_data_pipeline.py', 'r') as f:
    content = f.read()

# I will just regex replace the entire test_fetch_news_headlines_mocked block
new_test = '''def test_fetch_news_headlines_mocked():
    from src.data.pipeline import MarketDataPipeline
    from unittest.mock import patch, MagicMock
    
    pipeline = MarketDataPipeline()
    with patch("src.data.pipeline.yf.Ticker") as mock_ticker:
        mock_instance = MagicMock()
        mock_instance.news = [{"title": "Apple is doing well", "link": "http", "providerPublishTime": 12345}]
        mock_ticker.return_value = mock_instance
        
        res = pipeline.fetch_news_headlines("AAPL", days=10)
        assert len(res) == 1
        assert res[0]["title"] == "Apple is doing well"
'''

content = re.sub(r'def test_fetch_news_headlines_mocked\(\).*', new_test, content, flags=re.DOTALL)

with open('tests/test_data_pipeline.py', 'w') as f:
    f.write(content)
