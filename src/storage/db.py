"""
SQLite Storage & WAL Persistence Layer for Stock Exchange Bot.
Handles thread-safe connections, WAL mode configuration, migrations, and CRUD operations.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from src.domain.models import (
    AgentRole,
    AgentStance,
    AuditLog,
    AuditSeverity,
    CandidateStatus,
    CircuitBreakerTier,
    CommitteeVerdict,
    DeliberationCacheEntry,
    ExitReason,
    Fill,
    HITLStatus,
    LLMDeliberation,
    MarketSnapshot,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    PortfolioSnapshot,
    Position,
    PositionStatus,
    ScreenedCandidate,
    StrategyType,
    VerdictOutcome,
)

from src.storage.migration_engine import MigrationEngine

DEFAULT_DB_PATH = os.getenv("DB_PATH", "data/trading.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Database:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        journal_mode = os.environ.get("SQLITE_JOURNAL_MODE", "WAL")
        if journal_mode:
            try:
                conn.execute(f"PRAGMA journal_mode = {journal_mode};")
            except sqlite3.OperationalError:
                pass
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    @contextmanager
    def session(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self, schema_file: Optional[Path] = None) -> None:
        if schema_file:
            if not schema_file.exists():
                raise FileNotFoundError(f"Schema file not found at {schema_file}")
            with open(schema_file, "r", encoding="utf-8") as f:
                schema_sql = f.read()
            with self.session() as conn:
                conn.executescript(schema_sql)
        else:
            engine = MigrationEngine(db_path=self.db_path)
            engine.run_migrations()

    def run_migrations(self, migrations_dir: Optional[Path] = None) -> List[int]:
        engine = MigrationEngine(db_path=self.db_path, migrations_dir=migrations_dir)
        return engine.run_migrations()

    # -------------------------------------------------------------
    # 1. Market Snapshots
    # -------------------------------------------------------------
    def save_market_snapshot(self, snap: MarketSnapshot) -> int:
        query = """
        INSERT INTO market_snapshots (
            timestamp, ticker, open, high, low, close, volume,
            rsi_14, ema_20, sma_50, sma_200, atr_14, rs_spy_63d, rvol_20, spread_bps
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            snap.timestamp.isoformat(),
            snap.ticker,
            snap.open,
            snap.high,
            snap.low,
            snap.close,
            snap.volume,
            snap.rsi_14,
            snap.ema_20,
            snap.sma_50,
            snap.sma_200,
            snap.atr_14,
            snap.rs_spy_63d,
            snap.rvol_20,
            snap.spread_bps,
        )
        with self.session() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.lastrowid

    def get_latest_market_snapshot(self, ticker: str) -> Optional[MarketSnapshot]:
        query = """
        SELECT * FROM market_snapshots WHERE ticker = ? ORDER BY timestamp DESC LIMIT 1
        """
        with self.session() as conn:
            row = conn.execute(query, (ticker,)).fetchone()
            if not row:
                return None
            return MarketSnapshot(
                snapshot_id=row["snapshot_id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                ticker=row["ticker"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                rsi_14=row["rsi_14"],
                ema_20=row["ema_20"],
                sma_50=row["sma_50"],
                sma_200=row["sma_200"],
                atr_14=row["atr_14"],
                rs_spy_63d=row["rs_spy_63d"],
                rvol_20=row["rvol_20"],
                spread_bps=row["spread_bps"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            )

    # -------------------------------------------------------------
    # 2. Screened Candidates
    # -------------------------------------------------------------
    def save_candidate(self, candidate: ScreenedCandidate) -> None:
        query = """
        INSERT OR REPLACE INTO screened_candidates (
            candidate_id, timestamp, ticker, strategy, entry_est,
            stop_loss, take_profit, risk_r, allocated_usd, rank_score, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            candidate.candidate_id,
            candidate.timestamp.isoformat(),
            candidate.ticker,
            candidate.strategy.value,
            candidate.entry_est,
            candidate.stop_loss,
            candidate.take_profit,
            candidate.risk_r,
            candidate.allocated_usd,
            candidate.rank_score,
            candidate.status.value,
        )
        with self.session() as conn:
            conn.execute(query, params)

    def get_candidate(self, candidate_id: str) -> Optional[ScreenedCandidate]:
        query = "SELECT * FROM screened_candidates WHERE candidate_id = ?"
        with self.session() as conn:
            row = conn.execute(query, (candidate_id,)).fetchone()
            if not row:
                return None
            return ScreenedCandidate(
                candidate_id=row["candidate_id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                ticker=row["ticker"],
                strategy=StrategyType(row["strategy"]),
                entry_est=row["entry_est"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                risk_r=row["risk_r"],
                allocated_usd=row["allocated_usd"],
                rank_score=row["rank_score"],
                status=CandidateStatus(row["status"]),
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            )

    def update_candidate_status(self, candidate_id: str, status: CandidateStatus) -> None:
        query = "UPDATE screened_candidates SET status = ? WHERE candidate_id = ?"
        with self.session() as conn:
            conn.execute(query, (status.value, candidate_id))

    def list_candidates_by_status(self, status: CandidateStatus) -> List[ScreenedCandidate]:
        query = "SELECT * FROM screened_candidates WHERE status = ? ORDER BY timestamp DESC"
        results = []
        with self.session() as conn:
            for row in conn.execute(query, (status.value,)).fetchall():
                results.append(
                    ScreenedCandidate(
                        candidate_id=row["candidate_id"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        ticker=row["ticker"],
                        strategy=StrategyType(row["strategy"]),
                        entry_est=row["entry_est"],
                        stop_loss=row["stop_loss"],
                        take_profit=row["take_profit"],
                        risk_r=row["risk_r"],
                        allocated_usd=row["allocated_usd"],
                        rank_score=row["rank_score"],
                        status=CandidateStatus(row["status"]),
                        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                    )
                )
        return results

    # -------------------------------------------------------------
    # 3. LLM Deliberations
    # -------------------------------------------------------------
    def save_llm_deliberation(self, delib: LLMDeliberation) -> None:
        query = """
        INSERT OR REPLACE INTO llm_deliberations (
            deliberation_id, candidate_id, ticker, agent_role, model_name,
            stance, score_10, bullish_catalysts_json, risk_factors_json,
            rationale_summary, token_cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            delib.deliberation_id,
            delib.candidate_id,
            delib.ticker,
            delib.agent_role.value,
            delib.model_name,
            delib.stance.value,
            delib.score_10,
            json.dumps(delib.bullish_catalysts),
            json.dumps(delib.risk_factors),
            delib.rationale_summary,
            delib.token_cost_usd,
        )
        with self.session() as conn:
            conn.execute(query, params)

    def get_deliberations_for_candidate(self, candidate_id: str) -> List[LLMDeliberation]:
        query = "SELECT * FROM llm_deliberations WHERE candidate_id = ? ORDER BY created_at ASC"
        results = []
        with self.session() as conn:
            for row in conn.execute(query, (candidate_id,)).fetchall():
                results.append(
                    LLMDeliberation(
                        deliberation_id=row["deliberation_id"],
                        candidate_id=row["candidate_id"],
                        ticker=row["ticker"],
                        agent_role=AgentRole(row["agent_role"]),
                        model_name=row["model_name"],
                        stance=AgentStance(row["stance"]),
                        score_10=row["score_10"],
                        bullish_catalysts=json.loads(row["bullish_catalysts_json"]),
                        risk_factors=json.loads(row["risk_factors_json"]),
                        rationale_summary=row["rationale_summary"],
                        token_cost_usd=row["token_cost_usd"],
                        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                    )
                )
        return results

    # -------------------------------------------------------------
    # 4. Committee Consensus Verdicts
    # -------------------------------------------------------------
    def save_committee_verdict(self, verdict: CommitteeVerdict) -> None:
        query = """
        INSERT OR REPLACE INTO committee_verdicts (
            verdict_id, candidate_id, ticker, timestamp, composite_score,
            verdict_outcome, risk_officer_dissent, hitl_status, hitl_responded_at,
            telegram_message_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            verdict.verdict_id,
            verdict.candidate_id,
            verdict.ticker,
            verdict.timestamp.isoformat(),
            verdict.composite_score,
            verdict.verdict_outcome.value,
            1 if verdict.risk_officer_dissent else 0,
            verdict.hitl_status.value if verdict.hitl_status else None,
            verdict.hitl_responded_at.isoformat() if verdict.hitl_responded_at else None,
            verdict.telegram_message_id,
        )
        with self.session() as conn:
            conn.execute(query, params)

    def get_committee_verdict(self, candidate_id: str) -> Optional[CommitteeVerdict]:
        query = "SELECT * FROM committee_verdicts WHERE candidate_id = ? ORDER BY timestamp DESC LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query, (candidate_id,)).fetchone()
            if not row:
                return None
            return CommitteeVerdict(
                verdict_id=row["verdict_id"],
                candidate_id=row["candidate_id"],
                ticker=row["ticker"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                composite_score=row["composite_score"],
                verdict_outcome=VerdictOutcome(row["verdict_outcome"]),
                risk_officer_dissent=bool(row["risk_officer_dissent"]),
                hitl_status=HITLStatus(row["hitl_status"]) if row["hitl_status"] else None,
                hitl_responded_at=datetime.fromisoformat(row["hitl_responded_at"]) if row["hitl_responded_at"] else None,
                telegram_message_id=row["telegram_message_id"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            )

    def update_hitl_verdict(
        self,
        candidate_id: str,
        status: HITLStatus,
        responded_at: Optional[datetime] = None,
        telegram_message_id: Optional[int] = None,
    ) -> None:
        query = """
        UPDATE committee_verdicts
        SET hitl_status = ?,
            hitl_responded_at = COALESCE(?, hitl_responded_at),
            telegram_message_id = COALESCE(?, telegram_message_id)
        WHERE candidate_id = ?
        """
        ts = responded_at.isoformat() if responded_at else datetime.now(timezone.utc).isoformat()
        with self.session() as conn:
            conn.execute(query, (status.value, ts, telegram_message_id, candidate_id))

    def transition_hitl_verdict(
        self,
        candidate_id: str,
        from_status: HITLStatus,
        to_status: HITLStatus,
        responded_at: Optional[datetime] = None,
    ) -> bool:
        """
        Atomically transitions HITL verdict from from_status to to_status.
        Returns True if transition succeeded, False if already transitioned or status mismatch.
        """
        query = """
        UPDATE committee_verdicts
        SET hitl_status = ?,
            hitl_responded_at = ?
        WHERE candidate_id = ? AND hitl_status = ?
        """
        ts = responded_at.isoformat() if responded_at else datetime.now(timezone.utc).isoformat()
        with self.session() as conn:
            cur = conn.execute(query, (to_status.value, ts, candidate_id, from_status.value))
            return cur.rowcount > 0

    def get_pending_hitl_verdicts(self) -> List[CommitteeVerdict]:
        """Returns all committee verdicts currently in PENDING_TELEGRAM_RESPONSE state."""
        query = """
        SELECT verdict_id, candidate_id, ticker, timestamp, composite_score,
               verdict_outcome, risk_officer_dissent, hitl_status, hitl_responded_at,
               telegram_message_id, created_at
        FROM committee_verdicts
        WHERE hitl_status = ?
        ORDER BY timestamp ASC
        """
        with self.session() as conn:
            rows = conn.execute(query, (HITLStatus.PENDING_TELEGRAM_RESPONSE.value,)).fetchall()
            results = []
            for row in rows:
                results.append(
                    CommitteeVerdict(
                        verdict_id=row["verdict_id"],
                        candidate_id=row["candidate_id"],
                        ticker=row["ticker"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        composite_score=row["composite_score"],
                        verdict_outcome=VerdictOutcome(row["verdict_outcome"]),
                        risk_officer_dissent=bool(row["risk_officer_dissent"]),
                        hitl_status=HITLStatus(row["hitl_status"]) if row["hitl_status"] else None,
                        hitl_responded_at=datetime.fromisoformat(row["hitl_responded_at"]) if row["hitl_responded_at"] else None,
                        telegram_message_id=row["telegram_message_id"],
                        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                    )
                )
            return results


    # -------------------------------------------------------------
    # 5. Orders
    # -------------------------------------------------------------
    def save_order(self, order: Order) -> None:
        query = """
        INSERT OR REPLACE INTO orders (
            order_id, client_order_id, candidate_id, ticker, side, order_type,
            allocated_usd, target_qty, stop_loss, take_profit, state, rejection_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            order.order_id,
            order.client_order_id,
            order.candidate_id,
            order.ticker,
            order.side.value,
            order.order_type.value,
            order.allocated_usd,
            order.target_qty,
            order.stop_loss,
            order.take_profit,
            order.state.value,
            order.rejection_reason,
        )
        with self.session() as conn:
            conn.execute(query, params)

    def update_order_state(
        self, order_id: str, state: OrderState, rejection_reason: Optional[str] = None
    ) -> None:
        query = """
        UPDATE orders
        SET state = ?,
            rejection_reason = COALESCE(?, rejection_reason),
            updated_at = CURRENT_TIMESTAMP
        WHERE order_id = ?
        """
        with self.session() as conn:
            conn.execute(query, (state.value, rejection_reason, order_id))

    def get_order(self, order_id: str) -> Optional[Order]:
        query = "SELECT * FROM orders WHERE order_id = ?"
        with self.session() as conn:
            row = conn.execute(query, (order_id,)).fetchone()
            if not row:
                return None
            return Order(
                order_id=row["order_id"],
                client_order_id=row["client_order_id"],
                candidate_id=row["candidate_id"],
                ticker=row["ticker"],
                side=OrderSide(row["side"]),
                order_type=OrderType(row["order_type"]),
                allocated_usd=row["allocated_usd"],
                target_qty=row["target_qty"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                state=OrderState(row["state"]),
                rejection_reason=row["rejection_reason"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            )

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Order]:
        query = "SELECT * FROM orders WHERE client_order_id = ?"
        with self.session() as conn:
            row = conn.execute(query, (client_order_id,)).fetchone()
            if not row:
                return None
            return Order(
                order_id=row["order_id"],
                client_order_id=row["client_order_id"],
                candidate_id=row["candidate_id"],
                ticker=row["ticker"],
                side=OrderSide(row["side"]),
                order_type=OrderType(row["order_type"]),
                allocated_usd=row["allocated_usd"],
                target_qty=row["target_qty"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                state=OrderState(row["state"]),
                rejection_reason=row["rejection_reason"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            )

    def get_open_orders(self) -> List[Order]:
        """Retrieve all active/open orders requiring broker monitoring or execution."""
        query = """
        SELECT * FROM orders
        WHERE state IN ('PENDING_RISK_CHECK', 'RISK_APPROVED', 'ROUTED_TO_BROKER', 'PARTIALLY_FILLED')
        ORDER BY created_at ASC
        """
        with self.session() as conn:
            rows = conn.execute(query).fetchall()
            return [
                Order(
                    order_id=row["order_id"],
                    client_order_id=row["client_order_id"],
                    candidate_id=row["candidate_id"],
                    ticker=row["ticker"],
                    side=OrderSide(row["side"]),
                    order_type=OrderType(row["order_type"]),
                    allocated_usd=row["allocated_usd"],
                    target_qty=row["target_qty"],
                    stop_loss=row["stop_loss"],
                    take_profit=row["take_profit"],
                    state=OrderState(row["state"]),
                    rejection_reason=row["rejection_reason"],
                    created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                    updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
                )
                for row in rows
            ]

    # -------------------------------------------------------------
    # 6. Fills
    # -------------------------------------------------------------
    def save_fill(self, fill: Fill) -> None:
        query = """
        INSERT OR REPLACE INTO fills (
            fill_id, order_id, broker_order_id, ticker, side,
            filled_qty, filled_price, filled_notional, broker_fee_usd,
            slippage_usd, executed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            fill.fill_id,
            fill.order_id,
            fill.broker_order_id,
            fill.ticker,
            fill.side.value,
            fill.filled_qty,
            fill.filled_price,
            fill.filled_notional,
            fill.broker_fee_usd,
            fill.slippage_usd,
            fill.executed_at.isoformat(),
        )
        with self.session() as conn:
            conn.execute(query, params)

    def get_fills_for_order(self, order_id: str) -> List[Fill]:
        query = "SELECT * FROM fills WHERE order_id = ? ORDER BY executed_at ASC"
        results = []
        with self.session() as conn:
            for row in conn.execute(query, (order_id,)).fetchall():
                results.append(
                    Fill(
                        fill_id=row["fill_id"],
                        order_id=row["order_id"],
                        broker_order_id=row["broker_order_id"],
                        ticker=row["ticker"],
                        side=OrderSide(row["side"]),
                        filled_qty=row["filled_qty"],
                        filled_price=row["filled_price"],
                        filled_notional=row["filled_notional"],
                        broker_fee_usd=row["broker_fee_usd"],
                        slippage_usd=row["slippage_usd"],
                        executed_at=datetime.fromisoformat(row["executed_at"]),
                    )
                )
        return results

    # -------------------------------------------------------------
    # 7. Positions
    # -------------------------------------------------------------
    def save_position(self, pos: Position) -> None:
        query = """
        INSERT OR REPLACE INTO positions (
            position_id, broker_position_id, ticker, side, qty,
            entry_price, current_price, stop_loss, take_profit,
            market_value, unrealized_pnl, status, opened_at,
            closed_at, realized_pnl, exit_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            pos.position_id,
            pos.broker_position_id,
            pos.ticker,
            pos.side.value,
            pos.qty,
            pos.entry_price,
            pos.current_price,
            pos.stop_loss,
            pos.take_profit,
            pos.market_value,
            pos.unrealized_pnl,
            pos.status.value,
            pos.opened_at.isoformat(),
            pos.closed_at.isoformat() if pos.closed_at else None,
            pos.realized_pnl,
            pos.exit_reason.value if pos.exit_reason else None,
        )
        with self.session() as conn:
            conn.execute(query, params)

    def update_position(self, pos: Position) -> None:
        self.save_position(pos)

    def get_open_positions(self) -> List[Position]:
        query = "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY opened_at ASC"
        results = []
        with self.session() as conn:
            for row in conn.execute(query).fetchall():
                results.append(
                    Position(
                        position_id=row["position_id"],
                        broker_position_id=row["broker_position_id"],
                        ticker=row["ticker"],
                        side=OrderSide(row["side"]),
                        qty=row["qty"],
                        entry_price=row["entry_price"],
                        current_price=row["current_price"],
                        stop_loss=row["stop_loss"],
                        take_profit=row["take_profit"],
                        market_value=row["market_value"],
                        unrealized_pnl=row["unrealized_pnl"],
                        status=PositionStatus(row["status"]),
                        opened_at=datetime.fromisoformat(row["opened_at"]),
                        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
                        realized_pnl=row["realized_pnl"],
                        exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
                    )
                )
        return results

    def get_position(self, position_id: str) -> Optional[Position]:
        query = "SELECT * FROM positions WHERE position_id = ?"
        with self.session() as conn:
            row = conn.execute(query, (position_id,)).fetchone()
            if not row:
                return None
            return Position(
                position_id=row["position_id"],
                broker_position_id=row["broker_position_id"],
                ticker=row["ticker"],
                side=OrderSide(row["side"]),
                qty=row["qty"],
                entry_price=row["entry_price"],
                current_price=row["current_price"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                market_value=row["market_value"],
                unrealized_pnl=row["unrealized_pnl"],
                status=PositionStatus(row["status"]),
                opened_at=datetime.fromisoformat(row["opened_at"]),
                closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
                realized_pnl=row["realized_pnl"],
                exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
            )

    def get_position_by_ticker(
        self, ticker: str, status: PositionStatus = PositionStatus.OPEN
    ) -> Optional[Position]:
        query = "SELECT * FROM positions WHERE ticker = ? AND status = ? LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query, (ticker, status.value)).fetchone()
            if not row:
                return None
            return Position(
                position_id=row["position_id"],
                broker_position_id=row["broker_position_id"],
                ticker=row["ticker"],
                side=OrderSide(row["side"]),
                qty=row["qty"],
                entry_price=row["entry_price"],
                current_price=row["current_price"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                market_value=row["market_value"],
                unrealized_pnl=row["unrealized_pnl"],
                status=PositionStatus(row["status"]),
                opened_at=datetime.fromisoformat(row["opened_at"]),
                closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
                realized_pnl=row["realized_pnl"],
                exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
            )

    # -------------------------------------------------------------
    # 8. Portfolio Snapshots
    # -------------------------------------------------------------
    def save_portfolio_snapshot(self, snap: PortfolioSnapshot) -> int:
        query = """
        INSERT INTO portfolio_snapshots (
            timestamp, total_equity, cash_balance, invested_capital,
            unrealized_pnl, active_slots_used, circuit_breaker_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            snap.timestamp.isoformat(),
            snap.total_equity,
            snap.cash_balance,
            snap.invested_capital,
            snap.unrealized_pnl,
            snap.active_slots_used,
            snap.circuit_breaker_tier.value,
        )
        with self.session() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.lastrowid

    def get_latest_portfolio_snapshot(self) -> Optional[PortfolioSnapshot]:
        query = "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query).fetchone()
            if not row:
                return None
            return PortfolioSnapshot(
                snapshot_id=row["snapshot_id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                total_equity=row["total_equity"],
                cash_balance=row["cash_balance"],
                invested_capital=row["invested_capital"],
                unrealized_pnl=row["unrealized_pnl"],
                active_slots_used=row["active_slots_used"],
                circuit_breaker_tier=CircuitBreakerTier(row["circuit_breaker_tier"]),
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            )

    def get_portfolio_history(self, limit: int = 100) -> List[PortfolioSnapshot]:
        query = "SELECT * FROM portfolio_snapshots ORDER BY timestamp ASC LIMIT ?"
        results = []
        with self.session() as conn:
            for row in conn.execute(query, (limit,)).fetchall():
                results.append(
                    PortfolioSnapshot(
                        snapshot_id=row["snapshot_id"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        total_equity=row["total_equity"],
                        cash_balance=row["cash_balance"],
                        invested_capital=row["invested_capital"],
                        unrealized_pnl=row["unrealized_pnl"],
                        active_slots_used=row["active_slots_used"],
                        circuit_breaker_tier=CircuitBreakerTier(row["circuit_breaker_tier"]),
                        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                    )
                )
        return results

    # -------------------------------------------------------------
    # 9. Audit Logs
    # -------------------------------------------------------------
    def save_audit_log(
        self,
        severity: AuditSeverity,
        component: str,
        event_name: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[datetime] = None,
    ) -> int:
        ts = timestamp or datetime.now(timezone.utc)
        query = """
        INSERT INTO system_audit_logs (
            timestamp, severity, component, event_name, message, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """
        meta_json = json.dumps(metadata) if metadata else None
        with self.session() as conn:
            cursor = conn.cursor()
            cursor.execute(
                query,
                (ts.isoformat(), severity.value, component, event_name, message, meta_json),
            )
            return cursor.lastrowid

    def get_audit_logs(
        self,
        limit: int = 50,
        severity: Optional[AuditSeverity] = None,
        component: Optional[str] = None,
    ) -> List[AuditLog]:
        clauses = []
        params = []
        if severity:
            clauses.append("severity = ?")
            params.append(severity.value)
        if component:
            clauses.append("component = ?")
            params.append(component)

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM system_audit_logs {where_clause} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        results = []
        with self.session() as conn:
            for row in conn.execute(query, tuple(params)).fetchall():
                results.append(
                    AuditLog(
                        log_id=row["log_id"],
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        severity=AuditSeverity(row["severity"]),
                        component=row["component"],
                        event_name=row["event_name"],
                        message=row["message"],
                        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else None,
                        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                    )
                )
        return results

    # -------------------------------------------------------------
    # 10. Deliberation Cache
    # -------------------------------------------------------------
    def save_deliberation_cache(self, entry: DeliberationCacheEntry) -> None:
        query = """
        INSERT OR REPLACE INTO deliberation_cache (
            cache_key, symbol, as_of_date, strategy_id, agent_role,
            prompt_hash, model_name, response_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            entry.cache_key,
            entry.symbol,
            entry.as_of_date,
            entry.strategy_id,
            entry.agent_role,
            entry.prompt_hash,
            entry.model_name,
            entry.response_json,
        )
        with self.session() as conn:
            conn.execute(query, params)

    def get_deliberation_cache(self, cache_key: str) -> Optional[DeliberationCacheEntry]:
        query = "SELECT * FROM deliberation_cache WHERE cache_key = ?"
        with self.session() as conn:
            row = conn.execute(query, (cache_key,)).fetchone()
            if not row:
                return None
            return DeliberationCacheEntry(
                cache_key=row["cache_key"],
                symbol=row["symbol"],
                as_of_date=row["as_of_date"],
                strategy_id=row["strategy_id"],
                agent_role=row["agent_role"],
                prompt_hash=row["prompt_hash"],
                model_name=row["model_name"],
                response_json=row["response_json"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            )

    # -------------------------------------------------------------
    # 11. System / Operator Controls
    # -------------------------------------------------------------
    def set_system_control(
        self,
        key: str,
        value: str,
        actor: str = "system",
        reason: Optional[str] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        ts = updated_at or datetime.now(timezone.utc)
        query = """
        INSERT OR REPLACE INTO system_controls (key, value, actor, reason, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """
        with self.session() as conn:
            conn.execute(query, (key, value, actor, reason, ts.isoformat()))

    def get_system_control(self, key: str) -> Optional[Dict[str, Any]]:
        query = "SELECT key, value, actor, reason, updated_at FROM system_controls WHERE key = ?"
        with self.session() as conn:
            row = conn.execute(query, (key,)).fetchone()
            if not row:
                return None
            return {
                "key": row["key"],
                "value": row["value"],
                "actor": row["actor"],
                "reason": row["reason"],
                "updated_at": row["updated_at"],
            }

    def set_soft_freeze(
        self,
        enabled: bool,
        actor: str = "operator",
        reason: str = "",
    ) -> None:
        val = "true" if enabled else "false"
        self.set_system_control(
            key="soft_freeze",
            value=val,
            actor=actor,
            reason=reason,
        )

    def is_soft_freeze_active(self) -> bool:
        ctrl = self.get_system_control("soft_freeze")
        if not ctrl:
            return False
        return str(ctrl["value"]).strip().lower() in ("true", "1", "yes", "t", "enabled")

    def get_soft_freeze_details(self) -> Optional[Dict[str, Any]]:
        return self.get_system_control("soft_freeze")

    # -------------------------------------------------------------
    # 12. Trading Runs
    # -------------------------------------------------------------
    def create_trading_run(
        self,
        run_id: str,
        mode: str,
        config_hash: str,
        trigger: str,
        started_at: Optional[datetime] = None,
        lease_owner: Optional[str] = None,
        lease_expires_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        started = started_at or datetime.now(timezone.utc)
        query = """
        INSERT INTO trading_runs (
            run_id, mode, started_at, status, config_hash, trigger, lease_owner, lease_expires_at
        ) VALUES (?, ?, ?, 'STARTING', ?, ?, ?, ?)
        """
        lease_exp_iso = lease_expires_at.isoformat() if lease_expires_at else None
        with self.session() as conn:
            conn.execute(
                query,
                (run_id, mode, started.isoformat(), config_hash, trigger, lease_owner, lease_exp_iso),
            )
        return self.get_trading_run(run_id)  # type: ignore

    def update_trading_run_status(
        self,
        run_id: str,
        status: str,
        error_message: Optional[str] = None,
        ended_at: Optional[datetime] = None,
    ) -> None:
        ended_iso = ended_at.isoformat() if ended_at else (
            datetime.now(timezone.utc).isoformat() if status in ("COMPLETED", "FAILED", "INTERRUPTED") else None
        )
        query = """
        UPDATE trading_runs
        SET status = ?, error_message = COALESCE(?, error_message), ended_at = COALESCE(?, ended_at)
        WHERE run_id = ?
        """
        with self.session() as conn:
            conn.execute(query, (status, error_message, ended_iso, run_id))

    def get_trading_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM trading_runs WHERE run_id = ?"
        with self.session() as conn:
            row = conn.execute(query, (run_id,)).fetchone()
            return dict(row) if row else None

    def get_active_trading_run(self) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM trading_runs WHERE status IN ('STARTING', 'RUNNING') ORDER BY started_at DESC LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query).fetchone()
            return dict(row) if row else None

    def acquire_run_lease(
        self,
        run_id: str,
        mode: str = "paper",
        config_hash: str = "default",
        trigger: str = "MANUAL",
        lease_owner: Optional[str] = None,
        lease_timeout_seconds: int = 300,
    ) -> bool:
        """Atomically acquire the singleton run lease.

        Returns False if another run holds an unexpired lease. A run whose lease
        expired is marked INTERRUPTED (restart recovery will reconcile) and the lease
        is granted. Never silently overrides a live lease.
        """
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=lease_timeout_seconds)
        conn = self.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT run_id, lease_expires_at FROM trading_runs WHERE status IN ('STARTING','RUNNING')"
            ).fetchall()
            for row in rows:
                exp_raw = row["lease_expires_at"]
                try:
                    exp = datetime.fromisoformat(exp_raw) if exp_raw else None
                except ValueError:
                    exp = None
                if exp is not None and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp is not None and exp > now:
                    conn.rollback()
                    return False
                conn.execute(
                    "UPDATE trading_runs SET status='INTERRUPTED', ended_at=?, "
                    "error_message='STALE_LEASE_RECOVERED' WHERE run_id=?",
                    (now.isoformat(), row["run_id"]),
                )
            conn.execute(
                "INSERT INTO trading_runs (run_id, mode, started_at, status, config_hash, trigger, "
                "lease_owner, lease_expires_at) VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?)",
                (run_id, mode, now.isoformat(), config_hash, trigger, lease_owner, expires.isoformat()),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_run_lease(self, run_id: str, lease_timeout_seconds: int = 300) -> bool:
        expires = datetime.now(timezone.utc) + timedelta(seconds=lease_timeout_seconds)
        with self.session() as conn:
            cur = conn.execute(
                "UPDATE trading_runs SET lease_expires_at=? WHERE run_id=? AND status='RUNNING'",
                (expires.isoformat(), run_id),
            )
            return cur.rowcount == 1

    def release_run_lease(
        self, run_id: str, final_status: str = "COMPLETED", error_message: Optional[str] = None
    ) -> None:
        self.update_trading_run_status(run_id, final_status, error_message=error_message)
        with self.session() as conn:
            conn.execute("UPDATE trading_runs SET lease_expires_at=NULL WHERE run_id=?", (run_id,))

    def get_last_trading_run(self) -> Optional[Dict[str, Any]]:
        with self.session() as conn:
            row = conn.execute(
                "SELECT * FROM trading_runs ORDER BY started_at DESC, created_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_last_run(self) -> Optional[Dict[str, Any]]:
        return self.get_last_trading_run()

    def get_active_run(self) -> Optional[Dict[str, Any]]:
        return self.get_active_trading_run()


    # -------------------------------------------------------------
    # 13. Order Intents
    # -------------------------------------------------------------
    def save_order_intent(
        self,
        intent_id: str,
        request_hash: str,
        ticker: str,
        side: str,
        target_qty: float,
        allocated_usd: float,
        idempotency_key: str,
        risk_decision: str = "APPROVED",
        candidate_id: Optional[str] = None,
        limit_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        risk_reason: Optional[str] = None,
        status: str = "CREATED",
    ) -> str:
        query = """
        INSERT INTO order_intents (
            intent_id, request_hash, candidate_id, ticker, side,
            target_qty, allocated_usd, limit_price, stop_loss, take_profit,
            risk_decision, risk_reason, idempotency_key, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.session() as conn:
            conn.execute(
                query,
                (
                    intent_id, request_hash, candidate_id, ticker, side,
                    target_qty, allocated_usd, limit_price, stop_loss, take_profit,
                    risk_decision, risk_reason, idempotency_key, status
                ),
            )
        return intent_id

    def get_order_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM order_intents WHERE intent_id = ?"
        with self.session() as conn:
            row = conn.execute(query, (intent_id,)).fetchone()
            return dict(row) if row else None

    def get_order_intent_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM order_intents WHERE idempotency_key = ?"
        with self.session() as conn:
            row = conn.execute(query, (idempotency_key,)).fetchone()
            return dict(row) if row else None

    def update_order_intent_status(
        self, intent_id: str, status: str, risk_reason: Optional[str] = None
    ) -> None:
        if risk_reason is not None:
            query = """
            UPDATE order_intents
            SET status = ?, risk_reason = ?, updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ?
            """
            params = (status, risk_reason, intent_id)
        else:
            query = """
            UPDATE order_intents
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ?
            """
            params = (status, intent_id)
        with self.session() as conn:
            conn.execute(query, params)

    def get_pending_order_intents(self) -> List[Dict[str, Any]]:
        """Retrieve order intents currently requiring submission or reconciliation."""
        query = """
        SELECT * FROM order_intents
        WHERE status IN ('CREATED', 'SUBMITTED', 'UNKNOWN_PENDING_RECONCILIATION')
        ORDER BY created_at ASC
        """
        with self.session() as conn:
            rows = conn.execute(query).fetchall()
            return [dict(r) for r in rows]

    def get_all_order_intents(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve most recent order intents."""
        query = "SELECT * FROM order_intents ORDER BY created_at DESC LIMIT ?"
        with self.session() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # 14. Broker Submissions
    # -------------------------------------------------------------
    def record_broker_submission(
        self,
        submission_id: str,
        intent_id: str,
        client_order_id: str,
        outcome_classification: str,
        broker_order_id: Optional[str] = None,
        attempt_number: int = 1,
        request_payload_redacted: Optional[str] = None,
        response_payload_redacted: Optional[str] = None,
    ) -> str:
        query = """
        INSERT INTO broker_submissions (
            submission_id, intent_id, broker_order_id, attempt_number,
            client_order_id, request_payload_redacted, response_payload_redacted, outcome_classification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.session() as conn:
            conn.execute(
                query,
                (
                    submission_id, intent_id, broker_order_id, attempt_number,
                    client_order_id, request_payload_redacted, response_payload_redacted, outcome_classification
                ),
            )
        return submission_id

    def get_submissions_for_intent(self, intent_id: str) -> List[Dict[str, Any]]:
        query = "SELECT * FROM broker_submissions WHERE intent_id = ? ORDER BY attempt_number ASC"
        with self.session() as conn:
            rows = conn.execute(query, (intent_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_submission_by_broker_order_id(self, broker_order_id: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM broker_submissions WHERE broker_order_id = ? ORDER BY created_at DESC LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query, (broker_order_id,)).fetchone()
            return dict(row) if row else None

    def get_submission_by_client_order_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM broker_submissions WHERE client_order_id = ? ORDER BY created_at DESC LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query, (client_order_id,)).fetchone()
            return dict(row) if row else None

    # -------------------------------------------------------------
    # 15. Reconciliation Events
    # -------------------------------------------------------------
    def record_reconciliation_event(
        self,
        event_id: str,
        local_snapshot_hash: str,
        broker_snapshot_hash: str,
        mismatches_json: str,
        resolution_status: str,
        run_id: Optional[str] = None,
        resolution_notes: Optional[str] = None,
    ) -> str:
        query = """
        INSERT INTO reconciliation_events (
            event_id, run_id, local_snapshot_hash, broker_snapshot_hash,
            mismatches_json, resolution_status, resolution_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        with self.session() as conn:
            conn.execute(
                query,
                (
                    event_id, run_id, local_snapshot_hash, broker_snapshot_hash,
                    mismatches_json, resolution_status, resolution_notes
                ),
            )
        return event_id

    def get_latest_reconciliation_event(self) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM reconciliation_events ORDER BY created_at DESC LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query).fetchone()
            return dict(row) if row else None

    def get_reconciliation_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM reconciliation_events WHERE event_id = ?"
        with self.session() as conn:
            row = conn.execute(query, (event_id,)).fetchone()
            return dict(row) if row else None

    def get_reconciliation_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        query = "SELECT * FROM reconciliation_events ORDER BY created_at DESC LIMIT ?"
        with self.session() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # 16. Protection Status
    # -------------------------------------------------------------
    def save_protection_status(
        self,
        protection_id: str,
        ticker: str,
        protection_mode: str,
        last_verified_at: Optional[datetime] = None,
        position_id: Optional[str] = None,
        order_id: Optional[str] = None,
        stop_loss_order_id: Optional[str] = None,
        take_profit_order_id: Optional[str] = None,
        watchdog_healthy: int = 1,
        degradation_reason: Optional[str] = None,
    ) -> str:
        verified_iso = (last_verified_at or datetime.now(timezone.utc)).isoformat()
        query = """
        INSERT OR REPLACE INTO protection_status (
            protection_id, position_id, order_id, ticker, protection_mode,
            stop_loss_order_id, take_profit_order_id, watchdog_healthy,
            last_verified_at, degradation_reason, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """
        with self.session() as conn:
            conn.execute(
                query,
                (
                    protection_id, position_id, order_id, ticker, protection_mode,
                    stop_loss_order_id, take_profit_order_id, watchdog_healthy,
                    verified_iso, degradation_reason
                ),
            )
        return protection_id

    def get_protection_status_for_position(self, position_id: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM protection_status WHERE position_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        with self.session() as conn:
            row = conn.execute(query, (position_id,)).fetchone()
            return dict(row) if row else None

    def get_active_protection_statuses(self) -> List[Dict[str, Any]]:
        query = "SELECT * FROM protection_status ORDER BY updated_at DESC"
        with self.session() as conn:
            rows = conn.execute(query).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # 17. Instrument Metadata
    # -------------------------------------------------------------
    def save_instrument_metadata(
        self,
        ticker: str,
        exchange: str,
        universe_version: str,
        sector: Optional[str] = None,
        industry: Optional[str] = None,
        tradable: int = 1,
        min_lot_size: float = 1.0,
        price_increment: float = 0.01,
        next_earnings_date: Optional[datetime] = None,
        corporate_action_flag: int = 0,
    ) -> None:
        earnings_iso = next_earnings_date.isoformat() if next_earnings_date else None
        query = """
        INSERT OR REPLACE INTO instrument_metadata (
            ticker, sector, industry, exchange, tradable,
            min_lot_size, price_increment, next_earnings_date,
            corporate_action_flag, universe_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """
        with self.session() as conn:
            conn.execute(
                query,
                (
                    ticker.upper(), sector, industry, exchange, tradable,
                    min_lot_size, price_increment, earnings_iso,
                    corporate_action_flag, universe_version
                ),
            )

    def get_instrument_metadata(self, ticker: str) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM instrument_metadata WHERE ticker = ?"
        with self.session() as conn:
            row = conn.execute(query, (ticker.upper(),)).fetchone()
            return dict(row) if row else None
