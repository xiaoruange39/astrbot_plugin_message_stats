from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class GroupMessageSnapshot:
    nickname: str
    group_name: Optional[str] = None
    avatar_url: Optional[str] = None


GROUP_NAME_KEYS = (
    "group_name",
    "group_title",
    "guild_name",
    "channel_name",
    "chat_name",
    "name",
    "title",
)

NICKNAME_KEYS = ("nickname", "nick", "username", "name")
CARD_KEYS = ("card", "member_name", "remark")
AVATAR_URL_KEYS = (
    "avatar_url",
    "avatar",
    "face_url",
    "head_url",
    "headimgurl",
    "icon",
    "icon_url",
    "profile_image_url",
)


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_value(source: Any, key: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _read_first_text(source: Any, keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = _clean_text(_read_value(source, key))
        if value:
            return value
    return None


def _iter_nested_sources(source: Any, keys: Iterable[str]) -> Iterable[Any]:
    for key in keys:
        nested = _read_value(source, key)
        if nested is not None:
            yield nested
            user = _read_value(nested, "user")
            if user is not None:
                yield user


def _iter_sources(event: Any, *, include_group_containers: bool = False) -> Iterable[Any]:
    if event is None:
        return

    seen = set()
    stack = [event]
    attr_names = (
        "raw_event",
        "message_obj",
        "message_event",
        "platform_event",
        "original_event",
        "event",
        "raw_message",
    )
    if include_group_containers:
        # Some adapters keep conversation metadata on the raw platform
        # message. For example, Discord exposes the channel as
        # ``raw_message.channel`` rather than copying its name to
        # ``AstrBotMessage``.
        attr_names += (
            "channel",
            "chat",
            "conversation",
            "room",
            "thread",
            "guild",
        )

    while stack:
        current = stack.pop(0)
        marker = id(current)
        if current is None or marker in seen:
            continue
        seen.add(marker)
        yield current

        for name in attr_names:
            nested = _read_value(current, name)
            if nested is not None:
                stack.append(nested)


def _read_event_sender_name(event: Any) -> Optional[str]:
    if event is None or not hasattr(event, "get_sender_name"):
        return None
    try:
        return _clean_text(event.get_sender_name())
    except Exception:
        return None


def extract_group_name_from_event(event: Any) -> Optional[str]:
    for source in _iter_sources(event, include_group_containers=True):
        group_name = _read_first_text(source, GROUP_NAME_KEYS)
        if group_name:
            return group_name
    return None


def extract_group_message_snapshot(event: Any, user_id: str) -> GroupMessageSnapshot:
    framework_sender_name = _read_event_sender_name(event)
    card = None
    base_nickname = None
    group_name = extract_group_name_from_event(event)
    avatar_url = None

    # Keep group containers out of this traversal: channel/guild objects can
    # also expose a generic ``name`` field, which must not become a nickname.
    for source in _iter_sources(event):
        person_sources = [source]
        person_sources.extend(_iter_nested_sources(source, (
            "sender",
            "author",
            "member",
            "user",
            "from_user",
            "operator",
        )))

        for person in person_sources:
            if card is None:
                card = _read_first_text(person, CARD_KEYS)
            if base_nickname is None:
                base_nickname = _read_first_text(person, NICKNAME_KEYS)
            if avatar_url is None:
                avatar_url = _read_first_text(person, AVATAR_URL_KEYS)

        if card and base_nickname and avatar_url:
            break

    nickname = framework_sender_name or card or base_nickname or f"用户{user_id}"
    return GroupMessageSnapshot(
        nickname=nickname,
        group_name=group_name,
        avatar_url=avatar_url,
    )
