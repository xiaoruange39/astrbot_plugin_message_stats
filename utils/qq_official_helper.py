"""QQ 官方 Bot 平台辅助模块

参考 astrbot_plugin_meme_api_python 的实现，为 QQ 官方机器人
(qq_official / qq_official_webhook) 提供头像与昵称获取能力。

官方机器人与 OneBot(QQ 号) 的差异：
- 用户标识是 32 位 openid，而非数字 QQ 号，无法用 q*.qlogo.cn/g?nk= 拼接头像；
  头像需通过 https://q.qlogo.cn/qqapp/{appid}/{openid}/0 获取（依赖 bot 的 appid）。
- 没有 get_group_member_info / get_group_member_list 之类的接口可按 openid
  反查昵称；成员昵称只在其发言时携带于 d.author.username。因此这里在成员发言
  时缓存 openid -> 昵称，供后续引用复用。
- 群消息负载只带 group_openid，不带群名，事件对象里翻不到；群名要反查官方
  OpenAPI /v2/groups/{group_openid}/info（参考 astrbot_plugin_qqadmin_official）。
"""

import asyncio
import time
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .group_id_utils import is_official_qq_openid

QQ_OFFICIAL_AVATAR_URL_TEMPLATE = "https://q.qlogo.cn/qqapp/{appid}/{user_id}/0"
OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}

# openid -> 昵称 缓存（成员发言时填充，供 @ 引用等场景复用）
_OFFICIAL_NICK_CACHE: dict = {}
_OFFICIAL_NICK_CACHE_MAX = 2000

# 群名称查询：走 botpy 已鉴权的 HTTP 客户端直接请求群管理接口。
QQ_OFFICIAL_GROUP_INFO_PATH = "/v2/groups/{group_openid}/info"
_GROUP_NAME_TIMEOUT = 5  # 秒；在消息处理路径上，且 webhook 适配器的 http 自带 300s 超时
_GROUP_NAME_TTL = 3600  # 查到后复用 1 小时，群改名最多延迟这么久
_GROUP_NAME_RETRY_TTL = 300  # 网络/服务端故障后的短退避
_GROUP_NAME_DENIED_TTL = 86400  # 未开通群管理 API 的 bot 会一直被拒，长退避
_GROUP_NAME_CACHE_MAX = 500

# group_openid -> (群名, 过期时间)；群名为空串表示上次查询失败，处于退避期
_OFFICIAL_GROUP_NAME_CACHE: dict = {}
# group_openid -> 锁，避免同群消息并发时打出多个重复请求
_OFFICIAL_GROUP_NAME_LOCKS: dict = {}


def platform_name(event: AstrMessageEvent) -> str:
    """返回事件所属平台的规范化名称（小写）。"""
    get_platform_name = getattr(event, "get_platform_name", None)
    if callable(get_platform_name):
        try:
            name = str(get_platform_name() or "").strip().lower()
            if name:
                return name
        except Exception:
            pass
    meta = getattr(event, "platform_meta", None)
    return str(getattr(meta, "name", "") or "").strip().lower()


def is_official_platform(event: AstrMessageEvent) -> bool:
    """判断事件是否来自 QQ 官方机器人平台。"""
    return platform_name(event) in OFFICIAL_PLATFORMS


def platform_client(event: AstrMessageEvent) -> object:
    """返回事件底层的平台 client/bot 实例。"""
    return getattr(event, "client", None) or getattr(event, "bot", None)


def official_bot_appid(event: AstrMessageEvent) -> str:
    """解析构建官方头像所需的 appid。"""
    client = platform_client(event)
    platform = getattr(client, "platform", None)
    for source in (platform, client, getattr(event, "platform_meta", None)):
        appid = getattr(source, "appid", None)
        if appid:
            return str(appid).strip()
    return ""


def official_avatar_url(event: AstrMessageEvent, user_id: str) -> str:
    """为官方机器人成员 openid 构建头像 URL；appid 不可用时返回空串。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return ""
    appid = official_bot_appid(event)
    if not appid:
        logger.debug("QQ 官方 Bot appid 不可用，无法获取头像")
        return ""
    return QQ_OFFICIAL_AVATAR_URL_TEMPLATE.format(
        appid=quote(appid, safe=""), user_id=quote(user_id, safe="")
    )


def _raw_event_dict(event: AstrMessageEvent) -> dict:
    """定位事件中的原始平台负载字典。"""
    message_obj = getattr(event, "message_obj", None)
    for value in (
        getattr(message_obj, "raw_message", None),
        getattr(message_obj, "raw_event", None),
        getattr(event, "raw_message", None),
        getattr(event, "raw_event", None),
    ):
        if isinstance(value, dict):
            return value
    return {}


def _official_author_payload(event: AstrMessageEvent) -> object:
    """定位事件上的 QQ 官方消息 author 对象/字典。"""
    message_obj = getattr(event, "message_obj", None)
    for raw in (
        getattr(message_obj, "raw_message", None),
        getattr(message_obj, "raw_event", None),
        getattr(event, "raw_message", None),
    ):
        if raw is None:
            continue
        author = getattr(raw, "author", None)
        if author is not None:
            return author
        if isinstance(raw, dict):
            data = raw.get("d") if isinstance(raw.get("d"), dict) else raw
            if isinstance(data, dict) and isinstance(data.get("author"), dict):
                return data["author"]
    return None


def official_author_fields(event: AstrMessageEvent) -> tuple:
    """提取当前官方消息作者的 (openid, username)；两值均可能为空。"""
    author = _official_author_payload(event)
    if author is None:
        return "", ""
    if isinstance(author, dict):
        user_id = str(
            author.get("member_openid")
            or author.get("user_openid")
            or author.get("id")
            or ""
        ).strip()
        name = str(author.get("username") or "").strip()
        return user_id, name
    user_id = str(
        getattr(author, "member_openid", "")
        or getattr(author, "user_openid", "")
        or getattr(author, "id", "")
        or ""
    ).strip()
    name = str(getattr(author, "username", "") or "").strip()
    return user_id, name


def _official_mention_payload(event: AstrMessageEvent) -> list:
    """定位官方消息 mentions 列表（频道/群 @ 负载携带 openid + username）。"""
    message_obj = getattr(event, "message_obj", None)
    for raw in (
        getattr(message_obj, "raw_message", None),
        getattr(message_obj, "raw_event", None),
        getattr(event, "raw_message", None),
    ):
        if raw is None:
            continue
        mentions = getattr(raw, "mentions", None)
        if isinstance(mentions, list) and mentions:
            return mentions
        if isinstance(raw, dict):
            data = raw.get("d") if isinstance(raw.get("d"), dict) else raw
            if isinstance(data, dict) and isinstance(data.get("mentions"), list):
                return data["mentions"]
    return []


def official_mention_fields(event: AstrMessageEvent) -> list:
    """从官方 mentions 负载提取 (openid, username) 列表；可能为空。"""
    results = []
    for entry in _official_mention_payload(event):
        if isinstance(entry, dict):
            user_id = str(
                entry.get("member_openid")
                or entry.get("user_openid")
                or entry.get("openid")
                or entry.get("id")
                or ""
            ).strip()
            name = str(entry.get("username") or entry.get("nick") or "").strip()
        else:
            user_id = str(
                getattr(entry, "member_openid", "")
                or getattr(entry, "user_openid", "")
                or getattr(entry, "openid", "")
                or getattr(entry, "id", "")
                or ""
            ).strip()
            name = str(
                getattr(entry, "username", "") or getattr(entry, "nick", "") or ""
            ).strip()
        if user_id or name:
            results.append((user_id, name))
    return results


def _remember_official_nick(user_id: str, name: str) -> None:
    """缓存官方成员 openid -> 昵称（FIFO 淘汰，限制容量）。"""
    user_id = str(user_id or "").strip()
    name = str(name or "").strip()
    if not user_id or not name or user_id == name:
        return
    if user_id in _OFFICIAL_NICK_CACHE:
        _OFFICIAL_NICK_CACHE.pop(user_id, None)
    elif len(_OFFICIAL_NICK_CACHE) >= _OFFICIAL_NICK_CACHE_MAX:
        _OFFICIAL_NICK_CACHE.pop(next(iter(_OFFICIAL_NICK_CACHE)), None)
    _OFFICIAL_NICK_CACHE[user_id] = name


def official_cached_nick(user_id: str) -> str:
    """返回已缓存的官方成员昵称，未命中返回空串。"""
    return _OFFICIAL_NICK_CACHE.get(str(user_id or "").strip(), "")


def cache_official_author_nick(event: AstrMessageEvent) -> None:
    """缓存当前官方消息作者及被 @ 成员的 openid -> 昵称。

    官方机器人只在成员发言时于 d.author.username 暴露昵称，且无接口按
    openid 反查昵称。发言/被 @ 时缓存，可供后续引用复用。
    """
    if not is_official_platform(event):
        return
    author_id, author_name = official_author_fields(event)
    if author_id and author_name:
        _remember_official_nick(author_id, author_name)
    for mention_id, mention_name in official_mention_fields(event):
        if mention_id and mention_name:
            _remember_official_nick(mention_id, mention_name)


def resolve_official_nickname(event: AstrMessageEvent, user_id: str) -> str:
    """尽力解析官方成员昵称：当前作者 > 被 @ 成员 > 缓存。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return ""
    author_id, author_name = official_author_fields(event)
    if author_id == user_id and author_name:
        _remember_official_nick(user_id, author_name)
        return author_name
    for mention_id, mention_name in official_mention_fields(event):
        if mention_id == user_id and mention_name:
            _remember_official_nick(user_id, mention_name)
            return mention_name
    return official_cached_nick(user_id)


def _botpy_http(client: object) -> object:
    """返回 botpy 已鉴权的 HTTP 客户端；未登录完成时返回 None。

    只在调用时读 client.api：webhook 适配器登录后会整体替换 client.api /
    client.http，提前缓存会拿到未鉴权的旧实例。
    """
    api = getattr(client, "api", None)
    return getattr(api, "_http", None)


def official_clients_from_context(context: object) -> list:
    """从平台管理器取所有官方 bot 的 botpy client（无事件对象的场景用）。"""
    clients = []
    platform_manager = getattr(context, "platform_manager", None)
    get_insts = getattr(platform_manager, "get_insts", None)
    if not callable(get_insts):
        return clients

    try:
        platforms = get_insts() or []
    except Exception as e:
        logger.debug(f"读取平台实例列表失败: {e}")
        return clients

    for platform in platforms:
        get_client = getattr(platform, "get_client", None)
        if not callable(get_client):
            continue
        try:
            name = str(getattr(platform.meta(), "name", "") or "").strip().lower()
            if name not in OFFICIAL_PLATFORMS:
                continue
            client = get_client()
        except Exception:
            continue
        if client is not None:
            clients.append(client)
    return clients


def _cached_official_group_name(group_id: str):
    """返回缓存中的群名（空串代表退避期内的失败）；未缓存或已过期返回 None。"""
    entry = _OFFICIAL_GROUP_NAME_CACHE.get(group_id)
    if not entry:
        return None
    group_name, expire_at = entry
    if expire_at > time.time():
        return group_name
    _OFFICIAL_GROUP_NAME_CACHE.pop(group_id, None)
    return None


def _remember_official_group_name(group_id: str, group_name: str, ttl: int) -> None:
    """写入群名缓存（FIFO 淘汰，限制容量）。"""
    if (
        group_id not in _OFFICIAL_GROUP_NAME_CACHE
        and len(_OFFICIAL_GROUP_NAME_CACHE) >= _GROUP_NAME_CACHE_MAX
    ):
        _OFFICIAL_GROUP_NAME_CACHE.pop(next(iter(_OFFICIAL_GROUP_NAME_CACHE)), None)
    _OFFICIAL_GROUP_NAME_CACHE[group_id] = (group_name, time.time() + ttl)


def _official_group_name_lock(group_id: str) -> asyncio.Lock:
    """返回该群的查询锁，顺带清掉空闲的旧锁。"""
    lock = _OFFICIAL_GROUP_NAME_LOCKS.get(group_id)
    if lock is not None:
        return lock

    if len(_OFFICIAL_GROUP_NAME_LOCKS) >= _GROUP_NAME_CACHE_MAX:
        for cached_id, cached_lock in list(_OFFICIAL_GROUP_NAME_LOCKS.items()):
            if not cached_lock.locked():
                _OFFICIAL_GROUP_NAME_LOCKS.pop(cached_id, None)
    return _OFFICIAL_GROUP_NAME_LOCKS.setdefault(group_id, asyncio.Lock())


def _group_api_denied_errors() -> tuple:
    """代表“群管理 API 用不了”的异常类型，命中后长退避。

    延迟导入 botpy：非 QQ 官方部署可能没装，缺失本身也属于用不了。
    """
    try:
        from botpy import errors
    except ImportError:
        return (ImportError,)
    return (
        ImportError,
        errors.AuthenticationFailedError,
        errors.ForbiddenError,
        errors.NotFoundError,
    )


async def _request_official_group_info(client: object, group_openid: str):
    """请求 /v2/groups/{group_openid}/info；拿不到 HTTP 客户端时返回 None。"""
    from botpy.http import Route

    http = _botpy_http(client)
    if http is None:
        return None
    route = Route("GET", QQ_OFFICIAL_GROUP_INFO_PATH, group_openid=group_openid)
    return await asyncio.wait_for(http.request(route), timeout=_GROUP_NAME_TIMEOUT)


async def _resolve_official_group_name(group_id: str, clients: list) -> str:
    """逐个 client 查询群名并写入缓存；查不到返回空串。"""
    denied_errors = _group_api_denied_errors()
    denied_only = True

    for client in clients:
        try:
            info = await _request_official_group_info(client, group_id)
        except Exception as e:
            if isinstance(e, denied_errors):
                logger.debug(f"群 {group_id} 群名查询被拒绝（群管理 API 未开通？）: {e}")
            else:
                denied_only = False
                logger.debug(f"群 {group_id} 群名查询失败: {e}")
            continue

        group_name = ""
        if isinstance(info, dict):
            group_name = str(info.get("group_name") or "").strip()
        if group_name:
            _remember_official_group_name(group_id, group_name, _GROUP_NAME_TTL)
            return group_name

        # 请求通了但没群名（botpy 超时会返回 None），按可重试处理
        denied_only = False

    ttl = _GROUP_NAME_DENIED_TTL if denied_only else _GROUP_NAME_RETRY_TTL
    _remember_official_group_name(group_id, "", ttl)
    return ""


async def fetch_official_group_name(
    group_id: str,
    event: AstrMessageEvent = None,
    context: object = None,
) -> str:
    """查询官方机器人所在群的群名；不适用或查不到时返回空串。

    官方群消息负载不带群名，只能反查 /v2/groups/{group_openid}/info。该接口属于
    QQ 开放平台的群管理能力，未开通的 bot 会被拒，因此成功与失败都进缓存，
    避免每条消息都打一次请求。

    Args:
        group_id: 群 openid（即官方群消息的 group_openid）
        event: 消息事件对象，可为 None（如定时推送场景）
        context: 插件 Context，用于在没有事件对象时找官方 bot 客户端

    Returns:
        群名称，取不到时为空串
    """
    group_id = str(group_id or "").strip()
    # 频道消息的 group_id 是纯数字 channel_id，不适用群接口
    if not is_official_qq_openid(group_id):
        return ""
    # 有事件对象时以事件所属平台为准，别拿别的平台的群 ID 去查官方接口
    if event is not None and not is_official_platform(event):
        return ""

    cached = _cached_official_group_name(group_id)
    if cached is not None:
        return cached

    clients = []
    if event is not None:
        client = platform_client(event)
        if client is not None:
            clients.append(client)
    if context is not None:
        for client in official_clients_from_context(context):
            if client not in clients:
                clients.append(client)
    if not clients:
        return ""

    async with _official_group_name_lock(group_id):
        # 等锁期间可能已被同群的其他消息填好
        cached = _cached_official_group_name(group_id)
        if cached is not None:
            return cached
        return await _resolve_official_group_name(group_id, clients)
