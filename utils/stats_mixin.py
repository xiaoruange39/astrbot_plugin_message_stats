"""消息记录、群信息缓存、昵称解析和里程碑能力。"""

import re
from typing import Any, Dict, List, Optional, Set

import orjson

from astrbot.api.event import AstrMessageEvent

from .event_snapshot import extract_group_name_from_event
from .exception_handlers import (
    ExceptionConfig,
    data_operation_handler,
    exception_handler,
)
from .models import GroupInfo, UserData
from .platform_helper import PlatformHelper
from .qq_official_helper import fetch_official_group_name
from .validators import Validators
from .group_id_utils import extract_numeric_group_id, is_placeholder_group_name, normalize_group_id


class StatsMixin:
    """消息记录、群信息缓存、昵称解析和里程碑能力。"""

    def _init_blocked_sets(self):
        """初始化屏蔽用户/群聊的 set 缓存"""
        if self.plugin_config:
            self._blocked_user_set: Set[str] = set(str(uid) for uid in getattr(self.plugin_config, 'blocked_users', []))
            self._blocked_group_set: Set[str] = set(str(gid) for gid in getattr(self.plugin_config, 'blocked_groups', []))
        else:
            self._blocked_user_set: Set[str] = set()
            self._blocked_group_set: Set[str] = set()

    def _load_unified_msg_origins(self):
        """从文件加载持久化的 unified_msg_origin 映射表"""
        try:
            if self._umo_file.exists():
                with open(str(self._umo_file), 'r', encoding='utf-8') as f:
                    data = orjson.loads(f.read())
                if isinstance(data, dict):
                    self.group_unified_msg_origins = data
                    self.logger.info(f"已加载 unified_msg_origin 映射表: {len(data)} 条记录")
        except Exception as e:
            self.logger.debug(f"加载 unified_msg_origin 文件失败: {e}")

    def _save_unified_msg_origins(self):
        """将 unified_msg_origin 映射表保存到文件"""
        try:
            self._umo_file.parent.mkdir(parents=True, exist_ok=True)
            with open(str(self._umo_file), 'w', encoding='utf-8') as f:
                f.write(orjson.dumps(self.group_unified_msg_origins).decode('utf-8'))
        except Exception as e:
            self.logger.debug(f"保存 unified_msg_origin 文件失败: {e}")

    def _load_group_names(self):
        """从文件加载持久化的群组名称缓存"""
        try:
            if self._group_names_file.exists():
                with open(str(self._group_names_file), 'r', encoding='utf-8') as f:
                    data = orjson.loads(f.read())
                if isinstance(data, dict):
                    self._web_group_name_cache = {
                        str(group_id): str(group_name).strip()
                        for group_id, group_name in data.items()
                        if not is_placeholder_group_name(group_name, group_id)
                    }
                    self.logger.info(f"已加载群组名称缓存: {len(data)} 条记录")
        except Exception as e:
            self.logger.debug(f"加载群组名称文件失败: {e}")

    def _save_group_names(self):
        """将群组名称持久化到文件"""
        try:
            self._group_names_file.parent.mkdir(parents=True, exist_ok=True)
            with open(str(self._group_names_file), 'w', encoding='utf-8') as f:
                f.write(orjson.dumps(self._web_group_name_cache).decode('utf-8'))
        except Exception as e:
            self.logger.debug(f"保存群组名称文件失败: {e}")

    async def _collect_group_unified_msg_origin(self, event: AstrMessageEvent):
        """收集群组的unified_msg_origin和群组名称

        自动从 unified_msg_origin 中提取群组ID，同时以原始ID和提取的ID作为键存储，
        确保不同平台（QQ正数ID、Telegram负数ID）都能正确匹配。

        Args:
            event: 消息事件对象
        """
        try:
            group_id = event.get_group_id()
            unified_msg_origin = event.unified_msg_origin

            if group_id and unified_msg_origin:
                group_id_str = str(group_id)

                # 检查是否是新的unified_msg_origin
                old_origin = self.group_unified_msg_origins.get(group_id_str)
                self.group_unified_msg_origins[group_id_str] = unified_msg_origin

                # 从 unified_msg_origin 中提取备用ID（部分平台这里不是群号，可能是会话/用户ID）
                extracted_id = None
                try:
                    extracted_id = unified_msg_origin.rsplit(':', 1)[-1]
                    if extracted_id and extracted_id != group_id_str:
                        self.group_unified_msg_origins[extracted_id] = unified_msg_origin
                except (AttributeError, IndexError, ValueError):
                    pass

                # 同时以 unified_msg_origin 本身作为键存储
                # 这样无论 timer_target_groups 中填的是群号还是 unified_msg_origin 都能匹配
                self.group_unified_msg_origins[unified_msg_origin] = unified_msg_origin

                # 标记 UMO 脏标记，由后台批量保存
                self._umo_dirty = True

                if old_origin != unified_msg_origin:
                    self.logger.info(f"已收集群组 {group_id} 的 unified_msg_origin")

                    # 如果定时任务正在运行且需要此群组，更新配置
                    if self.timer_manager:
                        # 记录当前unified_msg_origin状态（安全截断）
                        origin_preview = unified_msg_origin[:20] + "..." if len(unified_msg_origin) > 20 else unified_msg_origin
                        self.logger.info(f"群组 {group_id} 的 unified_msg_origin: {origin_preview}")

                        # 检查目标群组是否匹配（支持多种格式）
                        # timer_target_groups 可能存储的是：
                        #   1. 群组ID（如 -1003715592711 或 1081839722）
                        #   2. unified_msg_origin 字符串（如 Amy:GroupMessage:1081839722）
                        is_target_group = False
                        for target_id in self.plugin_config.timer_target_groups:
                            if group_id_str == target_id or unified_msg_origin == target_id:
                                is_target_group = True
                                break

                        if self.plugin_config.timer_enabled and is_target_group:
                            self.logger.info(f"检测到目标群组 {group_id} 的 unified_msg_origin 已更新，刷新定时推送目标映射...")
                            # 只刷新 unified_msg_origin 映射表，避免在到点窗口重启任务导致当天推送被跳过
                            self.timer_manager.push_service.group_unified_msg_origins = self.group_unified_msg_origins
                            success = await self.timer_manager.update_config(self.plugin_config, self.group_unified_msg_origins)
                            if success:
                                self.logger.info(f"定时推送目标映射刷新成功")
                            else:
                                self.logger.warning(f"定时推送目标映射刷新失败")


        except (AttributeError, KeyError, TypeError) as e:
            self.logger.error(f"收集群组unified_msg_origin失败: {e}")
        except (RuntimeError, OSError, IOError, ImportError, ValueError) as e:
            self.logger.error(f"收集群组unified_msg_origin失败(系统错误): {e}")

    def _remember_group_name(self, group_id: str, group_name: Optional[str]) -> Optional[str]:
        """记录已知群名到 Web 缓存和定时任务缓存。"""
        if not group_name:
            return None

        group_id_str = str(group_id)
        group_name = str(group_name).strip()
        if is_placeholder_group_name(group_name, group_id_str):
            return None

        old_name = self._web_group_name_cache.get(group_id_str)
        changed = old_name != group_name
        if changed:
            self._web_group_name_cache[group_id_str] = group_name
            self._group_names_dirty = True
            self.logger.info(f"已获取群组 {group_id} 的名称: {group_name}")

        if self.timer_manager:
            timer_cache = getattr(self.timer_manager, "_group_name_cache", None)
            timer_name = timer_cache.get(group_id_str) if isinstance(timer_cache, dict) else None
            if changed or (isinstance(timer_cache, dict) and timer_name != group_name):
                self.timer_manager.update_group_name_cache(group_id, group_name)

        return group_name

    async def _cache_group_name(self, event: Optional[AstrMessageEvent], group_id: str, group_name: Optional[str] = None):
        """获取并缓存群组名称（跨平台通用）

        使用 PlatformHelper 统一获取群组名称，支持所有平台。
        同时更新 Web 页面缓存并持久化到文件，供 page_stats 直接读取。

        每次生成发言榜時都重新获取群名，群改名后立即同步。

        Args:
            event: 消息事件对象（可为 None，此时尝试从 context 获取 API 客户端）
            group_id: 群组ID

        Returns:
            解析出的群名称，取不到时返回 None
        """
        try:
            group_id_str = str(group_id)

            if not group_name:
                group_name = extract_group_name_from_event(event)

            if not group_name:
                # QQ 官方 Bot：群消息负载不带群名，只能反查官方 OpenAPI（内部带缓存）
                group_name = await fetch_official_group_name(
                    group_id_str, event=event, context=self.context
                )

            return self._remember_group_name(group_id_str, group_name)

        except (AttributeError, KeyError, TypeError, RuntimeError) as e:
            self.logger.debug(f"缓存群组名称失败: {e}")
            return None

    async def _collect_group_unified_msg_origins(self):
        """收集所有群组的unified_msg_origin（从缓存中获取）"""
        # 这个方法用于初始化时的批量收集
        # 由于没有event对象，我们先返回空字典
        # 实际的收集将在命令执行时进行
        return self.group_unified_msg_origins.copy()

    def _get_wake_prefixes(self) -> List[str]:
        """Return AstrBot's configured command wake prefixes."""
        try:
            config = self.context.get_config()
            prefixes = config.get("wake_prefix") if config else None
        except Exception:
            prefixes = None

        if not prefixes:
            return ["/"]

        if isinstance(prefixes, str):
            prefixes = [prefixes]
        else:
            try:
                prefixes = list(prefixes)
            except TypeError:
                prefixes = [prefixes]

        normalized = [
            str(prefix).strip()
            for prefix in prefixes
            if prefix is not None and str(prefix).strip()
        ]
        return normalized or ["/"]

    def _is_command_message(self, message_str: str) -> bool:
        """Check whether a message starts with the current AstrBot command prefix."""
        text = str(message_str or "").strip()
        return bool(text and any(text.startswith(prefix) for prefix in self._get_wake_prefixes()))

    def _is_command_event(self, event: AstrMessageEvent) -> bool:
        """Check the original event text before AstrBot strips the wake prefix."""
        message_obj = getattr(event, "message_obj", None)
        raw_message_str = getattr(message_obj, "message_str", "")
        return self._is_command_message(raw_message_str)

    def _is_bot_message(self, event: AstrMessageEvent, user_id: str) -> bool:
        """检查是否为机器人消息"""
        try:
            self_id = event.get_self_id()
            return self_id and user_id == str(self_id)
        except (AttributeError, KeyError, TypeError):
            return False
    
    def _is_sticker_message(self, event: AstrMessageEvent) -> bool:
        """检测消息是否包含 QQ 表情/贴图（Face/Mface/Image）

        统计范围：系统表情（Face）、市场动画表情（mface）、表情包图片（Image）。
        优先遍历 message_obj.message 组件列表；若组件未识别（如 mface 被解析为
        Unknown），则回退到 raw_message 中匹配 CQ 码。

        Args:
            event: 消息事件对象

        Returns:
            bool: 是否包含表情/贴图组件
        """
        try:
            message_obj = getattr(event, 'message_obj', None)
            if not message_obj:
                return False

            # 1) 遍历消息组件列表（结构化检测，覆盖 Face / Image / Mface）
            message = getattr(message_obj, 'message', None)
            if message:
                for comp in message:
                    comp_type = str(getattr(comp, 'type', '') or '').lower()
                    class_name = type(comp).__name__
                    if comp_type in ('face', 'image', 'mface') or class_name in ('Face', 'Image', 'Mface'):
                        if self.plugin_config.detailed_logging_enabled:
                            self.logger.debug(f"_is_sticker_message: 检测到 {class_name}({comp_type}) 组件")
                        return True

            # 2) 兜底：raw_message 中匹配 CQ 码（覆盖 mface 被解析为 Unknown 的情况）
            raw_message = getattr(message_obj, 'raw_message', None) or getattr(message_obj, 'message_str', None)
            if raw_message:
                raw = str(raw_message).lower()
                if re.search(r'\[cq:(?:face|mface|image)', raw):
                    if self.plugin_config.detailed_logging_enabled:
                        self.logger.debug(f"_is_sticker_message: raw_message 匹配到 face/mface/image CQ 码")
                    return True
            return False
        except Exception:
            return False

    async def _record_message_stats(self, group_id: str, user_id: str, nickname: str, group_name: Optional[str] = None, avatar_url: Optional[str] = None, is_sticker: bool = False):
        """记录消息统计

        内部方法,用于记录群成员的消息统计数据.会自动验证输入参数并更新数据.

        Args:
            group_id (str): 群组ID,必须是5-12位数字字符串
            user_id (str): 用户ID,必须是1-20位数字字符串
            nickname (str): 用户昵称,会进行HTML转义和安全验证

        Raises:
            ValueError: 当参数验证失败时抛出
            TypeError: 当参数类型错误时抛出
            KeyError: 当数据格式错误时抛出

        Returns:
            None: 无返回值,记录结果通过日志输出

        Example:
            >>> await self._record_message_stats("123456789", "987654321", "用户昵称")
            # 将在数据管理器中更新该用户的发言统计
        """
        try:
            # 步骤0: 检查是否为屏蔽用户
            if self._is_blocked_user(user_id):
                if self.plugin_config.detailed_logging_enabled:
                    self.logger.debug(f"用户 {user_id} 在屏蔽列表中，跳过统计")
                return

            # 步骤1: 安全处理昵称，确保不为空
            if not nickname or not nickname.strip():
                nickname = f"用户{user_id}"
                self.logger.warning(f"昵称获取失败，使用默认昵称: {nickname}")

            # 步骤2: 验证输入数据
            validated_data = await self._validate_message_data(group_id, user_id, nickname)
            group_id, user_id, nickname = validated_data

            # 步骤3: 处理消息统计和记录
            await self._process_message_stats(group_id, user_id, nickname, group_name, avatar_url, is_sticker)

        except Exception as e:
            self.logger.error(f"记录消息统计失败({type(e).__name__}): {e}", exc_info=True)

    @data_operation_handler('validate', '消息数据参数')
    async def _validate_message_data(self, group_id: str, user_id: str, nickname: str) -> tuple:
        """验证消息数据参数

        验证输入的群组ID、用户ID和昵称参数，确保数据格式正确。

        Args:
            group_id (str): 群组ID
            user_id (str): 用户ID
            nickname (str): 用户昵称

        Returns:
            tuple: 验证后的 (group_id, user_id, nickname) 元组

        Raises:
            ValueError: 当参数验证失败时抛出
            TypeError: 当参数类型错误时抛出
        """
        # 验证数据
        group_id = Validators.validate_group_id(group_id)
        user_id = Validators.validate_user_id(user_id)
        nickname = Validators.validate_nickname(nickname)

        return group_id, user_id, nickname

    async def _process_message_stats(self, group_id: str, user_id: str, nickname: str, group_name: Optional[str] = None, avatar_url: Optional[str] = None, is_sticker: bool = False):
        """处理消息统计和记录

        执行实际的消息统计更新操作，并记录结果日志。
        智能缓存管理：检查昵称变化，只在必要时更新缓存。
        支持发言里程碑检测：当用户发言达到里程碑次数时自动推送排行榜。

        Args:
            group_id (str): 验证后的群组ID
            user_id (str): 验证后的用户ID
            nickname (str): 验证后的用户昵称
            is_sticker (bool): 是否为表情包/贴图消息
        """
        # 直接使用data_manager更新用户消息，同时获取更新后的总发言数
        if self.plugin_config.detailed_logging_enabled:
            self.logger.debug(f"_process_message_stats: user={nickname}, is_sticker={is_sticker}")
        success, message_count = await self.data_manager.update_user_message(
            group_id,
            user_id,
            nickname,
            group_name=group_name,
            avatar_url=avatar_url,
            is_sticker=is_sticker,
        )

        if success:
            if self.plugin_config.detailed_logging_enabled:
                self.logger.info(f"记录消息统计: {nickname}")

            # 发言里程碑检测（使用update_user_message返回的message_count，无需额外查询）
            await self._check_milestone(group_id, user_id, nickname, message_count)
        else:
            self.logger.error(f"记录消息统计失败: {nickname}")

    @staticmethod
    def _calc_beat_percentage(rank: int, active_members: int) -> float:
        """计算击败的活跃群友百分比。

        排名第 1 视为击败全部其他活跃群友（100%），排名末位视为 0%。
        仅 1 名活跃群友时返回 100%。
        """
        if active_members <= 1:
            return 100.0
        beat = (active_members - rank) / (active_members - 1) * 100
        # 防御性裁剪到 [0, 100]
        return max(0.0, min(100.0, beat))

    async def _check_milestone(self, group_id: str, user_id: str, nickname: str, current_count: int):
        """检测用户发言是否达到里程碑，达到则自动推送个人成就卡片

        性能优化：仅在 milestone_enabled=True 且 current_count 在 milestone_targets 中时才执行后续操作。
        使用缓存（_milestone_set）防止重复创建 set，使用里程碑缓存防止重复推送。
        """
        # 快速短路：里程碑功能未启用或目标列表为空，直接返回
        if not self.plugin_config.milestone_enabled or not self.plugin_config.milestone_targets:
            return

        # 使用缓存的 milestone_set，避免每次创建 O(n) 的 set
        # 同时在 _load_plugin_config 中同步更新
        if current_count not in self._milestone_set:
            return

        # 检查是否已经推送过该里程碑（使用缓存防止重复推送）
        if self.member_cache.is_milestone_cached(group_id, user_id, current_count):
            return  # 已推送过，跳过

        # 标记已推送（先标记再执行，防止并发重复推送）
        self.member_cache.mark_milestone_cached(group_id, user_id, current_count)

        self.logger.info(f"🎉 用户 {nickname} 发言达到 {current_count} 次里程碑，准备推送个人成就卡片")

        try:
            # 获取群组的 unified_msg_origin
            unified_msg_origin = self.group_unified_msg_origins.get(str(group_id))
            if not unified_msg_origin:
                self.logger.warning(f"群组 {group_id} 缺少 unified_msg_origin，无法推送里程碑")
                return

            # 获取群组数据
            group_data = await self.data_manager.get_group_data(group_id)
            if not group_data:
                return

            # 计算用户的群内排名、群总发言数与活跃群友数
            rank = 1
            group_total_messages = 0
            active_members = 0
            target_user_data = None

            for user_data_item in group_data:
                if not isinstance(user_data_item, UserData):
                    continue
                group_total_messages += user_data_item.message_count
                if user_data_item.message_count > 0:
                    active_members += 1
                if user_data_item.message_count > current_count:
                    rank += 1
                if user_data_item.user_id == user_id:
                    target_user_data = user_data_item

            # 计算击败的活跃群友百分比（基于群内排名）
            percentage = self._calc_beat_percentage(rank, active_members)

            # 计算今日发言数
            daily_count = 0
            if target_user_data:
                from datetime import date as date_cls
                today = date_cls.today()
                daily_count = target_user_data.get_message_count_in_period(today, today)

            # 计算活跃天数（_message_dates 中的键数量）
            active_days = 0
            if target_user_data:
                target_user_data._ensure_message_dates()
                active_days = len(target_user_data._message_dates)

            # 获取最后发言日期
            last_date = ""
            if target_user_data and target_user_data.last_date:
                last_date = target_user_data.last_date

            # 创建群组信息
            unified_msg_origin = self.group_unified_msg_origins.get(str(group_id), "")
            group_info = GroupInfo(group_id=str(group_id), unified_msg_origin=unified_msg_origin)
            group_name = await self._get_group_name(None, group_id)
            group_info.group_name = group_name

            if not self.image_generator:
                self.logger.warning("里程碑推送：图片生成器未初始化")
                return

            # 计算表情包数量和占比
            sticker_count = 0
            if target_user_data:
                sticker_count = target_user_data.sticker_count
            sticker_percentage = round(sticker_count / current_count * 100, 1) if current_count > 0 else 0

            # 生成里程碑个人成就卡片（复用记录时存下的头像，兼容 QQ 官方 Bot openid）
            milestone_avatar_url = target_user_data.avatar_url if target_user_data else ""
            image_path = await self.image_generator.generate_milestone_image(
                user_id=user_id,
                nickname=nickname,
                milestone_count=current_count,
                rank=rank,
                daily_count=daily_count,
                active_days=active_days,
                last_date=last_date,
                group_total_messages=group_total_messages,
                percentage=percentage,
                group_info=group_info,
                sticker_count=sticker_count,
                sticker_percentage=sticker_percentage,
                avatar_url=milestone_avatar_url or ""
            )

            if not image_path:
                self.logger.warning("里程碑推送：个人卡片生成失败")
                return

            # 构建消息并推送
            from astrbot.api.event import MessageChain
            message_chain = MessageChain()
            message_chain = message_chain.file_image(image_path)

            try:
                await self.context.send_message(unified_msg_origin, message_chain)
                self.logger.info(f"✅ 里程碑推送成功: {nickname} 发言 {current_count} 次")
            finally:
                self._schedule_file_cleanup(image_path)

        except Exception as e:
            self.logger.error(f"里程碑推送失败: {e}", exc_info=True)

    def _stop_command_event(self, event: AstrMessageEvent):
        """阻止命令消息继续进入默认 LLM 回复流程。"""
        try:
            event.should_call_llm(False)
        except Exception:
            pass

        stop_event = getattr(event, 'stop_event', None)
        if callable(stop_event):
            return stop_event()
        return None

    async def _yield_stop_command_event(self, event: AstrMessageEvent):
        """生成 stop_event 结果，兼容旧版 AstrBot 没有 stop_event 的情况。"""
        stop_result = self._stop_command_event(event)
        if stop_result is not None:
            yield stop_result

    async def _get_user_display_name(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        local_nickname: Optional[str] = None,
    ) -> str:
        """获取用户的群昵称（委托给 MemberCacheManager）"""
        allow_event_sender_fallback = False
        try:
            sender_id = event.get_sender_id() if event is not None else None
            allow_event_sender_fallback = sender_id is not None and str(sender_id) == str(user_id)
        except (AttributeError, KeyError, TypeError):
            allow_event_sender_fallback = False

        return await self.member_cache.get_user_display_name(
            event,
            group_id,
            user_id,
            local_nickname=local_nickname,
            allow_event_sender_fallback=allow_event_sender_fallback,
        )

    @data_operation_handler('extract', '群成员昵称数据')
    def _get_display_name_from_member(self, member: Dict[str, Any]) -> Optional[str]:
        """从群成员信息中提取显示昵称（委托给 MemberCacheManager）"""
        return self.member_cache.get_display_name_from_member(member)

    async def _get_user_nickname_unified(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """统一的用户昵称获取方法（委托给 MemberCacheManager）"""
        return await self.member_cache.get_user_display_name(event, group_id, user_id)

    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    async def _get_from_dict_cache(self, group_id: str, user_id: str) -> Optional[str]:
        """从群成员字典缓存获取昵称（委托给 MemberCacheManager）"""
        return self.member_cache._get_from_dict_cache(group_id, user_id)

    async def _fetch_and_cache_from_api(self, event: AstrMessageEvent, group_id: str, user_id: str) -> Optional[str]:
        """从API获取群成员信息并缓存（委托给 MemberCacheManager）"""
        return await self.member_cache._fetch_and_cache_from_api(event, group_id, user_id)

    async def _get_fallback_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取备用昵称（委托给 MemberCacheManager）"""
        return await self.member_cache.get_fallback_nickname(event, user_id)

    def _is_blocked_user(self, user_id: str) -> bool:
        """检查用户是否在屏蔽列表中（使用 set 缓存，O(1) 查询）"""
        return str(user_id) in self._blocked_user_set

    def _is_blocked_group(self, group_id: str) -> bool:
        """检查群聊是否在屏蔽列表中（使用 set 缓存，O(1) 查询）"""
        group_id_str = normalize_group_id(group_id)
        if group_id_str in self._blocked_group_set:
            return True
        numeric_group_id = extract_numeric_group_id(group_id_str)
        return bool(numeric_group_id and numeric_group_id in self._blocked_group_set)

    async def _get_group_members_cache(self, event: AstrMessageEvent, group_id: str) -> Optional[List[Dict[str, Any]]]:
        """获取群成员缓存（委托给 MemberCacheManager）"""
        return await self.member_cache.get_group_members(event, group_id)

    async def _fetch_group_members_from_api(self, event: AstrMessageEvent, group_id: str) -> Optional[List[Dict[str, Any]]]:
        """从API获取群成员（委托给 MemberCacheManager）"""
        return await self.member_cache._fetch_group_members_from_api(event, group_id)

    async def _get_group_name(self, event: Optional[AstrMessageEvent], group_id: str) -> str:
        """获取群名称（跨平台通用）

        使用 PlatformHelper 统一获取群组名称，支持所有平台。
        当 event 为 None 时（如定时推送场景），跳过事件对象获取，直接使用 API 或默认名称。

        Args:
            event: 消息事件对象（可能为 None，如定时推送场景）
            group_id: 群组ID

        Returns:
            群组名称，如果获取失败则返回 "群{group_id}"
        """
        group_id_str = str(group_id)
        try:
            # 首先尝试通过事件对象获取群组信息（仅在 event 不为 None 时）
            if event is not None:
                group_data = await event.get_group(group_id)
                if group_data:
                    # 简化群名获取逻辑，直接尝试常用属性
                    group_name = getattr(group_data, 'group_name', None) or \
                                 getattr(group_data, 'name', None) or \
                                 getattr(group_data, 'title', None) or \
                                 getattr(group_data, 'group_title', None)
                    remembered_group_name = self._remember_group_name(group_id_str, group_name)
                    if remembered_group_name:
                        return remembered_group_name

            cached_group_name = self._web_group_name_cache.get(group_id_str)
            if cached_group_name:
                return str(cached_group_name).strip()

            # 如果事件对象获取失败或 event 为 None，使用 PlatformHelper 统一通过API获取（跨平台通用）
            helper = PlatformHelper(event, self.context)
            group_name = await helper.get_group_name(group_id)
            if not group_name:
                numeric_group_id = extract_numeric_group_id(group_id_str)
                if numeric_group_id and numeric_group_id != group_id_str:
                    group_name = await helper.get_group_name(numeric_group_id)
            remembered_group_name = self._remember_group_name(group_id_str, group_name)
            if remembered_group_name:
                return remembered_group_name

            # QQ 官方 Bot 没有 OneBot 那套 get_group_info，改走官方 OpenAPI
            group_name = await fetch_official_group_name(
                group_id_str, event=event, context=self.context
            )
            remembered_group_name = self._remember_group_name(group_id_str, group_name)
            if remembered_group_name:
                return remembered_group_name

            return f"群{group_id}"
        except (AttributeError, KeyError, TypeError, OSError) as e:
            self.logger.warning(f"获取群名称失败，使用默认名称: {e}")
            return f"群{group_id}"
