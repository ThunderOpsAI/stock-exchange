with open('tests/test_broker_adapters.py', 'r') as f:
    content = f.read()

content = content.replace('''    # Mock WS tick stream
    with patch("src.broker.alpaca.websockets.connect") as mock_ws:
        pass  # Just ensure it doesn't break, maybe it needs async tests''', '')

with open('tests/test_broker_adapters.py', 'w') as f:
    f.write(content)
