"""
텔레그램 봇 알림 시스템.
거래 체결/청산, 복리 상태, 비상 중단, 일일/주간 리포트를 전송합니다.
모든 알림은 비동기 전송으로 거래 실행에 영향을 주지 않습니다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from src import config
from src.bot_state import BotState, Position, ExitReason

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """텔레그램 알림 전송기."""

    def __init__(self):
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self._enabled = bool(self.token and self.chat_id)
        self._last_sent: dict[str, float] = {}  # 알림 유형별 마지막 전송 시각
        self._min_interval = 10  # 동일 유형 최소 간격 (초)
        self._msg_count_1m = 0
        self._msg_count_reset: float = time.time()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str, priority: str = "P2") -> bool:
        """
        텔레그램 메시지를 전송합니다.
        priority: P1(즉시), P2(즉시), P3(배치), P4(예약)
        """
        if not self._enabled:
            return False

        # 과도한 알림 방지
        now = time.time()
        if now - self._msg_count_reset >= 60:
            self._msg_count_1m = 0
            self._msg_count_reset = now

        if self._msg_count_1m >= 10 and priority not in ("P1", "P2"):
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            self._msg_count_1m += 1
                            return True
                        logger.warning("Telegram send failed: %d", resp.status)
            except Exception as e:
                logger.debug("Telegram send error (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(5)

        return False

    # ── 거래 체결 알림 ────────────────────────────────────────

    async def notify_entry(self, bot: BotState, pos: Position):
        """포지션 진입 알림."""
        text = (
            f"\U0001f7e2 <b>진입 완료</b>\n"
            f"시장: {_esc(pos.market_question)}\n"
            f"유형: {pos.market_type}\n"
            f"방향: {pos.direction} 매수\n"
            f"진입가: {pos.entry_price:.4f}\n"
            f"베팅: ${pos.bet_amount:.2f} (잔고의 {bot.current_bet_pct*100:.0f}%)\n"
            f"목표 익절: {pos.tp_price:.4f}\n"
            f"손절 라인: {pos.sl_price:.4f}\n"
            f"전략: {pos.strategy}\n"
            f"컨플루언스: {pos.confluence_score:.1f}점"
        )
        if pos.boost_factor > 1.0:
            text += f" (부스트 x{pos.boost_factor:.1f})"
        await self.send(text, "P2")

    async def notify_exit(
        self,
        bot: BotState,
        pos: Position,
        reason: ExitReason,
    ):
        """포지션 청산 알림."""
        emoji = {
            ExitReason.TAKE_PROFIT: "\U0001f7e2",
            ExitReason.STOP_LOSS: "\U0001f534",
            ExitReason.TIME_LIMIT: "\u23f0",
            ExitReason.TRAILING: "\U0001f7e2",
            ExitReason.INDICATOR_REVERSAL: "\U0001f7e1",
            ExitReason.EXPIRY: "\U0001f3af",
            ExitReason.EMERGENCY: "\U0001f6a8",
            ExitReason.MANUAL: "\u270b",
        }.get(reason, "\u2753")

        label = {
            ExitReason.TAKE_PROFIT: "익절 성공",
            ExitReason.STOP_LOSS: "손절",
            ExitReason.TIME_LIMIT: "시간청산",
            ExitReason.TRAILING: "트레일링 청산",
            ExitReason.INDICATOR_REVERSAL: "지표역전 청산",
            ExitReason.EXPIRY: "만기정산",
            ExitReason.EMERGENCY: "비상청산",
            ExitReason.MANUAL: "수동청산",
        }.get(reason, str(reason))

        pnl_sign = "+" if pos.pnl_usd >= 0 else ""
        streak_info = ""
        if bot.consecutive_wins > 0:
            streak_info = f"\U0001f525 현재 {bot.consecutive_wins}연승"
        elif bot.consecutive_losses > 0:
            streak_info = f"\u2744\ufe0f 현재 {bot.consecutive_losses}연패"

        text = (
            f"{emoji} <b>{label}</b>\n"
            f"시장: {_esc(pos.market_question)}\n"
            f"진입: {pos.entry_price:.4f} -> 청산: {pos.current_price:.4f}\n"
            f"수익: {pnl_sign}${pos.pnl_usd:.2f} ({pnl_sign}{pos.pnl_pct*100:.1f}%)\n"
            f"보유 시간: {_fmt_time(pos.hold_time_sec)}\n"
            f"전략: {pos.strategy} / 유형: {pos.market_type}\n\n"
            f"\U0001f4b0 현재 잔고: ${bot.balance:.2f}\n"
            f"\U0001f4ca 오늘 성적: {bot.daily_wins}승 {bot.daily_losses}패 "
            f"({'+' if bot.daily_pnl >= 0 else ''}${bot.daily_pnl:.2f})\n"
            f"{streak_info}"
        )
        await self.send(text, "P2")

    # ── 복리 상태 알림 ────────────────────────────────────────

    async def notify_bet_change(self, old_pct: float, new_pct: float, reason: str, next_bet: float):
        """베팅 비율 변경 알림."""
        text = (
            f"\u26a1 <b>베팅 비율 변경</b>\n"
            f"{old_pct*100:.0f}% -> {new_pct*100:.0f}% ({reason})\n"
            f"다음 베팅 예상: ${next_bet:.2f}"
        )
        await self.send(text, "P3")

    # ── 비상 중단 알림 ────────────────────────────────────────

    async def notify_emergency(self, reason: str, balance: float, cooldown_until: float):
        """비상 중단 알림."""
        import datetime
        restart_time = datetime.datetime.fromtimestamp(
            cooldown_until, tz=datetime.timezone.utc
        ).strftime("%H:%M UTC")

        text = (
            f"\U0001f6a8 <b>비상 중단 발생</b>\n"
            f"사유: {_esc(reason)}\n"
            f"조치: 포지션 전량 청산 완료\n"
            f"현재 잔고: ${balance:.2f}\n\n"
            f"\u23f3 15분 쿨다운 시작\n"
            f"예상 재시작: {restart_time}"
        )
        await self.send(text, "P1")

    async def notify_restart(self, success: bool, balance: float, bet_pct: float, reason: str = ""):
        """자동 재시작 알림."""
        if success:
            text = (
                f"\u2705 <b>자동 재시작 완료</b>\n"
                f"체크리스트: 전항목 통과\n"
                f"베팅 비율: {bet_pct*100:.0f}% (안전 모드)\n"
                f"잔고: ${balance:.2f}\n"
                f"거래 재개합니다."
            )
        else:
            text = (
                f"\u23f3 <b>재시작 실패</b> - {_esc(reason)}\n"
                f"추가 15분 대기 후 재시도"
            )
        await self.send(text, "P1")

    # ── HWM / 드로다운 알림 ───────────────────────────────────

    async def notify_hwm_update(self, new_hwm: float, old_hwm: float, total_return_pct: float):
        """신규 최고 잔고 달성 알림."""
        text = (
            f"\U0001f3c6 <b>신규 최고 잔고 달성!</b>\n"
            f"HWM: ${new_hwm:.2f} (이전: ${old_hwm:.2f})\n"
            f"총 수익률: +{total_return_pct:.1f}%"
        )
        await self.send(text, "P2")

    async def notify_drawdown_warning(self, hwm: float, balance: float, dd_pct: float):
        """드로다운 경고 알림."""
        text = (
            f"\u26a0\ufe0f <b>드로다운 경고</b>\n"
            f"HWM: ${hwm:.2f} -> 현재: ${balance:.2f}\n"
            f"드로다운: {dd_pct*100:.1f}%\n"
        )
        if dd_pct <= -0.15:
            text += "가속 제한 발동"
        await self.send(text, "P3")

    # ── 일일 리포트 ───────────────────────────────────────────

    async def send_daily_report(self, bot: BotState, strategy_stats: dict = None):
        """매일 00:00 UTC 일일 리포트."""
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y.%m.%d")

        daily_return = 0.0
        if bot.daily_start_balance > 0:
            daily_return = (bot.balance - bot.daily_start_balance) / bot.daily_start_balance * 100

        total_return = 0.0
        if bot.initial_balance > 0:
            total_return = (bot.balance - bot.initial_balance) / bot.initial_balance * 100

        text = (
            f"\U0001f4cb <b>일일 리포트 - {today}</b>\n\n"
            f"총 거래: {bot.daily_trades}건\n"
            f"승률: {_win_rate(bot.daily_wins, bot.daily_losses)}\n"
            f"순수익: {'+'if bot.daily_pnl>=0 else ''}${bot.daily_pnl:.2f}\n\n"
            f"시작 잔고: ${bot.daily_start_balance:.2f}\n"
            f"종료 잔고: ${bot.balance:.2f}\n"
            f"일일 수익률: {'+'if daily_return>=0 else ''}{daily_return:.2f}%\n"
            f"HWM: ${bot.hwm:.2f}\n\n"
            f"\U0001f504 누적 수익률: {'+'if total_return>=0 else ''}{total_return:.1f}%"
        )

        if strategy_stats:
            text += "\n\n전략별 성적:\n"
            for strat, stats in strategy_stats.items():
                text += (
                    f"  {strat}: {stats['wins']}승 {stats['losses']}패 "
                    f"{'+'if stats['pnl']>=0 else ''}${stats['pnl']:.2f}\n"
                )

        await self.send(text, "P4")

    # ── 주간 리포트 ───────────────────────────────────────────

    async def send_weekly_report(self, bot: BotState, weekly_data: dict = None):
        """매주 월요일 00:00 UTC 주간 리포트."""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        week_num = now.isocalendar()[1]

        total_return = 0.0
        if bot.initial_balance > 0:
            total_return = (bot.balance - bot.initial_balance) / bot.initial_balance * 100

        text = (
            f"\U0001f4ca <b>주간 리포트 - Week {week_num}, {now.year}</b>\n\n"
            f"총 거래: {bot.total_trades}건\n"
            f"HWM: ${bot.hwm:.2f}\n"
            f"최대 드로다운: {bot.max_drawdown_pct*100:.1f}%\n\n"
            f"\U0001f504 총 누적 수익률: {'+'if total_return>=0 else ''}{total_return:.1f}%"
        )
        await self.send(text, "P4")

    # ── 텔레그램 명령어 처리 ──────────────────────────────────

    async def handle_command(self, command: str, bot: BotState) -> str:
        """텔레그램 명령어를 처리합니다."""
        cmd = command.strip().lower()

        if cmd == "/status":
            s = bot.summary()
            return (
                f"잔고: ${s['balance']}\n"
                f"HWM: ${s['hwm']}\n"
                f"베팅 비율: {s['bet_pct']}%\n"
                f"연승: {s['consecutive_wins']} / 연패: {s['consecutive_losses']}\n"
                f"상태: {s['phase']}\n"
                f"DD: {s['drawdown_pct']}%"
            )
        elif cmd == "/today":
            return (
                f"오늘 거래: {bot.daily_trades}건\n"
                f"승률: {_win_rate(bot.daily_wins, bot.daily_losses)}\n"
                f"순수익: {'+'if bot.daily_pnl>=0 else ''}${bot.daily_pnl:.2f}"
            )
        elif cmd == "/pause":
            return "봇 일시정지 요청됨"
        elif cmd == "/resume":
            return "봇 재시작 요청됨"
        elif cmd == "/config":
            return (
                f"베팅 비율: {bot.current_bet_pct*100:.0f}%\n"
                f"상태: {bot.phase.value}\n"
                f"총 거래: {bot.total_trades}"
            )
        else:
            return "알 수 없는 명령어"


def _esc(text: str) -> str:
    """HTML 이스케이프."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}분 {s}초"


def _win_rate(wins: int, losses: int) -> str:
    total = wins + losses
    if total == 0:
        return "N/A"
    return f"{wins/total*100:.0f}% ({wins}승 {losses}패)"
