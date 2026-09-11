from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.notifications.service import NotificationService
from app.schedule.models import merge_schedules
from app.schedule.parser import PARSER_VERSION, ExcelScheduleParser
from app.schedule.repository import ScheduleRepository
from app.schedule.service import ScheduleService
from app.sources.yandex_disk import YandexScheduleSource

log = logging.getLogger(__name__)


def _lesson_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


class ScheduleUpdater:
    def __init__(
        self,
        source: YandexScheduleSource,
        parser: ExcelScheduleParser,
        repository: ScheduleRepository,
        schedules: ScheduleService,
        notifications: NotificationService,
        timezone: ZoneInfo,
    ) -> None:
        self.source, self.parser, self.repository = source, parser, repository
        self.schedules, self.notifications, self.timezone = schedules, notifications, timezone
        self._lock = asyncio.Lock()

    async def check(self) -> bool:
        async with self._lock:
            today = datetime.now(self.timezone).date()
            items = await self.source.current_and_next(today)
            contents = [await self.source.download(item) for item in items]
            digest = hashlib.sha256()
            digest.update(f"parser:{PARSER_VERSION}\0".encode())
            for item, content in zip(items, contents, strict=True):
                digest.update(item.name.encode())
                digest.update(b"\0")
                digest.update(content)
                digest.update(b"\0")
            content_hash = digest.hexdigest()
            if await self.repository.has_hash(content_hash):
                log.info(
                    "Schedule check complete: unchanged; files=%s; hash=%s",
                    " | ".join(item.name for item in items),
                    content_hash[:12],
                )
                return False
            old = await self.repository.latest()
            parsed = tuple(self.parser.parse(content) for content in contents)
            new = merge_schedules(parsed)
            current = items[0]
            await self.repository.save(
                " | ".join(item.name for item in items),
                current.week_number,
                max(item.modified_date for item in items),
                content_hash,
                new,
            )
            self.schedules.replace(new)
            changed_groups: tuple[str, ...] = ()
            new_week_starts: tuple[date, ...] = ()
            if old is not None:
                old_weeks = {_lesson_week(lesson.date) for lesson in old.lessons}
                new_weeks = {_lesson_week(lesson.date) for lesson in new.lessons}
                common_weeks = old_weeks & new_weeks
                new_week_starts = tuple(
                    sorted(
                        item.start_date
                        for item in items
                        if not any(
                            item.start_date <= lesson.date <= item.start_date + timedelta(days=6)
                            for lesson in old.lessons
                        )
                    )
                )
                groups = {lesson.group for lesson in old.lessons + new.lessons}
                changed_groups = tuple(
                    sorted(
                        group
                        for group in groups
                        if {
                            lesson
                            for lesson in old.for_group(group)
                            if _lesson_week(lesson.date) in common_weeks
                        }
                        != {
                            lesson
                            for lesson in new.for_group(group)
                            if _lesson_week(lesson.date) in common_weeks
                        }
                    )
                )
                if changed_groups:
                    await self.notifications.notify_groups(changed_groups)
                for item, schedule in zip(items, parsed, strict=True):
                    if item.start_date not in new_week_starts:
                        continue
                    week_groups = tuple(
                        sorted({group for groups in schedule.courses.values() for group in groups})
                    )
                    await self.notifications.notify_new_week(
                        item.week_number, item.start_date, week_groups
                    )
            log.info(
                "Schedule update processed: files=%s; hash=%s; lessons=%s; "
                "changed_groups=%s; new_weeks=%s",
                " | ".join(item.name for item in items),
                content_hash[:12],
                len(new.lessons),
                len(changed_groups),
                len(new_week_starts),
            )
            return True

    async def run(self, interval: int) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("Schedule update failed")
            await asyncio.sleep(interval)
