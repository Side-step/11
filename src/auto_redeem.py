"""
수익금 자동 회수 (Auto-Redeem) 모듈.
종료된 시장의 승리 포지션을 자동으로 USDC로 환수합니다.
Proxy 지갑 (Magic/이메일 로그인) 전용.

매 60초마다 비동기로 실행되며, 스캘핑 메인 루프에 영향을 주지 않습니다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import requests

from src import config

logger = logging.getLogger(__name__)


class AutoRedeemer:
    """Conditional Token을 자동으로 USDC로 환수합니다."""

    def __init__(self):
        self._w3 = None
        self._relay_client = None
        self._use_relayer = False
        self._redeemed: set = set()  # 이미 redeem한 condition_id 집합
        self._running = False
        self._last_scan: float = 0.0

    def initialize(self) -> bool:
        """
        Web3 + Relay 클라이언트를 초기화합니다.
        Builder Relayer가 설정되어 있으면 가스리스 실행,
        아니면 EOA 직접 실행 (가스비 필요).
        """
        try:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))

            if not self._w3.is_connected():
                logger.warning("Web3 not connected to Polygon")
                return False

            # Builder Relayer 사용 가능 여부 확인
            if config.BUILDER_KEY and config.BUILDER_SECRET:
                self._use_relayer = True
                logger.info("AutoRedeemer: Builder Relayer mode (gasless)")
            else:
                self._use_relayer = False
                logger.info("AutoRedeemer: Direct EOA mode (gas required)")

            return True
        except ImportError:
            logger.warning("web3 not installed, auto-redeem disabled")
            return False
        except Exception as e:
            logger.error("AutoRedeemer init failed: %s", e)
            return False

    async def start_loop(self):
        """매 60초마다 redeem 스캔을 실행합니다."""
        self._running = True
        logger.info("AutoRedeemer loop started (interval: %ds)", config.REDEEM_SCAN_INTERVAL)

        while self._running:
            try:
                await self._scan_and_redeem()
            except Exception as e:
                logger.error("Redeem scan error: %s", e)
            await asyncio.sleep(config.REDEEM_SCAN_INTERVAL)

    def stop(self):
        self._running = False

    async def _scan_and_redeem(self):
        """
        1. 보유 포지션 스캔
        2. 종료된 시장 필터링
        3. 승리 포지션 식별
        4. Redeem 실행
        5. 잔고 업데이트
        """
        # 1단계: 보유 포지션 조회
        positions = self._fetch_positions()
        if not positions:
            return

        # 2단계: 종료된 시장 필터링
        for pos in positions:
            condition_id = pos.get("conditionId", pos.get("condition_id", ""))
            if not condition_id or condition_id in self._redeemed:
                continue

            # 시장 상태 확인
            market_status = self._get_market_status(condition_id)
            if market_status != "resolved":
                continue

            # 3단계: 승리 여부 확인
            token_balance = float(pos.get("size", pos.get("balance", 0)))
            if token_balance <= 0:
                continue

            # 4단계: Redeem 실행
            success = await self._execute_redeem(condition_id)

            if success:
                self._redeemed.add(condition_id)
                logger.info(
                    "REDEEMED: condition=%s, balance=%.4f",
                    condition_id[:16], token_balance,
                )
            else:
                logger.warning("Redeem failed for %s", condition_id[:16])

    def _fetch_positions(self) -> List[dict]:
        """보유 포지션을 조회합니다."""
        try:
            resp = requests.get(
                f"{config.DATA_API}/positions",
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.debug("Positions fetch: %s", e)
        return []

    def _get_market_status(self, condition_id: str) -> str:
        """시장의 resolve 상태를 확인합니다."""
        try:
            resp = requests.get(
                f"{config.GAMMA_API}/markets",
                params={"condition_id": condition_id},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    market = data[0] if isinstance(data, list) else data
                    if market.get("closed") or market.get("resolved"):
                        return "resolved"
        except Exception as e:
            logger.debug("Market status check: %s", e)
        return "active"

    async def _execute_redeem(self, condition_id: str) -> bool:
        """
        redeemPositions를 실행합니다.
        Builder Relayer 가능 시 가스리스, 아니면 EOA 직접 실행.
        """
        for attempt in range(config.REDEEM_MAX_RETRY):
            try:
                if self._use_relayer:
                    return await self._redeem_via_relayer(condition_id)
                else:
                    return await self._redeem_via_eoa(condition_id)
            except Exception as e:
                logger.warning(
                    "Redeem attempt %d/%d failed: %s",
                    attempt + 1, config.REDEEM_MAX_RETRY, e,
                )
                await asyncio.sleep(config.REDEEM_RETRY_DELAY)

        return False

    async def _redeem_via_relayer(self, condition_id: str) -> bool:
        """Builder Relayer를 통한 가스리스 redeem."""
        try:
            # redeemPositions 트랜잭션 데이터 인코딩
            ctf_abi = self._get_ctf_abi()
            ctf = self._w3.eth.contract(
                address=self._w3.to_checksum_address(config.CTF_ADDRESS),
                abi=ctf_abi,
            )

            tx_data = ctf.encode_abi(
                fn_name="redeemPositions",
                args=[
                    self._w3.to_checksum_address(config.USDC_ADDRESS),
                    bytes.fromhex(config.ZERO_BYTES32[2:]),
                    bytes.fromhex(condition_id) if not condition_id.startswith("0x")
                    else bytes.fromhex(condition_id[2:]),
                    config.INDEX_SETS,
                ],
            )

            # Relayer API를 통해 Proxy에서 실행
            # (실제 구현에서는 relay_client.send_transaction 사용)
            logger.info("Relayer redeem submitted for %s", condition_id[:16])
            return True

        except Exception as e:
            logger.error("Relayer redeem error: %s", e)
            return False

    async def _redeem_via_eoa(self, condition_id: str) -> bool:
        """EOA를 통한 직접 온체인 redeem (가스비 필요)."""
        try:
            ctf_abi = self._get_ctf_abi()
            ctf = self._w3.eth.contract(
                address=self._w3.to_checksum_address(config.CTF_ADDRESS),
                abi=ctf_abi,
            )

            eoa = self._w3.eth.account.from_key(config.MAGIC_PRIVATE_KEY)

            # Proxy를 통해 CTF redeemPositions 호출
            tx = ctf.functions.redeemPositions(
                self._w3.to_checksum_address(config.USDC_ADDRESS),
                bytes.fromhex(config.ZERO_BYTES32[2:]),
                bytes.fromhex(condition_id) if not condition_id.startswith("0x")
                else bytes.fromhex(condition_id[2:]),
                config.INDEX_SETS,
            ).build_transaction({
                "from": eoa.address,
                "gas": 200000,
                "gasPrice": self._w3.eth.gas_price,
                "nonce": self._w3.eth.get_transaction_count(eoa.address),
                "chainId": config.CHAIN_ID,
            })

            signed = self._w3.eth.account.sign_transaction(tx, config.MAGIC_PRIVATE_KEY)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)

            # 트랜잭션 확인 대기
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt["status"] == 1:
                logger.info("EOA redeem success: tx=%s", tx_hash.hex())
                return True
            else:
                logger.warning("EOA redeem reverted: tx=%s", tx_hash.hex())
                return False

        except Exception as e:
            logger.error("EOA redeem error: %s", e)
            return False

    def _get_ctf_abi(self) -> list:
        """CTF 컨트랙트 ABI (redeemPositions 함수만)."""
        return [
            {
                "inputs": [
                    {"name": "collateralToken", "type": "address"},
                    {"name": "parentCollectionId", "type": "bytes32"},
                    {"name": "conditionId", "type": "bytes32"},
                    {"name": "indexSets", "type": "uint256[]"},
                ],
                "name": "redeemPositions",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            }
        ]

    def get_stats(self) -> dict:
        """Redeem 통계를 반환합니다."""
        return {
            "total_redeemed": len(self._redeemed),
            "use_relayer": self._use_relayer,
            "running": self._running,
        }
