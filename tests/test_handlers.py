from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import app.bot.handlers as handlers
from app.schedule.models import Lesson, Schedule
from app.schedule.service import ScheduleService


class Users:
    def __init__(self, user=None):
        self.user = user
        self.saved = None
        self.overrides = {}

    async def get(self, user_id):
        return self.user

    async def save(self, user_id, course, group, subgroup=None):
        self.saved = (user_id, course, group, subgroup)

    async def toggle_notifications(self, user_id):
        self.user = SimpleNamespace(notifications_enabled=False)
        return self.user

    async def hidden_subjects(self, user_id):
        return frozenset()

    async def toggle_hidden_subject(self, user_id, subject):
        return True

    async def clear_hidden_subjects(self, user_id):
        return None

    async def subject_subgroups(self, user_id):
        return dict(self.overrides)

    async def set_subject_subgroup(self, user_id, subject, subgroup):
        if subgroup is None:
            self.overrides.pop(subject, None)
        else:
            self.overrides[subject] = subgroup

    async def clear_subject_subgroups(self, user_id):
        self.overrides.clear()

    async def all_users(self):
        return (
            SimpleNamespace(telegram_id=10),
            SimpleNamespace(telegram_id=11),
        )


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, telegram_id, text):
        self.sent.append((telegram_id, text))


class FakeMessage:
    def __init__(self, text=None, user_id=7):
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.answers = []
        self.edits = []
        self.documents = []
        self.deleted = 0

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer_document(self, document, caption=None):
        self.documents.append((document, caption))

    async def delete(self):
        self.deleted += 1


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.from_user = SimpleNamespace(id=7)
        self.bot = FakeBot()
        self.answered = 0

    async def answer(self):
        self.answered += 1


def callbacks(router, observer):
    return {item.callback.__name__: item.callback for item in getattr(router, observer).handlers}


async def test_admin_can_confirm_broadcast(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    router = handlers.build_router(
        Users(),
        ScheduleService(),
        ZoneInfo("Asia/Yekaterinburg"),
        admin_ids=frozenset({7}),
    )
    message_handlers = callbacks(router, "message")
    callback_handlers = callbacks(router, "callback_query")
    message = FakeMessage("/broadcast Важное <сообщение>")

    await message_handlers["broadcast"](message)

    assert "Предпросмотр" in message.answers[0][0]
    assert "&lt;сообщение&gt;" in message.answers[0][0]
    confirmation = FakeCallback("broadcast:confirm")
    await callback_handlers["broadcast_confirm"](confirmation)
    assert confirmation.bot.sent == [
        (10, "Важное &lt;сообщение&gt;"),
        (11, "Важное &lt;сообщение&gt;"),
    ]
    assert "Отправлено: 2" in confirmation.message.edits[-1][0]


async def test_broadcast_is_denied_to_non_admin(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    router = handlers.build_router(Users(), ScheduleService(), ZoneInfo("Asia/Yekaterinburg"))
    message = FakeMessage("/broadcast test")
    await callbacks(router, "message")["broadcast"](message)
    assert message.answers[0][0] == "Команда недоступна."


class Calendars:
    async def subscription_url(self, telegram_id):
        return "https://schedule.example/calendar/private.ics"

    async def export_for_user(self, telegram_id):
        return "РИС-23-3", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    async def regenerate_subscription_url(self, telegram_id):
        return "https://schedule.example/calendar/replaced.ics"


async def test_start_and_settings_flows(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    users = Users()
    service = ScheduleService(Schedule({4: ("РИС-23-3",)}, ()))
    router = handlers.build_router(users, service, ZoneInfo("Asia/Yekaterinburg"))
    message = FakeMessage()
    await callbacks(router, "message")["start"](message)
    assert "уровень образования" in message.answers[0][0]
    users.user = SimpleNamespace(group_name="РИС-23-3", notifications_enabled=True)
    message = FakeMessage()
    await callbacks(router, "message")["settings"](message)
    assert message.deleted == 1
    assert "Группа: РИС-23-3" in message.answers[0][0]


async def test_education_program_course_and_group_selection(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    users = Users()
    router = handlers.build_router(
        users, ScheduleService(Schedule({4: ("РИС-23-3",)}, ())), ZoneInfo("Asia/Yekaterinburg")
    )
    callback_handlers = callbacks(router, "callback_query")

    bachelor = FakeCallback("education:bachelor")
    await callback_handlers["bachelor"](bachelor)
    assert "образовательную программу" in bachelor.message.edits[0][0]

    program = FakeCallback("program:РИС")
    await callback_handlers["program"](program)
    assert "Выбери курс" in program.message.edits[0][0]

    course = FakeCallback("course:РИС:4")
    await callback_handlers["course"](course)
    assert "выбери группу" in course.message.edits[0][0]
    group = FakeCallback("group:4:РИС-23-3")
    await callback_handlers["group"](group)
    assert users.saved == (7, 4, "РИС-23-3", None) and "Готово" in group.message.answers[0][0]


async def test_group_registration_asks_for_subgroup_and_saves_choice(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    monday = datetime.now(ZoneInfo("Asia/Yekaterinburg")).date()
    lessons = (
        Lesson("G-1", monday, 1, time(8), time(9), "A", subgroup=1),
        Lesson("G-1", monday, 1, time(8), time(9), "B", subgroup=2),
    )
    users = Users()
    router = handlers.build_router(
        users, ScheduleService(Schedule({1: ("G-1",)}, lessons)), ZoneInfo("Asia/Yekaterinburg")
    )
    callback_handlers = callbacks(router, "callback_query")
    group = FakeCallback("group:1:G-1")
    await callback_handlers["group"](group)
    assert users.saved is None
    assert [row[0].text for row in group.message.edits[0][1].inline_keyboard] == [
        "Подгруппа 1",
        "Подгруппа 2",
        "Показывать все",
    ]

    subgroup = FakeCallback("subgroup:1:G-1:2")
    await callback_handlers["subgroup"](subgroup)
    assert users.saved == (7, 1, "G-1", 2)
    assert "Подгруппа: 2" in subgroup.message.answers[0][0]


async def test_subject_can_override_default_subgroup(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    monday = datetime.now(ZoneInfo("Asia/Yekaterinburg")).date()
    lessons = (
        Lesson("G-1", monday, 1, time(8), time(9), "Базы данных", subgroup=1),
        Lesson("G-1", monday, 1, time(8), time(9), "Базы данных", subgroup=2),
    )
    user = SimpleNamespace(
        telegram_id=7, course=1, group_name="G-1", subgroup=1, notifications_enabled=True
    )
    users = Users(user)
    router = handlers.build_router(
        users, ScheduleService(Schedule({1: ("G-1",)}, lessons)), ZoneInfo("Asia/Yekaterinburg")
    )
    callback_handlers = callbacks(router, "callback_query")
    menu = FakeCallback("settings:subject_subgroups")
    await callback_handlers["subject_subgroups"](menu)
    subject_button = menu.message.edits[-1][1].inline_keyboard[0][0]
    select = FakeCallback(subject_button.callback_data)
    await callback_handlers["subject_subgroups_select"](select)
    subgroup_button = select.message.edits[-1][1].inline_keyboard[1][0]
    choice = FakeCallback(subgroup_button.callback_data)
    await callback_handlers["subject_subgroups_set"](choice)
    assert users.overrides == {"Базы данных": 2}


async def test_all_subgroups_mode_keeps_every_numbered_lesson(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    today = datetime.now(ZoneInfo("Asia/Yekaterinburg")).date()
    lessons = (
        Lesson("G-1", today, 1, time(8), time(9), "Первая", subgroup=1),
        Lesson("G-1", today, 2, time(9), time(10), "Вторая", subgroup=2),
    )
    user = SimpleNamespace(
        telegram_id=7, course=1, group_name="G-1", subgroup=None, notifications_enabled=True
    )
    router = handlers.build_router(
        Users(user),
        ScheduleService(Schedule({1: ("G-1",)}, lessons)),
        ZoneInfo("Asia/Yekaterinburg"),
    )
    message = FakeMessage()
    await callbacks(router, "message")["today"](message)
    assert "Первая" in message.answers[0][0]
    assert "Вторая" in message.answers[0][0]


async def test_master_program_is_placeholder(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    router = handlers.build_router(
        Users(),
        ScheduleService(Schedule({4: ("РИС-23-3",)}, ())),
        ZoneInfo("Asia/Yekaterinburg"),
    )
    master = FakeCallback("education:master")
    await callbacks(router, "callback_query")["master"](master)
    assert "скоро появится" in master.message.edits[0][0]


async def test_today_tomorrow_week_require_registration(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    users = Users()
    router = handlers.build_router(
        users, ScheduleService(Schedule({1: ("G",)}, ())), ZoneInfo("Asia/Yekaterinburg")
    )
    message_handlers = callbacks(router, "message")
    for name in ("today", "tomorrow", "week"):
        message = FakeMessage()
        await message_handlers[name](message)
        assert "уровень образования" in message.answers[0][0]


async def test_week_menu_shows_current_and_next_week(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    timezone = ZoneInfo("Asia/Yekaterinburg")
    today = datetime.now(timezone).date()
    monday = today - timedelta(days=today.weekday())
    lessons = (
        Lesson("G", monday, 1, time(8), time(9), "Текущая пара"),
        Lesson("G", monday + timedelta(days=7), 1, time(8), time(9), "Следующая пара"),
    )
    users = Users(SimpleNamespace(telegram_id=7, group_name="G", notifications_enabled=True))
    router = handlers.build_router(users, ScheduleService(Schedule({1: ("G",)}, lessons)), timezone)

    message = FakeMessage()
    await callbacks(router, "message")["week"](message)
    markup = message.answers[0][1]
    assert [row[0].text.split(" · ", 1)[0] for row in markup.inline_keyboard] == [
        "Текущая",
        "Следующая",
    ]

    selected = FakeCallback(f"week:{(monday + timedelta(days=7)).isoformat()}")
    await callbacks(router, "callback_query")["selected_week"](selected)
    assert selected.message.deleted == 1
    assert "Следующая пара" in selected.message.answers[0][0]


async def test_calendar_subscription_flow(monkeypatch):
    monkeypatch.setattr(handlers, "Message", FakeMessage)
    users = Users(SimpleNamespace(group_name="РИС-23-3", notifications_enabled=True))
    router = handlers.build_router(
        users,
        ScheduleService(Schedule({4: ("РИС-23-3",)}, ())),
        ZoneInfo("Asia/Yekaterinburg"),
        calendars=Calendars(),
    )
    callback_handlers = callbacks(router, "callback_query")

    menu = FakeCallback("settings:calendar")
    await callback_handlers["calendar"](menu)
    assert "Подписка на расписание" in menu.message.edits[0][0]

    link = FakeCallback("calendar:url")
    await callback_handlers["calendar_url"](link)
    assert "private.ics" in link.message.edits[0][0]

    download = FakeCallback("calendar:download")
    await callback_handlers["calendar_download"](download)
    assert download.message.documents
    assert "разовый снимок" in download.message.documents[0][1]

    help_callback = FakeCallback("calendar:help")
    await callback_handlers["calendar_help"](help_callback)
    assert "Google Calendar" in help_callback.message.edits[0][0]

    rotate = FakeCallback("calendar:rotate")
    await callback_handlers["calendar_rotate"](rotate)
    assert "Старая ссылка" in rotate.message.edits[0][0]

    confirm = FakeCallback("calendar:rotate:confirm")
    await callback_handlers["calendar_rotate_confirm"](confirm)
    assert "replaced.ics" in confirm.message.edits[0][0]
