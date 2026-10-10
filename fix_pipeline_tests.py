with open('tests/test_data_pipeline.py', 'r') as f:
    content = f.read()

start_str = '        pipeline = MarketDataPipeline()\n        with patch("src.data.pipeline.requests.get") as mock_get:'
end_str = 'res = pipeline.fetch_news_headlines("AAPL", days=10)'
replacement = '''        pipeline = MarketDataPipeline()
        with patch("src.data.pipeline.yf.Ticker") as mock_ticker:
            mock_instance = MagicMock()
            mock_instance.news = [{"title": "Apple is doing well", "link": "http", "providerPublishTime": 12345}]
            mock_ticker.return_value = mock_instance
            
            res = pipeline.fetch_news_headlines("AAPL", days=10)'''

content = content.replace(start_str + content.split(start_str)[1].split(end_str)[0] + end_str, replacement)

with open('tests/test_data_pipeline.py', 'w') as f:
    f.write(content)
