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
                self._api_creds = creds
            else:
                creds = self._clob_client.create_or_derive_api_creds()
                self._clob_client.set_api_creds(creds)
                self._api_creds = creds
                logger.info("API creds derived: key=%s", creds.api_key)

            self._initialized = True
            logger.info("PolymarketClient initialized (signature_type=1)")
            return True

        except Exception as e:
            logger.error("Failed to initialize PolymarketClient: %s", e)
            return False

    # ── 시장 조회 (Gamma API) ────────────────────────────────

    def fetch_crypto_markets(self) -> List[MarketInfo]:
        """
        Gamma API에서 활성 시장을 볼륨 순으로 가져온 후,
        클라이언트 측에서 크립토 가격 예측 시장을 필터링합니다.

        tag=crypto는 잘못된 결과를 반환하므로 사용하지 않습니다.
        대신 전체 시장을 볼륨 순으로 가져와 detect_asset()으로 필터링합니다.
        """
        all_markets: List[MarketInfo] = []
        crypto_markets: List[MarketInfo] = []

        # 페이지네이션으로 충분한 시장 수집 (볼륨 순)
        for offset in range(0, 600, 100):
            try:
                resp = requests.get(
                    f"{config.GAMMA_API}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "order": "volume24hr",
                        "ascending": "false",
                        "limit": 100,
                        "offset": offset,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break  # 더 이상 결과 없음

                for m in batch:
                    mi = self._parse_market(m)
                    if mi and not mi.is_xrp:
                        all_markets.append(mi)

                if len(batch) < 100:
                    break  # 마지막 페이지

            except Exception as e:
                logger.error("Gamma API fetch failed (offset=%d): %s", offset, e)
                break

        # 클라이언트 측 크립토 필터링
        for mi in all_markets:
            asset = config.detect_asset(mi.question)
            if asset:
                crypto_markets.append(mi)

        logger.info(
            "Market discovery: %d total active -> %d crypto markets (assets: %s)",
            len(all_markets),
            len(crypto_markets),
            ", ".join(sorted(set(
                config.detect_asset(m.question) or "?"
                for m in crypto_markets
            ))),
        )

        return crypto_markets

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
            # 토큰 ID 파싱: Gamma API는 clobTokenIds + outcomes를 별도 필드로 반환
            yes_token_id = ""
            no_token_id = ""
            yes_price = 0.0
            no_price = 0.0

            # 방법 1: tokens 배열 (일부 엔드포인트에서 제공)
            tokens = raw.get("tokens", [])
            if tokens and isinstance(tokens, list) and isinstance(tokens[0], dict):
                yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), {})
                no_token = next((t for t in tokens if t.get("outcome") == "No"), {})
                yes_token_id = yes_token.get("token_id", "")
                no_token_id = no_token.get("token_id", "")
                yes_price = float(yes_token.get("price", 0))
                no_price = float(no_token.get("price", 0))

            # 방법 2: clobTokenIds + outcomes (Gamma API 기본 형식)
            if not yes_token_id:
                import json as _json
                clob_ids_raw = raw.get("clobTokenIds", "[]")
                outcomes_raw = raw.get("outcomes", "[]")
                prices_raw = raw.get("outcomePrices", "[]")

                # JSON 문자열 또는 리스트 모두 처리
                if isinstance(clob_ids_raw, str):
                    clob_ids = _json.loads(clob_ids_raw)
                else:
                    clob_ids = clob_ids_raw or []

                if isinstance(outcomes_raw, str):
                    outcomes = _json.loads(outcomes_raw)
                else:
                    outcomes = outcomes_raw or []

                if isinstance(prices_raw, str):
                    prices = _json.loads(prices_raw)
                else:
                    prices = prices_raw or []

                for i, outcome in enumerate(outcomes):
                    tid = clob_ids[i] if i < len(clob_ids) else ""
                    price = float(prices[i]) if i < len(prices) else 0.0
                    if outcome == "Yes":
                        yes_token_id = tid
                        yes_price = price
                    elif outcome == "No":
                        no_token_id = tid
                        no_price = price

            if not yes_token_id and not no_token_id:
                logger.debug("No token IDs found for market: %s",
                             raw.get("question", "")[:60])
                return None

            # 태그 파싱
            tags_raw = raw.get("tags", [])
            if isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            else:
                tags = tags_raw or []

            mi = MarketInfo(
                condition_id=raw.get("conditionId", raw.get("condition_id", "")),
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                tags=tags,
                tokens=tokens if isinstance(tokens, list) else [],
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                yes_price=yes_price,
                no_price=no_price,
                volume_24h=float(raw.get("volume24hr", 0) or 0),
                active=raw.get("active", True),
                closed=raw.get("closed", False),
                end_date=raw.get("endDate", raw.get("end_date_iso", "")),
                minimum_order_size=float(raw.get("minimumOrderSize",
                                         raw.get("minimum_order_size", 0)) or 0),
                tick_size=float(raw.get("orderPriceMinTickSize",
                                raw.get("minimum_tick_size", 0.01)) or 0.01),
                seconds_delay=int(raw.get("secondsDelay",
                                  raw.get("seconds_delay", 0)) or 0),
                neg_risk=raw.get("negRisk", raw.get("neg_risk", False)),
                enable_order_book=raw.get("enableOrderBook",
                                  raw.get("enable_order_book", True)),
                accepting_orders=raw.get("acceptingOrders",
                                 raw.get("accepting_orders", True)),
            )

            if mi.yes_token_id:
                logger.debug("Parsed market: %s | yes=%s... no=%s... | yes_p=%.4f",
                             mi.question[:50], mi.yes_token_id[:16],
                             mi.no_token_id[:16], mi.yes_price)
            return mi
        except Exception as e:
            logger.error("Failed to parse market: %s (raw keys: %s)",
                         e, list(raw.keys())[:10])
            return None

    # ── 오더북 / 가격 ────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> tuple[List[OrderbookLevel], List[OrderbookLevel]]:
        """
        토큰의 오더북을 조회합니다. (bids, asks) 반환.

        공식 API: GET /book?token_id=...
        응답: OrderBookSummary 객체
          - bids: [OrderSummary(price="0.45", size="100"), ...]
          - asks: [OrderSummary(price="0.46", size="150"), ...]
          price/size는 문자열(str)
        """
        if not self._initialized:
            return [], []
        try:
            book = self._clob_client.get_order_book(token_id)

            # book은 OrderBookSummary 객체 (속성 접근)
            raw_bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
            raw_asks = book.asks if hasattr(book, "asks") else book.get("asks", [])

            bids = []
            for b in (raw_bids or []):
                # OrderSummary 객체: .price, .size (문자열)
                p = float(getattr(b, "price", None) or b.get("price", 0) if isinstance(b, dict) else b.price)
                s = float(getattr(b, "size", None) or b.get("size", 0) if isinstance(b, dict) else b.size)
                bids.append(OrderbookLevel(price=p, size=s))
            asks = []
            for a in (raw_asks or []):
                p = float(getattr(a, "price", None) or a.get("price", 0) if isinstance(a, dict) else a.price)
                s = float(getattr(a, "size", None) or a.get("size", 0) if isinstance(a, dict) else a.size)
                asks.append(OrderbookLevel(price=p, size=s))

            # 정렬: bids 내림차순, asks 오름차순
            bids.sort(key=lambda x: -x.price)
            asks.sort(key=lambda x: x.price)
            return bids, asks
        except Exception as e:
            logger.error("Orderbook fetch failed for %s: %s", token_id, e)
            return [], []

    @staticmethod
    def _extract_float(result, *keys) -> float:
        """API 반환값에서 float을 추출합니다. dict/객체/스칼라 모두 지원."""
        if result is None:
            return 0.0
        if isinstance(result, (int, float)):
            return float(result)
        if isinstance(result, str):
            return float(result) if result else 0.0
        # dict인 경우 여러 키 시도
        if isinstance(result, dict):
            for k in keys:
                if k in result:
                    return float(result[k])
            return 0.0
        # 객체인 경우 속성으로 시도
        for k in keys:
            val = getattr(result, k, None)
            if val is not None:
                return float(val)
        return 0.0

    def get_price(self, token_id: str, side: str = "buy") -> float:
        """
        현재 가격을 조회합니다.

        공식 API: GET /price?token_id=...&side=BUY
        응답: {"price": 0.45}
        """
        if not self._initialized:
            return 0.0
        try:
            result = self._clob_client.get_price(token_id, side)
            return self._extract_float(result, "price")
        except Exception as e:
            logger.error("Price fetch failed: %s", e)
            return 0.0

    def get_midpoint(self, token_id: str) -> float:
        """
        미드포인트 가격을 조회합니다.

        공식 API: GET /midpoint?token_id=...
        응답: {"mid_price": "0.45"}  (문자열!)
        """
        if not self._initialized:
            return 0.0
        try:
            result = self._clob_client.get_midpoint(token_id)
            return self._extract_float(result, "mid_price", "mid", "price")
        except Exception as e:
            logger.error("Midpoint fetch failed: %s", e)
            return 0.0

    def get_tick_size(self, condition_id: str) -> float:
        if not self._initialized:
            return 0.01
        try:
            result = self._clob_client.get_tick_size(condition_id)
            val = self._extract_float(result, "minimum_tick_size", "tick_size")
            return val if val > 0 else 0.01
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

    def _get_market_options(self, token_id: str, tick_size: float = 0.01):
        """마켓 옵션(tickSize, negRisk)을 조회합니다. 공식 문서 필수 파라미터."""
        neg_risk = self.is_neg_risk(token_id)
        # tick_size를 문자열로 변환 (공식 문서: "0.1", "0.01", "0.001", "0.0001")
        ts_str = str(tick_size)
        return {"tick_size": ts_str, "neg_risk": neg_risk}

    def _parse_order_resp(self, resp, label: str) -> Optional[Dict[str, Any]]:
        """주문 응답을 파싱합니다."""
        if resp is None:
            return None
        # dict 또는 객체 모두 처리
        if isinstance(resp, dict):
            d = resp
        else:
            d = {
                "orderID": getattr(resp, "orderID", getattr(resp, "order_id", "")),
                "status": getattr(resp, "status", ""),
                "errorMsg": getattr(resp, "errorMsg", getattr(resp, "error_msg", "")),
            }
        err = d.get("errorMsg", "")
        if err:
            logger.error("%s rejected: %s", label, err)
            return None
        return d

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

        공식 문서: createAndPostOrder({tokenID, price, size, side}, {tickSize, negRisk}, GTC)
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
            resp = self._clob_client.post_order(signed, OrderType.GTC)
            result = self._parse_order_resp(resp, "LIMIT")
            if result:
                logger.info(
                    "LIMIT %s %s: price=%.4f size=%.2f -> %s (status=%s)",
                    side, token_id[:16], price, size,
                    result.get("orderID", "?"), result.get("status", "?"),
                )
            return result
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

        공식 문서: create_order → post_order(signed, OrderType.FAK)
        price는 worst-price limit (슬리피지 보호).
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
            resp = self._clob_client.post_order(signed, OrderType.FAK)

            result = self._parse_order_resp(resp, "FAK")
            if result:
                logger.info(
                    "FAK %s %s: price=%.4f size=%.2f -> %s (status=%s)",
                    side, token_id[:16], price, size,
                    result.get("orderID", "?"), result.get("status", "?"),
                )
            return result
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
        """USDC 잔고를 조회합니다 (CLOB API → REST → on-chain 순서로 시도)."""

        # 방법 1: py-clob-client의 balance-allowance API (asset_type=COLLATERAL)
        if self._initialized:
            bal = self._balance_via_clob()
            if bal > 0:
                return bal

        # 방법 2: CLOB REST API 직접 호출
        bal = self._balance_via_rest()
        if bal > 0:
            return bal

        # 방법 3: Polygon RPC로 on-chain USDC (지갑 + CTF Exchange)
        bal = self._balance_onchain()
        if bal > 0:
            return bal

        logger.warning("All balance methods returned 0")
        return 0.0

    def _balance_via_clob(self) -> float:
        """py-clob-client로 CLOB 잔고 조회 (공식 예제 기반)."""
        # 공식 예제 방식: params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            result = self._clob_client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            logger.info("CLOB balance-allowance raw response: %s", result)
            bal = self._parse_balance_result(result, "CLOB")
            if bal > 0:
                return bal
        except ImportError:
            logger.info("BalanceAllowanceParams not available in this py-clob-client version")
        except Exception as e:
            logger.info("CLOB balance-allowance failed: %s", e)

        return 0.0

    def _parse_balance_result(self, result, label: str) -> float:
        """balance-allowance 응답에서 USDC 잔고 추출."""
        if not result:
            return 0.0
        if isinstance(result, dict):
            raw = result.get("balance", 0)
        else:
            raw = getattr(result, "balance", 0)
        bal = float(raw)
        # USDC 6 decimals: raw 단위가 매우 크면 wei→dollar 변환
        usdc = bal / 1e6 if bal > 100_000 else bal
        if usdc > 0:
            logger.info("Balance (%s): $%.2f (raw=%s)", label, usdc, raw)
        else:
            logger.info("Balance (%s): $0 (raw=%s)", label, raw)
        return usdc

    def _balance_via_rest(self) -> float:
        """CLOB REST API로 직접 잔고 조회."""
        try:
            # derive된 API 키로 인증 헤더 생성
            headers = {}
            if self._initialized and hasattr(self._clob_client, "creds") and self._clob_client.creds:
                creds = self._clob_client.creds
                headers = {
                    "POLY_API_KEY": getattr(creds, "api_key", ""),
                    "POLY_PASSPHRASE": getattr(creds, "api_passphrase", ""),
                }
            resp = requests.get(
                f"{config.CLOB_HOST}/balance-allowance",
                params={"asset_type": "COLLATERAL"},
                headers=headers,
                timeout=10,
            )
            logger.info("REST balance-allowance: status=%d body=%s",
                        resp.status_code, resp.text[:200])
            if resp.status_code == 200:
                data = resp.json()
                raw = data.get("balance", 0)
                bal = float(raw)
                usdc = bal / 1e6 if bal > 100_000 else bal
                if usdc > 0:
                    logger.info("Balance (REST): $%.2f", usdc)
                    return usdc
        except Exception as e:
            logger.info("REST balance fetch failed: %s", e)
        return 0.0

    def _balance_onchain(self) -> float:
        """Polygon RPC로 프록시 지갑의 USDC 잔고 조회 (지갑 + CTF Exchange)."""
        proxy = config.PROXY_WALLET_ADDRESS
        if not proxy:
            logger.info("No PROXY_WALLET_ADDRESS set for on-chain query")
            return 0.0

        addr_hex = proxy.lower().replace("0x", "").zfill(64)

        # 1) 지갑의 직접 USDC 잔고
        for usdc_addr in [config.USDC_NATIVE, config.USDC_ADDRESS]:
            bal = self._erc20_balance_of(usdc_addr, addr_hex, f"wallet-{usdc_addr[:10]}")
            if bal > 0:
                return bal

        # 2) CTF Exchange에 예치된 USDC 잔고
        #    balanceOf(address, tokenId) — ERC1155
        #    USDC collateral의 tokenId = 0
        try:
            # ERC1155 balanceOf(address,uint256) = 0x00fdd58e
            token_id_hex = "0" * 64  # tokenId = 0 for collateral
            call_data = f"0x00fdd58e{addr_hex}{token_id_hex}"
            for exchange in [config.CTF_EXCHANGE_ADDRESS, config.NEGRISK_CTF_EXCHANGE]:
                bal = self._rpc_call_balance(exchange, call_data, f"CTF-{exchange[:10]}")
                if bal > 0:
                    return bal
        except Exception as e:
            logger.info("CTF Exchange balance query failed: %s", e)

        return 0.0

    def _erc20_balance_of(self, contract: str, addr_hex: str, label: str) -> float:
        """ERC20 balanceOf 호출."""
        call_data = f"0x70a08231{addr_hex}"
        return self._rpc_call_balance(contract, call_data, label)

    def _rpc_call_balance(self, contract: str, call_data: str, label: str) -> float:
        """Polygon RPC eth_call 실행 후 USDC 잔고 반환. 여러 RPC 폴백 시도."""
        rpc_urls = [config.POLYGON_RPC_URL] + getattr(config, "POLYGON_RPC_FALLBACKS", [])

        for rpc_url in rpc_urls:
            try:
                resp = requests.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "method": "eth_call",
                        "params": [{"to": contract, "data": call_data}, "latest"],
                        "id": 1,
                    },
                    timeout=10,
                )
                result = resp.json().get("result", "0x0")
                if result and result != "0x":
                    balance = int(result, 16) / 1e6  # USDC = 6 decimals
                    if balance > 0:
                        logger.info("Balance (%s): $%.2f via %s", label, balance, rpc_url)
                    return balance
            except Exception as e:
                logger.debug("RPC balance query failed (%s via %s): %s", label, rpc_url, e)

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
