"""
적응형 학습 엔진 (Adaptive Learning Engine).
모든 거래의 진입 시점 지표값 + 결과를 누적 저장하고,
지표별 가중치를 동적 조정합니다.
패배 패턴 회피 + 승리 패턴 부스트를 구현합니다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src import config

logger = logging.getLogger(__name__)


@dataclass
class TradeSnapshot:
    """거래 스냅샷: 진입 시점 지표 + 결과."""
    trade_id: str
    timestamp: str
    market_type: str
    strategy: str
    asset: str
    direction: str
    indicators: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "timestamp": self.timestamp,
            "market_type": self.market_type,
            "strategy": self.strategy,
            "asset": self.asset,
            "direction": self.direction,
            "indicators": self.indicators,
            "context": self.context,
            "result": self.result,
        }


@dataclass
class Pattern:
    """승리/패배 패턴."""
    conditions: dict
    total_trades: int
    wins: int
    win_rate: float
    avg_pnl_pct: float
    pattern_type: str       # "win" | "loss"
    created_at: str
    last_evaluated: str


class AdaptiveLearningEngine:
    """통계 기반 적응형 학습 시스템."""

    def __init__(self):
        self.trades: List[dict] = []
        self.adaptive_weights: Dict[str, Tuple[float, float, float]] = {}
        self.win_rate_tables: Dict[str, Dict[str, dict]] = {}
        self.patterns: List[Pattern] = []
        self.context_stats: Dict[str, Dict[str, dict]] = {}

        self._trade_count: int = 0
        self._base_weights = config.INDICATOR_BASE_WEIGHTS.copy()

        self._history_path = Path(config.TRADE_HISTORY_FILE)
        self._weights_path = Path(config.ADAPTIVE_WEIGHTS_FILE)
        self._patterns_path = Path(config.PATTERNS_FILE)

    def initialize(self):
        """저장된 학습 데이터를 로드합니다."""
        self._history_path.parent.mkdir(parents=True, exist_ok=True)

        if self._history_path.exists():
            with open(self._history_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self.trades.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            self._trade_count = len(self.trades)
            logger.info("Loaded %d trade snapshots", self._trade_count)

        if self._weights_path.exists():
            with open(self._weights_path) as f:
                data = json.load(f)
                self.adaptive_weights = {
                    k: tuple(v) for k, v in data.get("weights", {}).items()
                }
                self.win_rate_tables = data.get("win_rate_tables", {})
                self.context_stats = data.get("context_stats", {})

        if self._patterns_path.exists():
            with open(self._patterns_path) as f:
                data = json.load(f)
                self.patterns = [Pattern(**p) for p in data.get("patterns", [])]

        logger.info(
            "Learning engine initialized: %d trades, %d patterns",
            self._trade_count, len(self.patterns),
        )

    # ── 거래 기록 ─────────────────────────────────────────────

    def record_trade(self, snapshot: dict):
        """거래 스냅샷을 기록하고 학습합니다."""
        self.trades.append(snapshot)
        self._trade_count += 1

        with open(self._history_path, "a") as f:
            f.write(json.dumps(snapshot) + "\n")

        self._update_adaptive_weights(snapshot)
        self._update_win_rate_tables(snapshot)
        self._update_context_stats(snapshot)

        if self._trade_count % 50 == 0:
            self._scan_patterns()

        self._save_state()

    # ── 블렌딩 비율 ───────────────────────────────────────────

    def get_blend_ratio(self) -> float:
        if self._trade_count < config.WARMUP_TRADES:
            return 0.0
        return min(0.8, self._trade_count / config.BLEND_FULL_TRADES)

    def get_adaptive_weights(self) -> Dict[str, tuple]:
        return self.adaptive_weights

    def get_win_rate_factors(self, indicators: dict) -> Dict[str, float]:
        """현재 지표값의 구간별 승률 부스트 계수."""
        factors = {}
        for ind_name, buckets in self.win_rate_tables.items():
            bucket_key = self._get_bucket_key(ind_name, indicators.get(ind_name))
            if bucket_key and bucket_key in buckets:
                stats = buckets[bucket_key]
                if stats.get("total", 0) >= config.WIN_RATE_MIN_SAMPLES:
                    wr = stats["wins"] / stats["total"]
                    if wr > 0.65:
                        factors[ind_name] = config.WIN_RATE_BOOST_HIGH
                    elif wr > 0.55:
                        factors[ind_name] = config.WIN_RATE_NEUTRAL
                    elif wr > 0.45:
                        factors[ind_name] = config.WIN_RATE_PENALTY
                    else:
                        factors[ind_name] = config.WIN_RATE_STRONG_PENALTY
        return factors

    # ── 패턴 매칭 ─────────────────────────────────────────────

    def check_loss_pattern(self, indicators: dict, context: dict) -> Optional[float]:
        """패배 패턴 체크. Returns: 배수(0.3~1.0) 또는 None(차단)."""
        for pattern in self.patterns:
            if pattern.pattern_type != "loss":
                continue
            if self._matches_pattern(pattern, indicators, context):
                if pattern.win_rate < 0.20:
                    return None
                elif pattern.win_rate < 0.30:
                    return None
                elif pattern.win_rate < 0.40:
                    return 0.3
        return 1.0

    def check_win_pattern(self, indicators: dict, context: dict) -> float:
        """승리 패턴 체크. Returns: 부스트 배수 (1.0~1.8)."""
        for pattern in self.patterns:
            if pattern.pattern_type != "win":
                continue
            if self._matches_pattern(pattern, indicators, context):
                if pattern.win_rate > 0.85:
                    return 1.8
                elif pattern.win_rate > 0.75:
                    return 1.5
                elif pattern.win_rate > 0.65:
                    return 1.3
        return 1.0

    # ── 내부: 가중치 업데이트 ─────────────────────────────────

    def _update_adaptive_weights(self, snapshot: dict):
        result = snapshot.get("result", {})
        outcome = result.get("outcome", "")
        pnl_pct = abs(result.get("pnl_pct", 0)) / 10
        indicators = snapshot.get("indicators", {})
        direction = snapshot.get("direction", "Yes")
        alpha = config.LEARNING_RATE

        for ind_name, base_w in self._base_weights.items():
            current = self.adaptive_weights.get(ind_name, base_w)
            c_strong, c_normal, c_counter = current

            ind_signal = self._get_indicator_signal(ind_name, indicators)
            expected_signal = "buy" if direction == "Yes" else "sell"

            if ind_signal == expected_signal:
                if outcome == "win":
                    c_strong += alpha * pnl_pct
                    c_normal += alpha * pnl_pct * 0.5
                else:
                    c_strong -= alpha * pnl_pct
                    c_normal -= alpha * pnl_pct * 0.5
            elif ind_signal and ind_signal != "neutral":
                if outcome == "win":
                    c_counter += alpha * pnl_pct
                else:
                    c_counter -= alpha * pnl_pct

            c_strong = max(config.WEIGHT_MIN_STRONG, min(config.WEIGHT_MAX_STRONG, c_strong))
            c_normal = max(config.WEIGHT_MIN_NORMAL, min(config.WEIGHT_MAX_NORMAL, c_normal))
            c_counter = max(config.WEIGHT_MIN_COUNTER, min(config.WEIGHT_MAX_COUNTER, c_counter))

            self.adaptive_weights[ind_name] = (
                round(c_strong, 4), round(c_normal, 4), round(c_counter, 4),
            )

    def _update_win_rate_tables(self, snapshot: dict):
        result = snapshot.get("result", {})
        outcome = result.get("outcome", "")
        indicators = snapshot.get("indicators", {})

        indicator_keys = {
            "rsi": "rsi_1m",
            "ema": "ema_cross",
            "vwap": "vwap_position",
            "bb": "bb_position",
            "macd": "macd_histogram",
            "volume": "volume_ratio",
            "ofi": "orderbook_imb",
        }

        for ind_name, key in indicator_keys.items():
            value = indicators.get(key)
            if value is None:
                continue

            bucket_key = self._get_bucket_key(ind_name, value)
            if not bucket_key:
                continue

            if ind_name not in self.win_rate_tables:
                self.win_rate_tables[ind_name] = {}
            if bucket_key not in self.win_rate_tables[ind_name]:
                self.win_rate_tables[ind_name][bucket_key] = {
                    "total": 0, "wins": 0, "losses": 0, "avg_pnl": 0.0,
                }

            entry = self.win_rate_tables[ind_name][bucket_key]
            entry["total"] += 1
            if outcome == "win":
                entry["wins"] += 1
            else:
                entry["losses"] += 1

            pnl = result.get("pnl_pct", 0)
            entry["avg_pnl"] = (
                (entry["avg_pnl"] * (entry["total"] - 1) + pnl) / entry["total"]
            )

    def _update_context_stats(self, snapshot: dict):
        result = snapshot.get("result", {})
        context = snapshot.get("context", {})
        outcome = result.get("outcome", "")

        categories = {
            "hour": str(context.get("hour_utc", "unknown")),
            "asset": snapshot.get("asset", "unknown"),
            "volatility": self._classify_atr(context.get("atr_pct", 0)),
            "strategy": snapshot.get("strategy", "unknown"),
            "market_type": snapshot.get("market_type", "unknown"),
        }

        for cat_name, cat_value in categories.items():
            if cat_name not in self.context_stats:
                self.context_stats[cat_name] = {}
            if cat_value not in self.context_stats[cat_name]:
                self.context_stats[cat_name][cat_value] = {
                    "total": 0, "wins": 0, "avg_pnl": 0.0,
                }

            entry = self.context_stats[cat_name][cat_value]
            entry["total"] += 1
            if outcome == "win":
                entry["wins"] += 1
            pnl = result.get("pnl_pct", 0)
            entry["avg_pnl"] = (
                (entry["avg_pnl"] * (entry["total"] - 1) + pnl) / entry["total"]
            )

    # ── 패턴 분석 ─────────────────────────────────────────────

    def _scan_patterns(self):
        recent = self.trades[-config.ROLLING_WINDOW:]
        if len(recent) < 30:
            return

        self.patterns = []
        condition_generators = [
            ("rsi_bucket", lambda s: self._get_bucket_key("rsi", s.get("indicators", {}).get("rsi_1m"))),
            ("vol_bucket", lambda s: self._get_bucket_key("volume", s.get("indicators", {}).get("volume_ratio"))),
            ("hour", lambda s: str(s.get("context", {}).get("hour_utc", ""))),
            ("asset", lambda s: s.get("asset", "")),
            ("ema_cross", lambda s: s.get("indicators", {}).get("ema_cross", "")),
            ("atr_class", lambda s: self._classify_atr(s.get("context", {}).get("atr_pct", 0))),
        ]

        for i in range(len(condition_generators)):
            for j in range(i + 1, len(condition_generators)):
                name_i, fn_i = condition_generators[i]
                name_j, fn_j = condition_generators[j]

                groups = defaultdict(list)
                for trade in recent:
                    key = (fn_i(trade), fn_j(trade))
                    if all(k for k in key):
                        groups[key].append(trade)

                for key, trades in groups.items():
                    total = len(trades)
                    if total < config.PATTERN_MIN_TRADES:
                        continue
                    wins = sum(1 for t in trades if t.get("result", {}).get("outcome") == "win")
                    wr = wins / total
                    avg_pnl = sum(t.get("result", {}).get("pnl_pct", 0) for t in trades) / total

                    conditions = {name_i: key[0], name_j: key[1]}
                    now_str = datetime.now(timezone.utc).isoformat()

                    if wr < config.PATTERN_LOSS_THRESHOLD:
                        self.patterns.append(Pattern(
                            conditions=conditions, total_trades=total, wins=wins,
                            win_rate=wr, avg_pnl_pct=avg_pnl, pattern_type="loss",
                            created_at=now_str, last_evaluated=now_str,
                        ))
                    elif wr >= config.PATTERN_WIN_THRESHOLD and avg_pnl > 3.0:
                        self.patterns.append(Pattern(
                            conditions=conditions, total_trades=total, wins=wins,
                            win_rate=wr, avg_pnl_pct=avg_pnl, pattern_type="win",
                            created_at=now_str, last_evaluated=now_str,
                        ))

        logger.info(
            "Pattern scan: %d loss, %d win patterns",
            sum(1 for p in self.patterns if p.pattern_type == "loss"),
            sum(1 for p in self.patterns if p.pattern_type == "win"),
        )

    def _matches_pattern(self, pattern: Pattern, indicators: dict, context: dict) -> bool:
        for cond_name, cond_value in pattern.conditions.items():
            if cond_name == "rsi_bucket":
                current = self._get_bucket_key("rsi", indicators.get("rsi_1m"))
            elif cond_name == "vol_bucket":
                current = self._get_bucket_key("volume", indicators.get("volume_ratio"))
            elif cond_name == "hour":
                current = str(context.get("hour_utc", ""))
            elif cond_name == "asset":
                current = context.get("asset", "")
            elif cond_name == "ema_cross":
                current = indicators.get("ema_cross", "")
            elif cond_name == "atr_class":
                current = self._classify_atr(context.get("atr_pct", 0))
            else:
                current = None
            if current != cond_value:
                return False
        return True

    # ── 헬퍼 ─────────────────────────────────────────────────

    def _get_bucket_key(self, ind_name: str, value) -> Optional[str]:
        if value is None:
            return None

        bucket_defs = {
            "rsi": [0, 20, 30, 40, 50, 60, 70, 80, 100],
            "vwap": [-999, -0.5, -0.1, 0.1, 0.5, 999],
            "bb": [-999, -1, -0.5, 0, 0.5, 1, 999],
            "volume": [0, 1.0, 2.0, 3.0, 999],
            "ofi": [-999, -0.4, -0.2, 0.2, 0.4, 999],
        }

        if ind_name == "ema":
            return str(value) if value else None
        if ind_name == "macd":
            if isinstance(value, (int, float)):
                return "pos" if value > 0 else "neg" if value < 0 else "zero"
            return None

        boundaries = bucket_defs.get(ind_name)
        if not boundaries:
            return None

        try:
            v = float(value)
        except (TypeError, ValueError):
            return None

        for i in range(len(boundaries) - 1):
            if boundaries[i] <= v < boundaries[i + 1]:
                return f"{boundaries[i]}_{boundaries[i+1]}"
        return None

    def _get_indicator_signal(self, ind_name: str, indicators: dict) -> Optional[str]:
        if ind_name == "rsi":
            rsi = indicators.get("rsi_1m")
            if rsi is not None:
                return "buy" if rsi < 30 else "sell" if rsi > 70 else "neutral"
        elif ind_name == "ema":
            cross = indicators.get("ema_cross")
            if cross == "golden":
                return "buy"
            elif cross == "dead":
                return "sell"
            return "neutral"
        elif ind_name == "vwap":
            pos = indicators.get("vwap_position", 0)
            return "buy" if pos < -0.1 else "sell" if pos > 0.1 else "neutral"
        elif ind_name == "volume":
            ratio = indicators.get("volume_ratio", 0)
            return "buy" if ratio > 2.0 else "neutral"
        elif ind_name == "ofi":
            imb = indicators.get("orderbook_imb", 0)
            return "buy" if imb > 0.3 else "sell" if imb < -0.3 else "neutral"
        return "neutral"

    def _classify_atr(self, atr_pct: float) -> str:
        if atr_pct > 0.3:
            return "high"
        elif atr_pct >= 0.15:
            return "normal"
        return "low"

    def _save_state(self):
        try:
            self._weights_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self._weights_path, "w") as f:
                json.dump({
                    "weights": {k: list(v) for k, v in self.adaptive_weights.items()},
                    "win_rate_tables": self.win_rate_tables,
                    "context_stats": self.context_stats,
                    "trade_count": self._trade_count,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)

            with open(self._patterns_path, "w") as f:
                json.dump({
                    "patterns": [
                        {
                            "conditions": p.conditions,
                            "total_trades": p.total_trades,
                            "wins": p.wins,
                            "win_rate": p.win_rate,
                            "avg_pnl_pct": p.avg_pnl_pct,
                            "pattern_type": p.pattern_type,
                            "created_at": p.created_at,
                            "last_evaluated": p.last_evaluated,
                        }
                        for p in self.patterns
                    ],
                }, f, indent=2)
        except Exception as e:
            logger.error("Save learning state failed: %s", e)

    def backup_daily(self):
        backup_dir = Path(config.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        backup_path = backup_dir / f"trade_history_{date_str}.jsonl"

        if self._history_path.exists() and not backup_path.exists():
            import shutil
            shutil.copy2(self._history_path, backup_path)
            logger.info("Daily backup: %s", backup_path)

    def get_learning_report(self) -> dict:
        loss_patterns = sum(1 for p in self.patterns if p.pattern_type == "loss")
        win_patterns = sum(1 for p in self.patterns if p.pattern_type == "win")

        strongest = max(
            self.adaptive_weights.items(),
            key=lambda x: x[1][0],
            default=("none", (0, 0, 0)),
        )
        weakest = min(
            self.adaptive_weights.items(),
            key=lambda x: x[1][0],
            default=("none", (0, 0, 0)),
        )

        return {
            "total_trades": self._trade_count,
            "blend_ratio": round(self.get_blend_ratio() * 100, 1),
            "active_loss_patterns": loss_patterns,
            "active_win_patterns": win_patterns,
            "strongest_indicator": f"{strongest[0]} ({strongest[1][0]:.1f})",
            "weakest_indicator": f"{weakest[0]} ({weakest[1][0]:.1f})",
            "weights": {k: round(v[0], 2) for k, v in self.adaptive_weights.items()},
        }

    def reset_full(self):
        self.trades = []
        self.adaptive_weights = {}
        self.win_rate_tables = {}
        self.patterns = []
        self.context_stats = {}
        self._trade_count = 0
        self._save_state()
        if self._history_path.exists():
            self._history_path.unlink()
        logger.info("Learning engine fully reset")

    def reset_soft(self):
        self.trades = self.trades[-50:]
        self._trade_count = len(self.trades)
        self.patterns = []
        self._save_state()
        with open(self._history_path, "w") as f:
            for t in self.trades:
                f.write(json.dumps(t) + "\n")
        logger.info("Learning engine soft reset (kept %d trades)", self._trade_count)

    def reset_patterns(self):
        self.patterns = []
        self._save_state()
        logger.info("Patterns reset")
