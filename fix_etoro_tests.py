with open('tests/test_broker_adapters.py', 'r') as f:
    content = f.read()

content = content.replace('close_resp.status_code = 200', 'close_resp.status_code = 200\n        close_resp.text = "{}"\n        close_resp.json.return_value = {}')
content = content.replace('mod_resp.status_code = 200', 'mod_resp.status_code = 200\n        mod_resp.text = "{}"\n        mod_resp.json.return_value = {}')

with open('tests/test_broker_adapters.py', 'w') as f:
    f.write(content)
