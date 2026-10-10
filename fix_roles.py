import re

with open('tests/test_llm_committee.py', 'r') as f:
    content = f.read()

content = content.replace('agent_role="sentiment"', 'agent_role="sentiment_catalyst"')
content = content.replace('agent_role="technical"', 'agent_role="technical_structure"')
content = content.replace('agent_role="risk"', 'agent_role="adversarial_risk"')
content = content.replace('agent_role="role"', 'agent_role="sentiment_catalyst"')

with open('tests/test_llm_committee.py', 'w') as f:
    f.write(content)
