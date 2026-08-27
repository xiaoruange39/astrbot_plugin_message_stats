import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "utils" / "event_snapshot.py"
SPEC = importlib.util.spec_from_file_location("event_snapshot", MODULE_PATH)
event_snapshot = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(event_snapshot)


class EventSnapshotTests(unittest.TestCase):
    def test_extracts_discord_channel_name_from_raw_message(self):
        channel = SimpleNamespace(name="bot-chat")
        raw_message = SimpleNamespace(channel=channel)
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message=raw_message),
        )

        self.assertEqual(
            event_snapshot.extract_group_name_from_event(event),
            "bot-chat",
        )

    def test_channel_name_takes_priority_over_guild_name(self):
        guild = SimpleNamespace(name="AstrBot Server")
        channel = SimpleNamespace(name="bot-chat", guild=guild)
        raw_message = SimpleNamespace(channel=channel, guild=guild)
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message=raw_message),
        )

        self.assertEqual(
            event_snapshot.extract_group_name_from_event(event),
            "bot-chat",
        )

    def test_extracts_name_from_nested_chat_mapping(self):
        raw_message = {"chat": {"title": "Telegram Group"}}
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message=raw_message),
        )

        self.assertEqual(
            event_snapshot.extract_group_name_from_event(event),
            "Telegram Group",
        )

    def test_existing_direct_group_name_takes_priority(self):
        raw_message = {
            "group_name": "QQ Group",
            "channel": {"name": "nested-channel"},
        }
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message=raw_message),
        )

        self.assertEqual(
            event_snapshot.extract_group_name_from_event(event),
            "QQ Group",
        )

    def test_channel_name_does_not_leak_into_sender_nickname(self):
        channel = SimpleNamespace(name="bot-chat")
        raw_message = SimpleNamespace(
            channel=channel,
            author=SimpleNamespace(id="123456"),
        )
        event = SimpleNamespace(
            message_obj=SimpleNamespace(raw_message=raw_message),
        )

        snapshot = event_snapshot.extract_group_message_snapshot(event, "123456")

        self.assertEqual(snapshot.group_name, "bot-chat")
        self.assertEqual(snapshot.nickname, "用户123456")


if __name__ == "__main__":
    unittest.main()
