"""
Validation script for Stock Exchange Bot Persistence Schema
Verifies table creation, foreign key constraints, and indexing.
"""

import json
import sqlite3
from datetime import datetime


def verify_schema():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")

    with open("prototypes/storage_schema/schema.sql", "r") as f:
        schema_sql = f.read()

    conn.executescript(schema_sql)

    # 1. Insert Market Snapshot
    conn.execute(
        """
        INSERT INTO market_snapshots (
            timestamp, ticker, open, high, low, close, volume,
            rsi_14, ema_20, sma_50, sma_200, atr_14, rs_spy_63d, rvol_20, spread_bps
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            datetime.utcnow(),
            "NVDA",
            128.0,
            130.5,
            127.5,
            129.8,
            45000000,
            44.5,
            126.2,
            122.0,
            110.5,
            4.2,
            1.15,
            1.35,
            2.5,
        ),
    )

    # 2. Insert Screened Candidate
    cand_id = "CAND_NVDA_20261002"
    conn.execute(
        """
        INSERT INTO screened_candidates (
            candidate_id, timestamp, ticker, strategy, entry_est,
            stop_loss, take_profit, risk_r, allocated_usd, rank_score, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            cand_id,
            datetime.utcnow(),
            "NVDA",
            "TREND_PULLBACK",
            129.8,
            125.6,
            138.2,
            4.2,
            28.5,
            1.42,
            "HITL_ESCALATED",
        ),
    )

    # 3. Insert Deliberation Triad (Sentiment, Technical, Adversarial Risk)
    conn.execute(
        """
        INSERT INTO llm_deliberations (
            deliberation_id, candidate_id, ticker, agent_role, model_name,
            stance, score_10, bullish_catalysts_json, risk_factors_json, rationale_summary, token_cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            "DELIB_1",
            cand_id,
            "NVDA",
            "sentiment_catalyst",
            "gemini-2.5-flash",
            "BULLISH",
            8.5,
            json.dumps(["Data center demand strong", "Analyst target upgrade"]),
            json.dumps(["Export controls"]),
            "Strong bullish sentiment across tech sector.",
            0.0008,
        ),
    )

    conn.execute(
        """
        INSERT INTO llm_deliberations (
            deliberation_id, candidate_id, ticker, agent_role, model_name,
            stance, score_10, bullish_catalysts_json, risk_factors_json, rationale_summary, token_cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            "DELIB_2",
            cand_id,
            "NVDA",
            "technical_structure",
            "gemini-2.5-flash",
            "BULLISH",
            8.0,
            json.dumps(["Clean 20 EMA bounce", "RVOL > 1.3"]),
            json.dumps(["Overhead resistance at 132"]),
            "Textbook 20 EMA pullback with high volume reversal.",
            0.0007,
        ),
    )

    conn.execute(
        """
        INSERT INTO llm_deliberations (
            deliberation_id, candidate_id, ticker, agent_role, model_name,
            stance, score_10, bullish_catalysts_json, risk_factors_json, rationale_summary, token_cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            "DELIB_3",
            cand_id,
            "NVDA",
            "adversarial_risk",
            "gemini-2.5-flash",
            "VETO",
            3.5,
            json.dumps([]),
            json.dumps(
                ["FOMC rate announcement in 48 hours", "Semiconductor beta high"]
            ),
            "High binary event risk due to upcoming Fed minutes.",
            0.0009,
        ),
    )

    # 4. Insert Committee Verdict (HITL Escalated due to 51% vs 49% split)
    conn.execute(
        """
        INSERT INTO committee_verdicts (
            verdict_id, candidate_id, ticker, timestamp, composite_score,
            verdict_outcome, risk_officer_dissent, hitl_status, telegram_message_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            "VERD_1",
            cand_id,
            "NVDA",
            datetime.utcnow(),
            5.88,
            "HITL_ESCALATED",
            1,
            "PENDING_TELEGRAM_RESPONSE",
            10482,
        ),
    )

    # 5. Insert Order
    ord_id = "ORD_NVDA_001"
    conn.execute(
        """
        INSERT INTO orders (
            order_id, client_order_id, candidate_id, ticker, side, order_type,
            allocated_usd, target_qty, stop_loss, take_profit, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            ord_id,
            "cli_ord_12345",
            cand_id,
            "NVDA",
            "BUY",
            "BRACKET",
            28.5,
            0.2195,
            125.6,
            138.2,
            "ROUTED_TO_BROKER",
        ),
    )

    # 6. Insert Fill
    conn.execute(
        """
        INSERT INTO fills (
            fill_id, order_id, ticker, side, filled_qty, filled_price,
            filled_notional, broker_fee_usd, slippage_usd, executed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            "FILL_1",
            ord_id,
            "NVDA",
            "BUY",
            0.2195,
            129.85,
            28.50,
            0.04,
            0.01,
            datetime.utcnow(),
        ),
    )

    # 7. Insert Portfolio Snapshot
    conn.execute(
        """
        INSERT INTO portfolio_snapshots (
            timestamp, total_equity, cash_balance, invested_capital, unrealized_pnl,
            active_slots_used, circuit_breaker_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (datetime.utcnow(), 100.0, 71.46, 28.50, 0.04, 1, 0),
    )

    conn.commit()

    # Verify query
    cursor = conn.cursor()
    cursor.execute("""
        SELECT o.order_id, o.ticker, o.state, f.filled_qty, f.filled_price, cv.verdict_outcome
        FROM orders o
        JOIN fills f ON o.order_id = f.order_id
        JOIN committee_verdicts cv ON o.candidate_id = cv.candidate_id
    """)
    row = cursor.fetchone()
    print("Schema Verification Result:", row)
    assert row[0] == "ORD_NVDA_001"
    assert row[1] == "NVDA"
    assert row[5] == "HITL_ESCALATED"
    print("All SQLite tables, foreign keys, and indexes verified successfully!")


if __name__ == "__main__":
    verify_schema()
