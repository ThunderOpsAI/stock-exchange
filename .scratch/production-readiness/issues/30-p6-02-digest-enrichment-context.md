# [30] Enrich digest with real earnings, filings, news provenance, macro events, and portfolio context

Type: task
Status: resolved
Blocked by: 24

## Summary
Enrich digest with real earnings, filings, news provenance, macro events, portfolio context

## Context & Spec Reference
- Phase: Phase 6 — Decision Intelligence (Optional)
- Spec ID: P6-02
- Owner: LLM agent
- Allowed files: src/llm/*, src/data/*, tests/test_digest_enrichment.py
- Validation Command: `./.venv/bin/pytest tests/test_digest_enrichment.py`

## Acceptance Criteria
Every input carries timestamp/source; unavailable required context blocks affected decision

## Comments
Implemented `EnrichedDigest`, `ContextItem`, and `DigestEnrichmentService` in `src/llm/digest.py`. Every input item carries verifiable source, ISO-8601 timestamp, and SHA-256 provenance hash. Integrated with `TriadLLMCommittee.deliberate`: if any required context (price, earnings, macro, portfolio) is missing, the decision is immediately blocked and fails closed (`AUTO_DROPPED`, 0 composite score, explicit reason, ADR 0002).

## Answer
Resolved. 3 tests passing in `tests/test_digest_enrichment.py`.
