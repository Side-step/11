#!/usr/bin/env python3
"""
Polymarket 크립토 복리 스캘핑 봇 - 엔트리 포인트.

사용법:
    python main.py                  # 기본 실행 (.env의 OPERATION_MODE 사용)
    OPERATION_MODE=observe python main.py   # 관찰 모드
    OPERATION_MODE=full python main.py      # 전체 운용 모드
"""
import asyncio
import logging
import signal
import sys

from src.orchestrator import ScalpingOrchestrator


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("bot.log", mode="a"),
        ],
    )
    # 외부 라이브러리 로그 레벨 조정 (HTTP 요청 로그 숨김)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("Polymarket Compound Scalping Bot v3.0")
    logger.info("=" * 60)

    orchestrator = ScalpingOrchestrator()

    # Graceful shutdown
    loop = asyncio.get_event_loop()

    def handle_signal():
        logger.info("Received shutdown signal")
        asyncio.create_task(orchestrator.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await orchestrator.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        await orchestrator.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
