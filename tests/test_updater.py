import logging
from dataclasses import replace
from datetime import date, time
from zoneinfo import ZoneInfo

from app.background.updater import ScheduleUpdater
from app.schedule.models import Lesson, Schedule
from app.schedule.repository import ScheduleRepository
from app.sources.yandex_disk import ScheduleFile
from app.storage.database import Database


class Source:
    content = b"workbook"

    async def current_and_next(self, today):
        return (ScheduleFile("f.xlsx", 1, date(2026, 9, 1), date(2026, 9, 1), "u"),)

    async def download(self, item):
        return self.content


class Parser:
    def parse(self, content):
        return Schedule({1: ("G",)}, ())


class Repo:
    def __init__(self, old=None):
        self.old = old
        self.hashes = set()
        self.saves = 0

    async def has_hash(self, value):
        return value in self.hashes

    async def latest(self):
        return self.old

    async def save(self, filename, week, modified, content_hash, schedule):
        self.hashes.add(content_hash)
        self.old = schedule
        self.saves += 1


class Schedules:
    def __init__(self):
        self.value = None

    def replace(self, value):
        self.value = value


class Notifications:
    def __init__(self):
        self.calls = []

    async def notify_groups(self, groups):
        self.calls.append(groups)

    async def notify_new_week(self, week, start):
        self.calls.append((week, start))


async def test_baseline_has_no_notifications_and_duplicate_is_idempotent():
    repo, notifications, schedules = Repo(), Notifications(), Schedules()
    updater = ScheduleUpdater(
        Source(), Parser(), repo, schedules, notifications, ZoneInfo("Asia/Yekaterinburg")
    )
    assert await updater.check() is True
    assert notifications.calls == [] and repo.saves == 1
    assert await updater.check() is False
    assert notifications.calls == [] and repo.saves == 1


async def test_check_logs_processed_and_unchanged_results(caplog):
    repo, notifications, schedules = Repo(), Notifications(), Schedules()
    updater = ScheduleUpdater(
        Source(), Parser(), repo, schedules, notifications, ZoneInfo("Asia/Yekaterinburg")
    )
    with caplog.at_level(logging.INFO, logger="app.background.updater"):
        await updater.check()
        await updater.check()
    assert "Schedule update processed" in caplog.text
    assert "changed_groups=0" in caplog.text
    assert "Schedule check complete: unchanged" in caplog.text


async def test_next_content_is_compared_and_notified():
    old = Schedule({1: ("G",)}, ())
    repo, notifications, schedules = Repo(old), Notifications(), Schedules()
    source = Source()
    updater = ScheduleUpdater(
        source, Parser(), repo, schedules, notifications, ZoneInfo("Asia/Yekaterinburg")
    )
    assert await updater.check() is True
    assert notifications.calls == [(1, date(2026, 9, 1))]


async def test_changed_group_is_notified_without_field_level_diff():
    old_lesson = Lesson("G", date(2026, 9, 1), 1, time(8, 10), time(9, 30), "S")
    new_lesson = Lesson("G", date(2026, 9, 1), 3, time(11, 50), time(13, 10), "S")
    repo = Repo(Schedule({1: ("G",)}, (old_lesson,)))
    notifications, schedules = Notifications(), Schedules()

    class ChangedParser:
        def parse(self, content):
            return Schedule({1: ("G",)}, (new_lesson,))

    updater = ScheduleUpdater(
        Source(), ChangedParser(), repo, schedules, notifications, ZoneInfo("Asia/Yekaterinburg")
    )

    assert await updater.check() is True
    assert notifications.calls == [("G",)]


async def test_new_week_gets_dedicated_notification_without_generic_duplicate():
    first = Lesson("G", date(2026, 9, 7), 1, time(8), time(9), "Old")
    second = Lesson("G", date(2026, 9, 14), 1, time(8), time(9), "New")
    repo = Repo(Schedule({1: ("G",)}, (first,)))
    notifications, schedules = Notifications(), Schedules()

    class TwoWeekSource:
        async def current_and_next(self, today):
            return (
                ScheduleFile("w2.xlsx", 2, date(2026, 9, 7), date(2026, 9, 7), "u2"),
                ScheduleFile("w3.xlsx", 3, date(2026, 9, 14), date(2026, 9, 10), "u3"),
            )

        async def download(self, item):
            return item.name.encode()

    class TwoWeekParser:
        def parse(self, content):
            lesson = first if content == b"w2.xlsx" else second
            return Schedule({1: ("G",)}, (lesson,))

    updater = ScheduleUpdater(
        TwoWeekSource(),
        TwoWeekParser(),
        repo,
        schedules,
        notifications,
        ZoneInfo("Asia/Yekaterinburg"),
    )
    assert await updater.check() is True
    assert notifications.calls == [(3, date(2026, 9, 14))]
    assert await updater.check() is False
    assert notifications.calls == [(3, date(2026, 9, 14))]


async def test_thursday_publication_is_not_reannounced_after_restart_or_revision(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    await db.create_schema()
    current = Lesson("G", date(2026, 9, 21), 1, time(8), time(9), "Current")
    following = replace(current, date=date(2026, 9, 28), subject="Next")
    notifications = Notifications()

    class ThursdaySource:
        published = False
        revised = False

        async def current_and_next(self, today):
            files = [ScheduleFile("w4.xlsx", 4, current.date, current.date, "u4")]
            if self.published:
                files.append(ScheduleFile("w5.xlsx", 5, following.date, date(2026, 9, 24), "u5"))
            return tuple(files)

        async def download(self, item):
            return item.name.encode() + (b" revised" if self.revised else b"")

    class WeekParser:
        def parse(self, content):
            lesson = current if content.startswith(b"w4") else following
            if content == b"w5.xlsx revised":
                lesson = replace(lesson, location="101[1]")
            return Schedule({1: ("G",)}, (lesson,))

    source = ThursdaySource()

    def updater():
        return ScheduleUpdater(
            source,
            WeekParser(),
            ScheduleRepository(db.sessions),
            Schedules(),
            notifications,
            ZoneInfo("Asia/Yekaterinburg"),
        )

    try:
        assert await updater().check() is True
        assert notifications.calls == []
        source.published = True
        assert await updater().check() is True
        assert notifications.calls == [(5, following.date)]
        assert await updater().check() is False
        assert notifications.calls == [(5, following.date)]
        source.revised = True
        assert await updater().check() is True
        # A later correction is an ordinary group update, not a publication broadcast.
        assert notifications.calls == [(5, following.date), ("G",)]
    finally:
        await db.close()
