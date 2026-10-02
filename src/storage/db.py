"""
SQLite Storage & WAL Persistence Layer for Stock Exchange Bot.
Handles thread-safe connections, WAL mode configuration, migrations, and CRUD operations.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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
        conn.execute("PRAGMA journal_mode = WAL;")
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
        target_schema = schema_file or SCHEMA_PATH
        if not target_schema.exists():
            raise FileNotFoundError(f"Schema file not found at {target_schema}")
        with open(target_schema, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        with self.session() as conn:
            conn.executescript(schema_sql)

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
        self, limit: int = 50, severity: Optional[AuditSeverity] = None
    ) -> List[AuditLog]:
        if severity:
            query = "SELECT * FROM system_audit_logs WHERE severity = ? ORDER BY timestamp DESC LIMIT ?"
            params = (severity.value, limit)
        else:
            query = "SELECT * FROM system_audit_logs ORDER BY timestamp DESC LIMIT ?"
            params = (limit,)

        results = []
        with self.session() as conn:
            for row in conn.execute(query, params).fetchall():
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
