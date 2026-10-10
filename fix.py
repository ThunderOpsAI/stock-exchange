import re

with open('tests/test_llm_committee.py', 'r') as f:
    content = f.read()

content = content.replace('mocker.patch.object(committee, "_evaluate_agent_with_cache", side_effect=[', 'mock = __import__("unittest.mock").mock.patch.object(committee, "_evaluate_agent_with_cache", side_effect=[')
content = content.replace('])\n    \n    verdict, delibs = committee.deliberate(candidate)', '])\n    mock.start()\n    verdict, delibs = committee.deliberate(candidate)\n    mock.stop()')
content = content.replace('])\n    verdict, delibs = committee.deliberate(candidate)', '])\n    mock.start()\n    verdict, delibs = committee.deliberate(candidate)\n    mock.stop()')
content = content.replace('])\n    verdict2, delibs2 = committee.deliberate(candidate)', '])\n    mock.start()\n    verdict2, delibs2 = committee.deliberate(candidate)\n    mock.stop()')
content = content.replace('mocker.patch.object(committee, "_evaluate_agent_with_cache", return_value=', 'mock = __import__("unittest.mock").mock.patch.object(committee, "_evaluate_agent_with_cache", return_value=')
content = content.replace('))\n    \n    verdict, delibs = committee.deliberate(candidate, enriched_digest=CompleteDigest())', '))\n    mock.start()\n    verdict, delibs = committee.deliberate(candidate, enriched_digest=CompleteDigest())\n    mock.stop()')

with open('tests/test_llm_committee.py', 'w') as f:
    f.write(content)
