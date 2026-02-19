"""
Polymarket CLOB/Gamma/Data API 통합 모듈.
주문 생성, 시장 조회, 오더북 조회, 포지션 관리를 담당합니다.
공식 API 규칙을 엄격히 준수합니다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from src import config
from src.indicators.orderbook import OrderbookLevel

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    """Polymarket 시장 정보."""
    condition_id: str
    question: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    tokens: List[Dict[str, str]] = field(default_factory=list)
    yes_token_id: str = ""
    no_token_id: str = ""
    yes_price: float = 0.0
    no_price: float = 0.0
    volume_24h: float = 0.0
    active: bool = True
    closed: bool = False
    end_date: str = ""
    minimum_order_size: float = 0.0
    tick_size: float = 0.01
    seconds_delay: int = 0
    fee_rate_bps: int = 0
    neg_risk: bool = False
    enable_order_book: bool = True
    accepting_orders: bool = True
    market_type: str = "Type B"  # "Type A" (5min) or "Type B" (general)

    @property
    def is_xrp(self) -> bool:
        """XRP/Ripple 관련 시장인지 확인."""
        text = f"{self.question} {self.description} {' '.join(self.tags)}"
        return any(kw in text for kw in config.EXCLUDED_KEYWORDS)

    @property
    def grade(self) -> str:
        if self.is_xrp:
            return "X"  # excluded
        if (self.volume_24h >= config.GRADE_A_VOLUME
                and config.GRADE_A_PRICE_MIN <= self.yes_price <= config.GRADE_A_PRICE_MAX):
            return "A"
        if self.volume_24h >= config.MIN_DAILY_VOLUME:
            return "B"
        return "C"


class PolymarketClient:
    """Polymarket API 클라이언트 (CLOB + Gamma + Data)."""

    def __init__(self):
        self._clob_client = None
        self._initialized = False
        self._api_creds = None

    def initialize(self) -> bool:
        """
        py-clob-client를 초기화합니다.
        signature_type=1 (Proxy/Magic 지갑).
        """
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            self._clob_client = ClobClient(
                host=config.CLOB_HOST,
                key=config.MAGIC_PRIVATE_KEY,
                chain_id=config.CHAIN_ID,
                signature_type=config.SIGNATURE_TYPE,
                funder=config.PROXY_WALLET_ADDRESS,
            )

            # API 인증키 설정
            if config.POLY_API_KEY:
                creds = ApiCreds(
                    api_key=config.POLY_API_KEY,
                    api_secret=config.POLY_API_SECRET,
                    api_passphrase=config.POLY_API_PASSPHRASE,
                )
                self._clob_client.set_api_creds(creds)
            else:
                creds = self._clob_client.create_or_derive_api_creds()
                self._clob_client.set_api_creds(creds)
                logger.info("API creds derived: key=%s", creds.api_key)

            self._initialized = True
            logger.info("PolymarketClient initialized (signature_type=1)")
            return True

        except Exception as e:
            logger.error("Failed to initialize PolymarketClient: %s", e)
            return False

    # ── 시장 조회 (Gamma API) ────────────────────────────────

    def fetch_crypto_markets(self) -> List[MarketInfo]:
        """Gamma API에서 크립토 태그 시장 목록을 조회합니다."""
        markets = []
        try:
            resp = requests.get(
                f"{config.GAMMA_API}/markets",
                params={"tag": "crypto", "active": "true", "closed": "false"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            for m in data:
                mi = self._parse_market(m)
                if mi and not mi.is_xrp:
                    markets.append(mi)

        except Exception as e:
            logger.error("Gamma API fetch failed: %s", e)

        return markets

    def fetch_events(self, tag: str = "crypto") -> List[dict]:
        """이벤트 목록을 조회합니다."""
        try:
            resp = requests.get(
                f"{config.GAMMA_API}/events",
                params={"tag": tag, "active": "true", "closed": "false"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Gamma events fetch failed: %s", e)
            return []

    def _parse_market(self, raw: dict) -> Optional[MarketInfo]:
        """Gamma API 응답을 MarketInfo로 파싱."""
        try:
            tokens = raw.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), {})
            no_token = next((t for t in tokens if t.get("outcome") == "No"), {})

            mi = MarketInfo(
                condition_id=raw.get("conditionId", raw.get("condition_id", "")),
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                tags=[t.strip() for t in raw.get("tags", "").split(",")]
                     if isinstance(raw.get("tags"), str) else raw.get("tags", []),
                tokens=tokens,
                yes_token_id=yes_token.get("token_id", ""),
                no_token_id=no_token.get("token_id", ""),
                yes_price=float(yes_token.get("price", 0)),
                no_price=float(no_token.get("price", 0)),
                volume_24h=float(raw.get("volume24hr", raw.get("volume_num", 0) or 0)),
                active=raw.get("active", True),
                closed=raw.get("closed", False),
                end_date=raw.get("endDate", raw.get("end_date_iso", "")),
                minimum_order_size=float(raw.get("minimum_order_size", 0)),
                tick_size=float(raw.get("minimum_tick_size", 0.01)),
                seconds_delay=int(raw.get("seconds_delay", 0)),
                neg_risk=raw.get("neg_risk", False),
                enable_order_book=raw.get("enable_order_book", True),
                accepting_orders=raw.get("accepting_orders", True),
            )
            return mi
        except Exception as e:
            logger.debug("Failed to parse market: %s", e)
            return None

    # ── 오더북 / 가격 ────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> tuple[List[OrderbookLevel], List[OrderbookLevel]]:
        """토큰의 오더북을 조회합니다. (bids, asks) 반환."""
        if not self._initialized:
            return [], []
        try:
            book = self._clob_client.get_order_book(token_id)
            bids = [
                OrderbookLevel(price=float(b["price"]), size=float(b["size"]))
                for b in (book.bids or [])
            ]
            asks = [
                OrderbookLevel(price=float(a["price"]), size=float(a["size"]))
                for a in (book.asks or [])
            ]
            # 정렬: bids 내림차순, asks 오름차순
            bids.sort(key=lambda x: -x.price)
            asks.sort(key=lambda x: x.price)
            return bids, asks
        except Exception as e:
            logger.error("Orderbook fetch failed for %s: %s", token_id, e)
            return [], []

    def get_price(self, token_id: str, side: str = "buy") -> float:
        """현재 가격을 조회합니다."""
        if not self._initialized:
            return 0.0
        try:
            return float(self._clob_client.get_price(token_id, side))
        except Exception as e:
            logger.error("Price fetch failed: %s", e)
            return 0.0

    def get_midpoint(self, token_id: str) -> float:
        if not self._initialized:
            return 0.0
        try:
            return float(self._clob_client.get_midpoint(token_id))
        except Exception as e:
            logger.error("Midpoint fetch failed: %s", e)
            return 0.0

    def get_tick_size(self, condition_id: str) -> float:
        if not self._initialized:
            return 0.01
        try:
            return float(self._clob_client.get_tick_size(condition_id))
        except Exception as e:
            logger.debug("Tick size fetch failed: %s", e)
            return 0.01

    def get_fee_rate(self, token_id: str) -> int:
        """수수료율(BPS)을 조회합니다."""
        try:
            resp = requests.get(
                f"{config.CLOB_HOST}/fee-rate",
                params={"token_id": token_id},
                timeout=5,
            )
            resp.raise_for_status()
            return int(resp.json().get("fee_rate_bps", 0))
        except Exception as e:
            logger.debug("Fee rate fetch failed: %s", e)
            return 0

    # ── 주문 실행 ─────────────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        tick_size: float = 0.01,
        post_only: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        GTC 리밋 오더를 제출합니다.
        메이커 수수료 0%를 위해 기본적으로 postOnly=true.
        """
        if not self._initialized:
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            # tick_size 배수로 가격 정렬
            price = round(round(price / tick_size) * tick_size, 6)

            order_side = BUY if side.lower() == "buy" else SELL
            order_args = OrderArgs(
                price=price,
                size=size,
                side=order_side,
                token_id=token_id,
            )
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(
                signed,
                order_type=OrderType.GTC,
                postOnly=post_only,
            )
            if resp.get("errorMsg"):
                logger.error("Order rejected: %s", resp["errorMsg"])
                return None
            logger.info(
                "LIMIT %s %s: price=%.4f size=%.2f -> %s",
                side, token_id[:12], price, size, resp.get("orderID"),
            )
            return resp
        except Exception as e:
            logger.error("Limit order failed: %s", e)
            return None

    def place_fak_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        tick_size: float = 0.01,
    ) -> Optional[Dict[str, Any]]:
        """
        FAK (Fill-And-Kill) 주문: 즉시 체결 가능한 만큼 체결, 나머지 취소.
        손절/시간청산에 적합.
        """
        if not self._initialized:
            return None

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            price = round(round(price / tick_size) * tick_size, 6)
            order_side = BUY if side.lower() == "buy" else SELL
            order_args = OrderArgs(
                price=price,
                size=size,
                side=order_side,
                token_id=token_id,
            )
            signed = self._clob_client.create_order(order_args)
            resp = self._clob_client.post_order(signed, order_type=OrderType.FAK)

            if resp.get("errorMsg"):
                logger.error("FAK order rejected: %s", resp["errorMsg"])
                return None
            logger.info(
                "FAK %s %s: price=%.4f size=%.2f -> %s",
                side, token_id[:12], price, size, resp.get("orderID"),
            )
            return resp
        except Exception as e:
            logger.error("FAK order failed: %s", e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """주문을 취소합니다."""
        if not self._initialized:
            return False
        try:
            self._clob_client.cancel(order_id=order_id)
            logger.info("Cancelled order: %s", order_id)
            return True
        except Exception as e:
            logger.error("Cancel failed: %s", e)
            return False

    def cancel_all(self) -> bool:
        """모든 미체결 주문을 취소합니다."""
        if not self._initialized:
            return False
        try:
            self._clob_client.cancel_all()
            logger.info("Cancelled all orders")
            return True
        except Exception as e:
            logger.error("Cancel all failed: %s", e)
            return False

    def cancel_market_orders(self, condition_id: str) -> bool:
        if not self._initialized:
            return False
        try:
            self._clob_client.cancel_market_orders(condition_id)
            return True
        except Exception as e:
            logger.error("Cancel market orders failed: %s", e)
            return False

    # ── 잔고 / 포지션 ────────────────────────────────────────

    def get_balance(self) -> float:
        """USDC 잔고를 조회합니다 (CLOB API → on-chain 순서로 시도)."""
        # 방법 1: py-clob-client의 balance-allowance API
        if self._initialized:
            try:
                result = self._clob_client.get_balance_allowance()
                if result:
                    if isinstance(result, dict):
                        raw = result.get("balance", 0)
                    else:
                        raw = getattr(result, "balance", 0)
                    bal = float(raw)
                    # USDC 6 decimals: raw 단위가 매우 크면 변환
                    usdc = bal / 1e6 if bal > 100_000 else bal
                    if usdc > 0:
                        logger.info("Balance (CLOB API): $%.2f", usdc)
                        return usdc
            except Exception as e:
                logger.debug("CLOB balance-allowance failed: %s", e)

        # 방법 2: Polygon RPC로 프록시 지갑의 on-chain USDC 잔고 조회
        bal = self._query_onchain_usdc()
        if bal > 0:
            return bal

        logger.warning("All balance methods returned 0")
        return 0.0

    def _query_onchain_usdc(self) -> float:
        """Polygon RPC를 통해 프록시 지갑의 USDC 잔고를 조회합니다."""
        proxy = config.PROXY_WALLET_ADDRESS
        if not proxy:
            return 0.0

        # ERC20 balanceOf(address) selector = 0x70a08231
        addr_hex = proxy.lower().replace("0x", "").zfill(64)
        call_data = f"0x70a08231{addr_hex}"

        for usdc_addr in [config.USDC_NATIVE, config.USDC_ADDRESS]:
            try:
                resp = requests.post(
                    config.POLYGON_RPC_URL,
                    json={
                        "jsonrpc": "2.0",
                        "method": "eth_call",
                        "params": [{"to": usdc_addr, "data": call_data}, "latest"],
                        "id": 1,
                    },
                    timeout=10,
                )
                result = resp.json().get("result", "0x0")
                balance = int(result, 16) / 1e6  # USDC = 6 decimals
                if balance > 0:
                    logger.info("Balance (on-chain %s): $%.2f", usdc_addr[:10], balance)
                    return balance
            except Exception as e:
                logger.debug("On-chain USDC query failed (%s): %s", usdc_addr[:10], e)

        return 0.0

    def get_positions(self) -> List[dict]:
        """보유 포지션 목록을 조회합니다."""
        try:
            resp = requests.get(
                f"{config.DATA_API}/positions",
                headers=self._auth_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("Positions fetch: %s", e)
            return []

    def get_order_status(self, order_id: str) -> Optional[dict]:
        """주문 상태를 조회합니다."""
        if not self._initialized:
            return None
        try:
            resp = requests.get(
                f"{config.CLOB_HOST}/data/order/{order_id}",
                headers=self._auth_headers(),
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("Order status fetch: %s", e)
            return None

    def _auth_headers(self) -> dict:
        """인증 헤더 (Data API용)."""
        return {}

    # ── NegRisk 확인 ──────────────────────────────────────────

    def is_neg_risk(self, token_id: str) -> bool:
        if not self._initialized:
            return False
        try:
            return self._clob_client.get_neg_risk(token_id)
        except Exception:
            return False
