from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from app.bot.formatters import format_changes, split_messages
from app.schedule.diff import changes_by_group
from app.schedule.models import LessonChange
from app.users.repository import UserRepository

log = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, users: UserRepository, send: Callable[[int, str], Awaitable[None]]) -> None:
        self.users = users
        self.send = send

    async def _send(self, telegram_id: int, message: str) -> None:
        for attempt in range(3):
            try:
                await self.send(telegram_id, message)
                return
            except TelegramRetryAfter as exc:
                if attempt == 2:
                    log.warning("Notification rate limit persists for user %s", telegram_id)
                    return
                await asyncio.sleep(exc.retry_after)
            except TelegramAPIError:
                log.warning("Could not notify user %s", telegram_id, exc_info=True)
                return

    async def notify_groups(self, groups: tuple[str, ...]) -> None:
        for group in groups:
            message = (
                f"<b>🔔 Расписание группы {escape(group)} обновилось</b>\n\n"
                "В расписании есть изменения. Откройте расписание на неделю "
                "и проверьте актуальные пары."
            )
            for user in await self.users.subscribers(group):
                await self._send(user.telegram_id, message)

    async def notify_new_week(self, week_number: int, start_date: date) -> None:
        end_date = start_date + timedelta(days=6)
        message = (
            f"<b>📅 Появилось расписание на новую неделю №{week_number}!</b>\n\n"
            f"{start_date:%d.%m}–{end_date:%d.%m.%Y}\n\n"
            "Нажми «Неделя» и выбери эти даты, чтобы посмотреть свои пары."
        )
        for user in await self.users.all_users():
            await self._send(user.telegram_id, message)

    async def notify(self, changes: tuple[LessonChange, ...]) -> None:
        for group, group_changes in changes_by_group(changes).items():
            recipients = await self.users.subscribers(group)
            for user in recipients:
                hidden = await self.users.hidden_subjects(user.telegram_id)
                visible = tuple(
                    change
                    for change in group_changes
                    if any(
                        lesson is not None and lesson.subject not in hidden
                        for lesson in (change.before, change.after)
                    )
                )
                if not visible:
                    continue
                messages = split_messages(format_changes(visible))
                for message in messages:
                    await self._send(user.telegram_id, message)
