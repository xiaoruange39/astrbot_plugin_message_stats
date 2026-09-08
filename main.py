"""
AstrBot 群发言统计插件
统计群成员发言次数,生成排行榜
"""

# 标准库导入
import asyncio
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

# AstrBot框架导入
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

# 本地模块导入
from .utils.data_manager import DataManager
from .utils.image_generator import ImageGenerator, ImageGenerationError
from .utils.member_cache_manager import MemberCacheManager
from .utils.event_snapshot import extract_group_message_snapshot
from .utils.qq_official_helper import (
    cache_official_author_nick,
    is_official_platform,
    official_avatar_url,
    resolve_official_nickname,
)
from .utils.web_panel_mixin import WebPanelMixin
from .utils.stats_mixin import StatsMixin
from .utils.help_mixin import HelpMixin
from .utils.ranking_mixin import CUSTOM_DATE_RANK_MESSAGE_PATTERN, RankingMixin
from .utils.models import GroupInfo, PluginConfig, RankType, UserData
from .utils.exception_handlers import ExceptionConfig, exception_handler
from .utils.constants import (
    USER_NICKNAME_CACHE_TTL,
    GROUP_MEMBERS_CACHE_TTL as CACHE_TTL_SECONDS,
)


def _install_feature_methods(*feature_classes):
    """Attach split feature methods without adding framework-visible base classes."""

    def decorator(plugin_class):
        for feature_class in feature_classes:
            for name, value in vars(feature_class).items():
                if name.startswith("__"):
                    continue
                if name in plugin_class.__dict__:
                    raise RuntimeError(f"duplicate plugin method: {name}")
                setattr(plugin_class, name, value)
        return plugin_class

    return decorator


@register("astrbot_plugin_message_stats", "xiaoruange39", "群发言统计插件", "2.2.4")
@_install_feature_methods(WebPanelMixin, StatsMixin, RankingMixin, HelpMixin)
class MessageStatsPlugin(Star):
    """群发言统计插件
    
    该插件用于统计群组成员的发言次数,并生成多种类型的排行榜.
    支持自动监听群消息、手动记录、总榜/日榜/周榜/月榜/年榜等功能.
    
    主要功能:
        - 自动监听和记录群成员发言统计
        - 支持多种排行榜类型(总榜、日榜、周榜、月榜、年榜)
        - 提供图片和文字两种显示模式
        - 完整的配置管理系统
        - 权限控制和安全管理
        - 群成员昵称智能获取
        - 高效的缓存机制
        - 支持指令别名，方便用户使用
        
    排行榜指令别名:
        - 总榜: 发言榜 → 水群榜、B话榜、发言排行、排行榜、发言统计
        - 日榜: 今日发言榜 → 今日排行、日榜、今日发言排行、今日排行榜
        - 周榜: 本周发言榜 → 本周排行、周榜、本周发言排行、本周排行榜
        - 月榜: 本月发言榜 → 本月排行、月榜、本月发言排行、本月排行榜
        - 年榜: 本年发言榜 → 本年排行、年榜、本年发言排行、本年排行榜
        
    Attributes:
        data_manager (DataManager): 数据管理器,负责数据的存储和读取
        plugin_config (PluginConfig): 插件配置对象
        image_generator (ImageGenerator): 图片生成器,用于生成排行榜图片
        group_members_cache (TTLCache): 群成员列表缓存,5分钟TTL
        logger: 日志记录器
        initialized (bool): 插件初始化状态
        
    Example:
        >>> plugin = MessageStatsPlugin(context)
        >>> await plugin.initialize()
        >>> # 插件将自动开始监听群消息并记录统计
    """
    
    def __init__(self, context: Context, config: 'AstrBotConfig' = None):
        """初始化插件实例
        
        Args:
            context (Context): AstrBot上下文对象,包含插件运行环境信息
            config (AstrBotConfig): AstrBot配置的插件配置对象,通过Web界面设置
        """
        super().__init__(context)
        
        # 注册 plugin pages API
        context.register_web_api(
            "/astrbot_plugin_message_stats/stats",
            self.page_stats,
            ["GET"],
            "发言统计面板数据",
        )
        context.register_web_api(
            "/astrbot_plugin_message_stats/delete",
            self.page_delete,
            ["POST"],
            "删除群组发言数据",
        )
        context.register_web_api(
            "/astrbot_plugin_message_stats/chart",
            self.page_chart,
            ["GET"],
            "群组发言趋势图表数据",
        )
        context.register_web_api(
            "/astrbot_plugin_message_stats/fonts",
            self.page_fonts,
            ["GET"],
            "字体管理数据",
        )
        context.register_web_api(
            "/astrbot_plugin_message_stats/fonts/upload",
            self.page_font_upload,
            ["POST"],
            "上传字体文件",
        )
        context.register_web_api(
            "/astrbot_plugin_message_stats/fonts/select",
            self.page_font_select,
            ["POST"],
            "选择字体文件",
        )
        context.register_web_api(
            "/astrbot_plugin_message_stats/fonts/delete",
            self.page_font_delete,
            ["POST"],
            "删除字体文件",
        )

        self.logger = logger
        
        # 使用旧版 message_stats 数据目录，避免老用户升级后数据目录变化导致历史统计不可见
        data_dir = Path(StarTools.get_data_dir('message_stats'))
        
        # 初始化组件
        self.data_manager = DataManager(data_dir)
        
        # 使用AstrBot的标准配置系统
        self.config = config
        self.plugin_config = self._convert_to_plugin_config()
        self.image_generator = None
        
        # 群组unified_msg_origin映射表 - 用于主动消息发送
        self.group_unified_msg_origins = {}
        # unified_msg_origin持久化文件（重启后自动恢复）
        self._umo_file = Path(data_dir) / "unified_msg_origins.json"
        self._load_unified_msg_origins()
        
        # 群组名称持久化存储 (group_id -> group_name)
        # 由 _cache_group_name（有event时）写入，Web页面直接读取
        self._group_names_file = Path(data_dir) / "group_names.json"
        self._web_group_name_cache: Dict[str, str] = {}
        self._load_group_names()
        
        # ---------- 脏标记批量保存优化 ----------
        self._umo_dirty = False          # UMO 脏标记
        self._group_names_dirty = False  # 群名脏标记
        self._save_task: Optional[asyncio.Task] = None  # 后台保存任务
        
        # 成员缓存管理器 - 管理群成员列表/字典缓存
        # 使用分层缓存策略（群成员字典缓存 → API获取），
        # 并在API请求外层添加异步锁防止缓存击穿
        self.member_cache = MemberCacheManager(
            context,
            cache_ttl=CACHE_TTL_SECONDS,
            nickname_cache_ttl=USER_NICKNAME_CACHE_TTL
        )
        
        # 定时任务管理器 - 延迟初始化
        self.timer_manager = None
        from quart import jsonify
        self._jsonify = jsonify
        
        # 屏蔽用户/群聊的 set 缓存（避免每次比较都创建新列表）
        # 在 _convert_to_plugin_config() 之后初始化
        self._init_blocked_sets()
        
        # 里程碑目标 set 缓存
        self._milestone_set: Set[int] = set(getattr(self.plugin_config, 'milestone_targets', []))

    def _schedule_file_cleanup(self, file_path: str, delay_seconds: int = 300):
        if file_path:
            asyncio.create_task(self._cleanup_file_later(str(file_path), delay_seconds))

    async def _cleanup_file_later(self, file_path: str, delay_seconds: int):
        try:
            await asyncio.sleep(delay_seconds)
            if os.path.exists(file_path):
                os.unlink(file_path)
        except OSError as e:
            self.logger.warning(f"清理临时图片文件失败: {file_path}, 错误: {e}")

    def _convert_to_plugin_config(self) -> PluginConfig:
        """将AstrBot配置转换为插件配置对象"""
        try:
            # 如果没有配置，使用默认配置
            if not self.config:
                self.logger.info("没有配置，使用默认配置")
                return PluginConfig()
            
            # 确保config是字典类型
            config_dict = dict(self.config) if hasattr(self.config, 'items') else {}
            
            # 兼容处理：将Web面板中的 theme_switch_light_time / theme_switch_dark_time 
            # 合并到 theme_switch_times 字典中
            if 'theme_switch_light_time' in config_dict or 'theme_switch_dark_time' in config_dict:
                theme_times = config_dict.get('theme_switch_times', {})
                if isinstance(theme_times, dict):
                    if 'theme_switch_light_time' in config_dict:
                        theme_times['light'] = config_dict.pop('theme_switch_light_time')
                    if 'theme_switch_dark_time' in config_dict:
                        theme_times['dark'] = config_dict.pop('theme_switch_dark_time')
                    config_dict['theme_switch_times'] = theme_times
            
            # 兼容旧版独立定时配置，迁移为 template_list 保存结构
            if 'timer_tasks' not in config_dict and (
                config_dict.get('timer_enabled') or config_dict.get('timer_target_groups')
            ):
                config_dict['timer_tasks'] = [{
                    "__template_key": "rank_push",
                    "enabled": config_dict.get('timer_enabled', False),
                    "push_time": config_dict.get('timer_push_time', '09:00'),
                    "target_groups": config_dict.get('timer_target_groups', []),
                    "rank_type": config_dict.get('timer_rank_type', 'daily')
                }]

            # 如果 llm_system_prompt 为空，回填默认提示词
            # 用户保存 Web 配置后空值会被持久化，升级时也不会被 schema 默认值覆盖
            if not config_dict.get('llm_system_prompt', ''):
                from .utils.llm_analyzer import DEFAULT_SYSTEM_PROMPT as default_prompt
                config_dict['llm_system_prompt'] = default_prompt
            
            # 兼容旧版 if_send_pic 配置迁移至 render_mode
            if 'render_mode' not in config_dict and 'if_send_pic' in config_dict:
                old_val = str(config_dict.get('if_send_pic', '')).strip()
                if old_val in ('文字', '0', 'false', 'off', 'no'):
                    config_dict['render_mode'] = 'text'
                else:
                    config_dict['render_mode'] = 'playwright'
            
            # 使用PluginConfig.from_dict()方法进行安全的配置转换
            config = PluginConfig.from_dict(config_dict)
            return config
        except Exception as e:
            self.logger.error(f"配置转换失败: {e}")
            self.logger.info("使用默认配置继续运行")
            return PluginConfig()
    
    # ========== 类常量定义 ==========
    
    # 排行榜数量限制常量（使用模块级常量）
    RANK_COUNT_MIN = 1
    MAX_RANK_COUNT = 100  # 最大排行榜显示人数，来源: constants.MAX_RANK_COUNT
    
    # 图片模式别名常量
    IMAGE_MODE_ENABLE_ALIASES = {'1', 'true', '开', 'on', 'yes'}
    IMAGE_MODE_DISABLE_ALIASES = {'0', 'false', '关', 'off', 'no'}
    
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后注册戳一戳事件监听（仅 aiocqhttp 平台）"""
        try:
            from astrbot.core.platform.platform_registry import get_platform_adapters
            adapters = get_platform_adapters()
            cqhttp = None
            for a in adapters:
                if a.__class__.__name__ == "AiocqhttpAdapter":
                    cqhttp = a
                    break
            if not cqhttp:
                self.logger.info("未检测到 Aiocqhttp 适配器，戳一戳统计不可用")
                return
            client = cqhttp.get_client()
            if not client:
                return
            
            @client.on_notice()
            async def on_poke(event):
                try:
                    if getattr(event, 'sub_type', None) != 'poke':
                        return
                    if str(getattr(event, 'target_id', '')) != str(getattr(event, 'self_id', '')):
                        return
                    gid = str(getattr(event, 'group_id', ''))
                    uid = str(getattr(event, 'user_id', ''))
                    if gid and uid:
                        self.logger.info(f"📌 戳一戳计入发言统计: {uid} in {gid}")
                        await self._record_message_stats(gid, uid, f"用户{uid}")
                except Exception as e:
                    self.logger.debug(f"戳一戳处理异常: {e}")
            
            self.logger.info("✅ 戳一戳统计监听注册成功")
        except Exception as e:
            self.logger.info(f"戳一戳监听注册失败（非 aiocqhttp 平台）: {e}")

    async def initialize(self):
        """初始化插件
        
        异步初始化插件的所有组件,包括数据管理器、配置和图片生成器.
        
        Raises:
            OSError: 当数据目录创建失败时抛出
            IOError: 当配置文件读写失败时抛出
            Exception: 其他初始化相关的异常
            
        Returns:
            None: 无返回值,初始化成功后设置initialized状态
            
        Example:
            >>> plugin = MessageStatsPlugin(context)
            >>> await plugin.initialize()
            >>> print(plugin.initialized)
            True
        """
        try:
            self.logger.info("群发言统计插件初始化中...")
            
            # 步骤1: 初始化数据管理器
            await self._initialize_data_manager()
            
            # 步骤2: 加载插件配置和创建图片生成器
            await self._load_plugin_config()
            
            # 步骤3: 设置数据管理器的配置引用
            self.data_manager.set_plugin_config(self.plugin_config)
            
            # 步骤4: 初始化定时任务管理器
            await self._initialize_timer_manager()
            
            # 步骤5: 设置缓存和最终初始化状态
            await self._setup_caches()
            
            # 启动后台批量保存任务（优化A）
            await self._start_background_save()
            
            self.logger.info("群发言统计插件初始化完成")
            
        except (OSError, IOError) as e:
            self.logger.error(f"插件初始化失败: {e}")
            raise
    
    async def _initialize_data_manager(self):
        """初始化数据管理器
        
        负责初始化数据管理器的核心功能，包括目录创建和基础设置。
        
        Raises:
            OSError: 当数据目录创建失败时抛出
            IOError: 当文件操作失败时抛出
            
        Returns:
            None: 无返回值
        """
        await self.data_manager.initialize()
    
    async def _load_plugin_config(self):
        """更新插件配置和创建图片生成器
        
        从AstrBot配置更新插件配置，并创建和初始化图片生成器。
        
        Raises:
            ImportError: 当导入图片生成器相关模块失败时抛出
            
        Returns:
            None: 无返回值
        """
        # 更新插件配置（从AstrBot配置转换）
        self.plugin_config = self._attach_font_dirs(self._convert_to_plugin_config())
        
        # 同步更新缓存 set
        self._milestone_set = set(self.plugin_config.milestone_targets)
        self._blocked_user_set = set(str(uid) for uid in self.plugin_config.blocked_users)
        self._blocked_group_set = set(str(gid) for gid in self.plugin_config.blocked_groups)
        
        # 创建图片生成器
        self.image_generator = ImageGenerator(self.plugin_config)
        
        # 初始化图片生成器
        try:
            await self.image_generator.initialize()
            self.logger.info("图片生成器初始化成功")
        except ImageGenerationError as e:
            self.logger.warning(f"图片生成器初始化失败，已回退到文字模式: {e}")
            self.image_generator = None
            self.plugin_config.if_send_pic = 0
        
        # 记录当前配置状态
        self.logger.info(f"当前配置: 主题={self.plugin_config.theme}, 图片模式={self.plugin_config.if_send_pic}, 显示人数={self.plugin_config.rand}")
    
    async def _initialize_timer_manager(self):
        """初始化定时任务管理器
        
        创建并初始化定时任务管理器，尝试启动定时任务（不阻塞初始化过程）。
        
        Raises:
            ImportError: 当导入定时任务管理器模块失败时抛出
            OSError: 当系统操作失败时抛出
            IOError: 当文件操作失败时抛出
            RuntimeError: 当运行时错误发生时抛出
            AttributeError: 当属性访问错误时抛出
            ValueError: 当参数值错误时抛出
            TypeError: 当类型错误时抛出
            ConnectionError: 当连接错误时抛出
            asyncio.TimeoutError: 当异步操作超时时抛出
            
        Returns:
            None: 无返回值
        """
        try:
            from .utils.timer_manager import TimerManager
            self.timer_manager = TimerManager(self.data_manager, self.image_generator, self.context, self.group_unified_msg_origins)
            self.timer_manager.update_group_name_cache_batch(self._web_group_name_cache)
            self.logger.info("定时任务管理器初始化成功")
            # 注意：定时任务的启动在 _setup_caches 中统一进行，避免重复启动
                    
        except (ImportError, OSError, IOError) as e:
            self.logger.warning(f"定时任务管理器初始化失败: {e}")
            self.timer_manager = None
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.warning(f"定时任务管理器初始化失败(运行时错误): {e}")
            self.timer_manager = None
    
    async def _setup_caches(self):
        """设置缓存和最终初始化状态
        
        完成插件初始化后的最终设置，包括缓存配置和状态标记。
        
        Raises:
            无特定异常抛出
            
        Returns:
            None: 无返回值
        """
        self.initialized = True
        
        # 插件初始化完成后，尝试启动定时任务
        if self.timer_manager and self.plugin_config.timer_enabled:
            try:
                self.logger.info("插件初始化完成，尝试启动定时任务...")
                # 确保unified_msg_origin映射表被正确传递
                if hasattr(self.timer_manager, 'push_service'):
                    self.timer_manager.push_service.group_unified_msg_origins = self.group_unified_msg_origins
                    self.logger.info(f"定时任务管理器已更新unified_msg_origin映射表: {list(self.group_unified_msg_origins.keys())}")
                else:
                    self.logger.warning("定时任务管理器未完全初始化，无法更新unified_msg_origin映射表")
                
                success = await self.timer_manager.update_config(self.plugin_config, self.group_unified_msg_origins)
                if success:
                    self.logger.info("定时任务启动成功")
                else:
                    self.logger.warning("定时任务启动失败，可能是因为群组unified_msg_origin尚未收集")
                    if self.plugin_config.timer_target_groups:
                        missing_groups = [g for g in self.plugin_config.timer_target_groups if g not in self.group_unified_msg_origins]
                        if missing_groups:
                            self.logger.info(f"缺少unified_msg_origin的群组: {missing_groups}")
            except (ImportError, AttributeError, RuntimeError) as e:
                self.logger.warning(f"定时任务启动失败: {e}")
                # 不影响插件的正常使用
            except (ValueError, TypeError, ConnectionError, asyncio.TimeoutError, KeyError) as e:
                # 修复：替换过于宽泛的Exception为具体异常类型
                self.logger.warning(f"定时任务启动失败(参数错误): {e}")
                # 不影响插件的正常使用
    
    # ========== 脏标记批量保存（优化A） ==========
    
    async def _start_background_save(self):
        """启动后台批量保存任务，每5分钟刷一次脏数据到磁盘"""
        if self._save_task and not self._save_task.done():
            return
        self._save_task = asyncio.create_task(self._background_save_loop())
        self.logger.debug("后台批量保存任务已启动")
    
    async def _stop_background_save(self):
        """停止后台保存任务并立即刷盘"""
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            try:
                await self._save_task
            except (asyncio.CancelledError, Exception):
                pass
            self._save_task = None
        # 停止时强制刷所有脏数据
        await self._flush_dirty_data()
    
    async def _flush_dirty_data(self):
        """刷所有脏标记的数据到磁盘"""
        if self._umo_dirty:
            self._save_unified_msg_origins()
            self._umo_dirty = False
        if self._group_names_dirty:
            self._save_group_names()
            self._group_names_dirty = False
    
    async def _background_save_loop(self):
        """后台循环：每5分钟检查并写入脏数据"""
        try:
            while True:
                await asyncio.sleep(600)  # 10分钟（仅检查脏标记，无脏数据不写盘）
                await self._flush_dirty_data()
        except asyncio.CancelledError:
            await self._flush_dirty_data()
            raise
    
    async def terminate(self):
        """插件卸载清理"""
        try:
            self.logger.info("群发言统计插件卸载中...")
            
            # 停止后台保存并刷脏数据
            await self._stop_background_save()
            
            # 刷新数据管理器的脏数据
            await self.data_manager.flush_all()
            
            # 清理图片生成器
            if self.image_generator:
                await self.image_generator.cleanup()
            
            # 清理数据缓存
            await self.data_manager.clear_cache()
            
            # 清理成员缓存管理器
            self.member_cache.clear_all()
            self.logger.info("成员缓存已清理")
            
            self.initialized = False
            self.logger.info("群发言统计插件卸载完成")
            
        except (OSError, IOError) as e:
            self.logger.error(f"插件卸载失败: {e}")
    
    # ========== 消息监听 ==========
    
    @filter.event_message_type(EventMessageType.ALL)
    async def auto_message_listener(self, event: AstrMessageEvent):
        """自动消息监听器 - 监听所有消息并记录群成员发言统计"""
        # 日期在前的自然语言查询不能用 RegexFilter 注册，否则 AstrBot 后台会把
        # 完整正则表达式展示成命令名；在消息监听器中识别可保留自然语法并避免该问题。
        message_text = str(event.get_message_str() or "").strip()
        if CUSTOM_DATE_RANK_MESSAGE_PATTERN.fullmatch(message_text):
            try:
                custom_period = self._parse_custom_rank_period(message_text)
            except ValueError as exc:
                event.should_call_llm(False)
                yield event.plain_result(str(exc))
                async for result in self._yield_stop_command_event(event):
                    yield result
                return
            async for result in self._show_rank(event, custom_period=custom_period):
                yield result
            async for result in self._yield_stop_command_event(event):
                yield result
            return

        # 跳过命令消息
        if self._is_command_event(event):
            # 命令消息只交给 command handler 处理；同时关闭默认 LLM 回复，避免全量监听器让 is_wake=True
            try:
                event.should_call_llm(False)
            except Exception:
                pass
            return

        # 获取基本信息
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        
        # 跳过非群聊或无效用户
        if not group_id or not user_id:
            return
        
        # 转换为字符串并跳过机器人
        group_id, user_id = str(group_id), str(user_id)
        if self._is_bot_message(event, user_id):
            return
        
        # 检查群聊是否在屏蔽列表中
        if self._is_blocked_group(group_id):
            if self.plugin_config.detailed_logging_enabled:
                self.logger.debug(f"群聊 {group_id} 在屏蔽列表中，跳过统计")
            return
        
        # 收集群组的unified_msg_origin（重要：用于定时推送）
        await self._collect_group_unified_msg_origin(event)
        
        # 检测消息是否包含贴图/表情包/图片组件
        is_sticker = self._is_sticker_message(event)
        
        # 获取用户昵称并记录统计
        snapshot = extract_group_message_snapshot(event, user_id)
        nickname = snapshot.nickname
        avatar_url = snapshot.avatar_url
        # QQ 官方 Bot：openid 无法用 qlogo 拼接头像，昵称也只在发言时携带，
        # 参考 meme 插件通过 appid 拼头像、缓存 d.author.username 昵称。
        if is_official_platform(event):
            cache_official_author_nick(event)
            official_nick = resolve_official_nickname(event, user_id)
            if official_nick:
                nickname = official_nick
            official_avatar = official_avatar_url(event, user_id)
            if official_avatar:
                avatar_url = official_avatar
        group_name = await self._cache_group_name(event, group_id, snapshot.group_name)
        await self._record_message_stats(group_id, user_id, nickname, group_name or snapshot.group_name, avatar_url, is_sticker=is_sticker)
    
    # ========== 排行榜命令 ==========

    @filter.command("发言榜里程碑", alias={'发言里程碑'})
    async def show_my_milestone(self, event: AstrMessageEvent):
        """显示个人里程碑成就卡片，别名：发言里程碑"""
        # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
        event.should_call_llm(False)

        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if not group_id or not user_id:
            yield event.plain_result("无法获取群组或用户信息,请在群聊中使用此命令！")
            return
            
        group_id, user_id = str(group_id), str(user_id)
        
        try:
            # 获取群组数据
            group_data = await self.data_manager.get_group_data(group_id)
            if not group_data:
                yield event.plain_result("该群暂无发言数据！")
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
                if user_data_item.user_id == user_id:
                    target_user_data = user_data_item

            if not target_user_data:
                # 重新计算排名，如果用户没有发言数据
                for user_data_item in group_data:
                    if isinstance(user_data_item, UserData) and user_data_item.message_count > 0:
                        rank += 1
                current_count = 0
            else:
                current_count = target_user_data.message_count
                # 如果已经找到了 target_user_data，需要重新正确计算排名
                rank = 1
                for user_data_item in group_data:
                    if isinstance(user_data_item, UserData) and user_data_item.message_count > current_count:
                        rank += 1

            # 计算击败的活跃群友百分比（基于群内排名）
            percentage = self._calc_beat_percentage(rank, active_members)
            
            # 计算今日发言数
            daily_count = 0
            if target_user_data:
                from datetime import date as date_cls
                today = date_cls.today()
                daily_count = target_user_data.get_message_count_in_period(today, today)
            
            # 计算活跃天数
            active_days = 0
            if target_user_data:
                target_user_data._ensure_message_dates()
                active_days = len(target_user_data._message_dates)
            
            # 获取最后发言日期
            last_date = ""
            if target_user_data and target_user_data.last_date:
                last_date = target_user_data.last_date

            nickname = await self._get_user_display_name(
                event,
                group_id,
                user_id,
                local_nickname=target_user_data.nickname if target_user_data else None,
            )
            
            # 创建群组信息
            unified_msg_origin = self.group_unified_msg_origins.get(str(group_id), "")
            group_info = GroupInfo(group_id=str(group_id), unified_msg_origin=unified_msg_origin)
            group_name = await self._get_group_name(event, group_id)
            group_info.group_name = group_name

            if not self.image_generator:
                yield event.plain_result("图片生成器未初始化，无法生成个人里程碑卡片！")
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
                yield event.plain_result("个人里程碑卡片生成失败！")
                return
            
            # 使用框架标准的 image_result 返回图片
            yield event.image_result(image_path)
            self._schedule_file_cleanup(image_path)

        except Exception as e:
            self.logger.error(f"里程碑获取失败: {e}", exc_info=True)
            yield event.plain_result("获取里程碑失败，请稍后重试！")

    
    @filter.command("发言榜帮助", alias={'发言帮助', '发言榜菜单', '水群榜帮助', '发言统计帮助'})
    async def show_help_menu(self, event: AstrMessageEvent):
        """显示帮助菜单，别名：发言帮助/发言榜菜单/水群榜帮助/发言统计帮助"""
        # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
        event.should_call_llm(False)

        async for result in self._show_help(event):
            yield result
        async for result in self._yield_stop_command_event(event):
            yield result

    @filter.command("发言榜", alias={'水群榜', 'B话榜', '发言排行', '发言统计'})
    async def show_full_rank(self, event: AstrMessageEvent, date_expr: str = ""):
        """显示总排行榜，支持可选的指定日期或日期区间。"""
        if date_expr.strip() in ('帮助', '菜单', 'help'):
            # 兼容「发言榜 帮助」这种带空格的写法
            async for result in self._show_help(event):
                yield result
            async for result in self._yield_stop_command_event(event):
                yield result
            return
        if date_expr:
            try:
                custom_period = self._parse_custom_rank_period(date_expr)
            except ValueError as exc:
                yield event.plain_result(str(exc))
                async for result in self._yield_stop_command_event(event):
                    yield result
                return
            async for result in self._show_rank(event, custom_period=custom_period):
                yield result
        else:
            async for result in self._show_rank(event, RankType.TOTAL):
                yield result
        async for result in self._yield_stop_command_event(event):
            yield result

    @filter.command("今日发言榜", alias={'今日水群榜', '今日发言排行', '今日B话榜'})
    async def show_daily_rank(self, event: AstrMessageEvent):
        """显示今日排行榜，别名：今日水群榜/今日发言排行/今日B话榜"""
        async for result in self._show_rank(event, RankType.DAILY):
            yield result
        async for result in self._yield_stop_command_event(event):
            yield result
    
    @filter.command("本周发言榜", alias={'本周水群榜', '本周发言排行', '本周B话榜'})
    async def show_weekly_rank(self, event: AstrMessageEvent):
        """显示本周排行榜，别名：本周水群榜/本周发言排行/本周B话榜"""
        async for result in self._show_rank(event, RankType.WEEKLY):
            yield result
        async for result in self._yield_stop_command_event(event):
            yield result
    
    @filter.command("本月发言榜", alias={'本月水群榜', '本月发言排行', '本月B话榜'})
    async def show_monthly_rank(self, event: AstrMessageEvent):
        """显示本月排行榜，别名：本月水群榜/本月发言排行/本月B话榜"""
        async for result in self._show_rank(event, RankType.MONTHLY):
            yield result
        async for result in self._yield_stop_command_event(event):
            yield result
    
    @filter.command("本年发言榜", alias={'本年水群榜', '本年发言排行', '本年B话榜', '年榜'})
    async def show_yearly_rank(self, event: AstrMessageEvent):
        """显示本年排行榜，别名：本年水群榜/本年发言排行/本年B话榜/年榜"""
        async for result in self._show_rank(event, RankType.YEARLY):
            yield result
        async for result in self._yield_stop_command_event(event):
            yield result
    
    @filter.command("去年发言榜", alias={'去年水群榜', '去年发言排行', '去年B话榜'})
    async def show_last_year_rank(self, event: AstrMessageEvent):
        """显示去年排行榜，别名：去年水群榜/去年发言排行/去年B话榜"""
        async for result in self._show_rank(event, RankType.LAST_YEAR):
            yield result
        async for result in self._yield_stop_command_event(event):
            yield result

    @filter.command("昨日发言榜", alias={'昨天发言榜', '昨日排行', '昨日水群榜', '昨日B话榜'})
    async def show_yesterday_rank(self, event: AstrMessageEvent):
        """显示昨日排行榜，别名：昨天发言榜/昨日排行/昨日水群榜/昨日B话榜"""
        async for result in self._show_rank(event, RankType.YESTERDAY):
            yield result
        async for result in self._yield_stop_command_event(event):
            yield result

    @filter.command("查看发言", alias={'查询发言', '我的发言'})
    async def show_personal_stats(self, event: AstrMessageEvent, target_user: str = ""):
        """显示个人发言统计，支持查询他人(带@或填ID)"""
        # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
        event.should_call_llm(False)

        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
            return

        group_id = str(group_id)
        target_uid = str(event.get_sender_id())
        
        # 解析目标用户
        if target_user:
            # 尝试从消息组件提取 AT
            from astrbot.core.message.components import At
            if hasattr(event, 'message_obj') and event.message_obj:
                for comp in event.message_obj.message:
                    if isinstance(comp, At) and hasattr(comp, 'qq'):
                        target_uid = str(comp.qq)
                        break
                else:
                    # 正则备用提取
                    m = re.search(r'qq="?(\d+)"?', event.message_str or "")
                    if m:
                        target_uid = str(m.group(1))
                    else:
                        target_str = target_user.strip().replace('@', '')
                        if target_str.isdigit():
                            target_uid = target_str
        
        try:
            group_data = await self.data_manager.get_group_data(group_id)
            if not group_data:
                yield event.plain_result("该群暂无发言数据！")
                return
            
            target_user_data = None
            total_messages_all = 0
            rank = 1
            
            # 聚合数据
            for u in group_data:
                if not isinstance(u, UserData):
                    continue
                total_messages_all += u.message_count
                if u.user_id == target_uid:
                    target_user_data = u
            
            if not target_user_data:
                yield event.plain_result(f"未找到该用户的发言记录！")
                return
                
            # 计算排名
            for u in group_data:
                if isinstance(u, UserData) and u.message_count > target_user_data.message_count:
                    rank += 1
            
            from datetime import date as date_cls, timedelta
            today = date_cls.today()
            week_start = today - timedelta(days=today.weekday())
            month_start = today.replace(day=1)
            
            daily_count = target_user_data.get_message_count_in_period(today, today)
            yesterday = today - timedelta(days=1)
            yesterday_count = target_user_data.get_message_count_in_period(yesterday, yesterday)
            weekly_count = target_user_data.get_message_count_in_period(week_start, today)
            monthly_count = target_user_data.get_message_count_in_period(month_start, today)
            
            target_user_data._ensure_message_dates()
            active_days = len(target_user_data._message_dates)
            percentage = (target_user_data.message_count / total_messages_all * 100) if total_messages_all > 0 else 0
            
            nickname = await self._get_user_display_name(
                event,
                group_id,
                target_uid,
                local_nickname=target_user_data.nickname,
            )
            group_name = await self._get_group_name(event, group_id)
            group_info = GroupInfo(group_id=group_id, group_name=group_name)
            
            avatar_url = ""
            if self.image_generator:
                # 预获取真实头像
                await self.image_generator._prefetch_avatars([target_user_data], group_info)
                avatar_url = self.image_generator._get_avatar_url(target_user_data, nickname, group_info)
                
            data = {
                'nickname': nickname,
                'user_id': target_uid,
                'avatar_url': avatar_url,
                'total': target_user_data.message_count,
                'daily': daily_count,
                'yesterday': yesterday_count,
                'weekly': weekly_count,
                'monthly': monthly_count,
                'active_days': active_days,
                'last_date': target_user_data.last_date,
                'llm_title': target_user_data.llm_title or target_user_data.display_title,
                'llm_color': target_user_data.llm_title_color or target_user_data.display_title_color,
                'rank': rank,
                'percentage': f"{percentage:.1f}",
            }
            
            if self.plugin_config.if_send_pic and self.image_generator:
                img_path = await self.image_generator.generate_personal_stats_image(data, group_info)
                if img_path:
                    yield event.image_result(img_path)
                    self._schedule_file_cleanup(img_path)
                    return
            
            text = f"【个人发言统计】\n用户: {nickname} ({target_uid})\n"
            text += f"总发言: {target_user_data.message_count}次 (第{rank}名, 占比{percentage:.1f}%)\n"
            text += f"今日: {daily_count}次 | 本周: {weekly_count}次 | 本月: {monthly_count}次\n"
            text += f"活跃天数: {active_days}天 | 最近互动: {target_user_data.last_date or '未知'}"
            if data['llm_title']:
                text += f"\n当前头衔: {data['llm_title']}"
            yield event.plain_result(text)
            
        except Exception as e:
            self.logger.error(f"查看发言失败: {e}", exc_info=True)
            yield event.plain_result("查询发言记录失败,请稍后重试！")
    
    # ========== 设置命令 ==========
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置发言榜数量")
    async def set_rank_count(self, event: AstrMessageEvent):
        """设置排行榜显示人数"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取群组ID
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            
            group_id = str(group_id)
            
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            if not args:
                yield event.plain_result("请指定数量！用法:#设置发言榜数量 10")
                return
            
            # 验证数量
            try:
                count = int(args[0])
                if count < self.RANK_COUNT_MIN or count > self.MAX_RANK_COUNT:
                    yield event.plain_result(f"数量必须在{self.RANK_COUNT_MIN}-{self.MAX_RANK_COUNT}之间！")
                    return
            except ValueError:
                yield event.plain_result("数量必须是数字！")
                return
            
            # 保存配置
            config = await self.data_manager.get_config()
            config.rand = count
            await self.data_manager.save_config(config)
            
            yield event.plain_result(f"排行榜显示人数已设置为 {count} 人！")
            
        except Exception as e:
            self.logger.error(f"设置排行榜数量失败: {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置发言榜图片")
    async def set_image_mode(self, event: AstrMessageEvent):
        """设置排行榜的显示模式（图片或文字）
        
        根据用户输入的参数设置排行榜的显示模式：
        - 1/true/开/on/yes: 设置为图片模式
        - 0/false/关/off/no: 设置为文字模式
        
        返回相应的设置成功提示信息。
        """
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取群组ID
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            
            group_id = str(group_id)
            
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            if not args:
                yield event.plain_result("请指定模式！用法:#设置发言榜图片 1")
                return
            
            # 验证模式
            mode = args[0].lower()
            if mode in self.IMAGE_MODE_ENABLE_ALIASES:
                send_pic = 1
                mode_text = "图片模式"
            elif mode in self.IMAGE_MODE_DISABLE_ALIASES:
                send_pic = 0
                mode_text = "文字模式"
            else:
                yield event.plain_result("模式参数错误！可用:1/true/开 或 0/false/关")
                return
            
            # 保存配置
            config = await self.data_manager.get_config()
            config.if_send_pic = send_pic
            await self.data_manager.save_config(config)
            
            yield event.plain_result(f"排行榜显示模式已设置为 {mode_text}！")
            
        except Exception as e:
            self.logger.error(f"设置图片模式失败: {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清除发言榜单")
    async def clear_message_ranking(self, event: AstrMessageEvent):
        """清除发言榜单"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            group_id = str(group_id)
            
            success = await self.data_manager.clear_group_data(group_id)
            
            if success:
                yield event.plain_result("本群发言榜单已清除！")
            else:
                yield event.plain_result("清除榜单失败,请稍后重试！")
            
        except (IOError, OSError, FileNotFoundError) as e:
            self.logger.error(f"清除榜单失败: {e}")
            yield event.plain_result("清除榜单失败,请稍后重试！")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("刷新发言榜群成员缓存")
    async def refresh_group_members_cache(self, event: AstrMessageEvent):
        """刷新群成员列表缓存"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            group_id = str(group_id)
            
            # 使用 MemberCacheManager 刷新缓存
            success = await self.member_cache.refresh_group_cache(event, group_id)
            
            if success:
                yield event.plain_result("群成员缓存和字典缓存已刷新！")
            else:
                yield event.plain_result("刷新缓存失败,请稍后重试！")
            
        except Exception as e:
            self.logger.error(f"刷新群成员缓存失败: {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
    
    @filter.command("发言榜缓存状态")
    async def show_cache_status(self, event: AstrMessageEvent):
        """显示缓存状态"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取数据管理器缓存统计
            cache_stats = await self.data_manager.get_cache_stats()
            
            # 获取成员缓存管理器统计
            member_cache_stats = self.member_cache.get_cache_stats()
            
            status_msg = [
                "📊 缓存状态报告",
                "━━━━━━━━━━━━━━",
                f"💾 数据缓存: {cache_stats['data_cache_size']}/{cache_stats['data_cache_maxsize']}",
                f"⚙️ 配置缓存: {cache_stats['config_cache_size']}/{cache_stats['config_cache_maxsize']}",
                f"👥 群成员缓存: {member_cache_stats['members_cache_size']}/{member_cache_stats['members_cache_maxsize']}",
                f"📖 字典缓存: {member_cache_stats['dict_cache_size']}",
                "━━━━━━━━━━━━━━",
                "🕐 数据缓存TTL: 5分钟",
                "🕐 配置缓存TTL: 1分钟", 
                "🕐 群成员缓存TTL: 5分钟"
            ]
            
            yield event.plain_result('\n'.join(status_msg))
            
        except Exception as e:
            self.logger.error(f"显示缓存状态失败: {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
    
    # ========== 私有方法 ==========
    
    # ========== 定时功能管理命令 ==========
    
    @filter.command("发言榜定时状态")
    async def timer_status(self, event: AstrMessageEvent):
        """查看定时任务状态"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            
            # 构建状态信息
            status_lines = [
                "📊 定时任务状态",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                "🔧 基础设置",
                f"┌─ 定时功能: {'✅ 已启用' if config.timer_enabled else '❌ 已禁用'}",
                f"├─ 推送时间: {config.timer_push_time}",
                f"├─ 排行榜类型: {self._get_rank_type_text(config.timer_rank_type)}",
                f"├─ 推送模式: {'图片' if config.if_send_pic else '文字'}",
                f"└─ 显示人数: {config.rand} 人",
                "",
                "🎯 目标群组"
            ]
            
            # 添加目标群组信息
            if config.timer_target_groups:
                for i, group_id in enumerate(config.timer_target_groups, 1):
                    origin_status = "✅" if str(group_id) in self.group_unified_msg_origins else "❌"
                    status_lines.append(f"┌─ {i}. {group_id} {origin_status}")
                
                # 添加unified_msg_origin说明
                status_lines.append("└─ 💡 unified_msg_origin状态: ✅已收集/❌未收集")
                status_lines.append("   (❌状态需在群组发送消息收集)")
            else:
                status_lines.append("┌─ ⚠️ 未设置任何目标群组")
                status_lines.append("└─ 💡 使用 #设置定时群组 添加群组")
            
            # 添加定时任务状态
            if self.timer_manager:
                timer_status = await self.timer_manager.get_status()
                status_lines.extend([
                    "",
                    "⏰ 任务状态",
                    f"┌─ 运行状态: {self._get_status_text(timer_status['status'])}",
                    f"├─ 下次推送: {timer_status['next_push_time'] or '未设置'}",
                    f"└─ 剩余时间: {timer_status['time_until_next'] or 'N/A'}"
                ])
            
            yield event.plain_result('\n'.join(status_lines))
            
        except (IOError, OSError, RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"获取定时状态失败: {e}")
            yield event.plain_result("获取定时状态失败，请稍后重试！")
    
    async def _apply_timer_config_update(self, config: PluginConfig) -> bool:
        config.sync_primary_timer_task()
        self.plugin_config = config
        await self.data_manager.save_config(config)
        self.data_manager.set_plugin_config(config)
        if self.timer_manager:
            success = await self.timer_manager.update_config(config, self.group_unified_msg_origins, force_restart=True)
            if not success:
                self.logger.warning("配置已保存，但定时任务运行态更新失败，可能需要重新加载插件或等待群组 unified_msg_origin 收集完成")
            return success
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("手动推送发言榜")
    async def manual_push(self, event: AstrMessageEvent):
        """手动推送排行榜"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            if not self.timer_manager:
                yield event.plain_result("定时管理器未初始化，无法执行手动推送！")
                return
            
            # 检查TimerManager是否有有效的context
            if not hasattr(self.timer_manager, 'context') or not self.timer_manager.context:
                yield event.plain_result("❌ 定时管理器未完全初始化！\n\n💡 可能的原因：\n• 插件初始化过程中出现异常\n• 上下文信息缺失\n\n🔧 解决方案：\n• 重启机器人或重新加载插件\n• 检查插件配置是否正确")
                return
            
            # 使用当前转换的配置而不是从文件读取
            config = self.plugin_config
            
            if not config.timer_target_groups:
                yield event.plain_result("未设置目标群组，请先使用 #设置定时群组 设置目标群组！")
                return
            
            # 执行手动推送
            yield event.plain_result("正在执行手动推送，请稍候...")
            
            success = await self.timer_manager.manual_push(config)
            
            if success:
                yield event.plain_result("✅ 手动推送执行成功！")
            else:
                yield event.plain_result("❌ 手动推送执行失败！\n\n💡 可能的原因：\n• 目标群组暂无发言数据\n• 群组ID配置错误\n• 机器人缺少群组发言权限\n\n🔧 解决方案：\n• 确认目标群组已有统计数据\n• 检查定时推送群组配置\n• 检查机器人是否有群组发言权限")
            
        except (AttributeError, TypeError, RuntimeError, ValueError, KeyError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理手动推送请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置发言榜定时时间")
    async def set_timer_time(self, event: AstrMessageEvent):
        """设置定时推送时间
        
        自动设置当前群组为定时群组并启用定时功能
        """
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            if not args:
                yield event.plain_result("请指定时间！用法:#设置定时时间 16:12")
                return
            
            time_str = args[0]
            
            # 验证时间格式
            if not self._validate_time_format(time_str):
                yield event.plain_result("时间格式错误！请使用 HH:MM 格式，例如：16:12")
                return
            
            # 获取当前群组ID
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取当前群组ID！")
                return
            
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            config.timer_push_time = time_str
            
            # 自动设置当前群组为定时群组
            if str(group_id) not in config.timer_target_groups:
                config.timer_target_groups.append(str(group_id))
            
            # 自动启用定时功能
            config.timer_enabled = True
            success = await self._apply_timer_config_update(config)
            rank_type_text = self._get_rank_type_text(config.timer_rank_type)
            if self.timer_manager:
                if success:
                    yield event.plain_result(
                        f"✅ 定时推送设置完成！\n"
                        f"• 推送时间：{time_str}\n"
                        f"• 目标群组：{group_id}\n"
                        f"• 排行榜类型：{rank_type_text}\n"
                        f"• 状态：已启用\n\n"
                        f"💡 提示：如果推送失败，请在群组中发送任意消息以收集unified_msg_origin"
                    )
                else:
                    yield event.plain_result(
                        f"⚠️ 定时推送设置部分完成！\n"
                        f"• 推送时间：{time_str}\n"
                        f"• 目标群组：{group_id}\n"
                        f"• 排行榜类型：{rank_type_text}\n"
                        f"• 状态：配置保存成功，但定时任务启动失败\n\n"
                        f"💡 提示：如果推送失败，请在群组中发送任意消息以收集unified_msg_origin"
                    )
            else:
                yield event.plain_result(f"✅ 定时推送配置已保存！\n• 推送时间：{time_str}\n• 目标群组：{group_id}\n• 排行榜类型：{rank_type_text}\n• 状态：配置保存成功\n\n💡 提示：定时管理器未初始化，请检查插件配置")
            
        except (ValueError, IOError, OSError, RuntimeError, AttributeError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理设置定时时间请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置发言榜定时群组")
    async def set_timer_groups(self, event: AstrMessageEvent):
        """设置定时推送目标群组"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            if not args:
                yield event.plain_result("请指定群组ID！用法:#设置发言榜定时群组 123456789 987654321")
                return
            
            # 验证群组ID
            valid_groups = []
            for group_id in args:
                if group_id.isdigit() and len(group_id) >= 5:
                    valid_groups.append(group_id)
                else:
                    yield event.plain_result(f"群组ID格式错误: {group_id}，必须是5位以上数字")
                    return
            
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            config.timer_target_groups = valid_groups
            await self._apply_timer_config_update(config)

            groups_text = "\n".join([f"   • {group_id}" for group_id in valid_groups])
            yield event.plain_result(f"✅ 定时推送目标群组已设置：\n{groups_text}")
            
        except (ValueError, IOError, OSError, RuntimeError, AttributeError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理设置定时群组请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除发言榜定时群组")
    async def remove_timer_groups(self, event: AstrMessageEvent):
        """删除定时推送目标群组"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            current_groups = config.timer_target_groups
            
            if not args:
                # 清空所有定时群组
                config.timer_target_groups = []
                await self._apply_timer_config_update(config)

                yield event.plain_result("✅ 已清空所有定时推送目标群组")
                return
            
            # 删除指定群组
            groups_to_remove = []
            invalid_groups = []
            
            for group_id in args:
                if group_id.isdigit() and len(group_id) >= 5:
                    groups_to_remove.append(group_id)
                else:
                    invalid_groups.append(group_id)
            
            if invalid_groups:
                yield event.plain_result(f"群组ID格式错误: {', '.join(invalid_groups)}，必须是5位以上数字")
                return
            
            # 从当前群组列表中移除指定群组
            remaining_groups = [group for group in current_groups if group not in groups_to_remove]
            
            # 保存配置
            config.timer_target_groups = remaining_groups
            await self._apply_timer_config_update(config)

            if groups_to_remove:
                removed_text = "\n".join([f"   • {group_id}" for group_id in groups_to_remove])
                remaining_text = "\n".join([f"   • {group_id}" for group_id in remaining_groups]) if remaining_groups else "   无"
                yield event.plain_result(f"✅ 已删除定时推送目标群组：\n{removed_text}\n\n📋 剩余群组：\n{remaining_text}")
            else:
                yield event.plain_result("⚠️ 未找到要删除的群组")
            
        except (ValueError, IOError, OSError, RuntimeError, AttributeError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理删除定时群组请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("启用发言榜定时")
    async def enable_timer(self, event: AstrMessageEvent):
        """启用定时推送功能"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            
            # 检查配置
            if not config.timer_target_groups:
                yield event.plain_result("请先设置目标群组！用法:#设置定时群组 群组ID")
                return
            
            # 启用定时功能
            config.timer_enabled = True
            success = await self._apply_timer_config_update(config)

            if self.timer_manager:
                if success:
                    yield event.plain_result("✅ 定时推送功能已启用！")
                else:
                    yield event.plain_result("⚠️ 定时推送功能启用失败，请检查配置！")
            else:
                yield event.plain_result("⚠️ 定时管理器未初始化！")
            
        except (IOError, OSError, RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理启用定时请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("禁用发言榜定时")
    async def disable_timer(self, event: AstrMessageEvent):
        """禁用定时推送功能"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            
            # 禁用定时功能
            config.timer_enabled = False
            await self._apply_timer_config_update(config)

            yield event.plain_result("✅ 定时推送功能已禁用！")
            
        except (IOError, OSError, RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理禁用定时请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置发言榜定时类型")
    async def set_timer_type(self, event: AstrMessageEvent):
        """设置定时推送的排行榜类型"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            event.should_call_llm(False)
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            if not args:
                yield event.plain_result("请指定排行榜类型！用法:#设置定时类型 total/daily/week/month")
                return
            
            rank_type = args[0].lower()
            rank_type_aliases = {
                '总榜': 'total',
                '今日榜': 'daily',
                '日榜': 'daily',
                '昨日榜': 'yesterday',
                '昨日': 'yesterday',
                '昨天': 'yesterday',
                '本周榜': 'weekly',
                '周榜': 'weekly',
                '本月榜': 'monthly',
                '月榜': 'monthly',
                '本年榜': 'yearly',
                '年榜': 'yearly',
                '去年榜': 'lastyear',
                '去年': 'lastyear'
            }
            rank_type = rank_type_aliases.get(rank_type, rank_type)

            # 验证排行榜类型
            valid_types = ['total', 'daily', 'yesterday', 'week', 'weekly', 'month', 'monthly', 'year', 'yearly', 'lastyear', 'last_year']
            if rank_type not in valid_types:
                yield event.plain_result("排行榜类型错误！可用类型: 总榜/今日榜/昨日榜/本周榜/本月榜/本年榜/去年榜")
                return
            
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            config.timer_rank_type = rank_type
            await self._apply_timer_config_update(config)

            type_text = self._get_rank_type_text(rank_type)
            yield event.plain_result(f"✅ 定时推送排行榜类型已设置为 {type_text}！")
            
        except (ValueError, IOError, OSError, RuntimeError, AttributeError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理设置定时类型请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    # ========== 辅助方法 ==========
    
    def _log_operation_result(self, operation_name: str, success: bool, details: str = ""):
        """公共的操作结果日志记录方法，减少代码重复"""
        if success:
            self.logger.info(f"{operation_name}成功{details}")
        else:
            self.logger.warning(f"{operation_name}失败{details}")
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _get_status_text(self, status: str) -> str:
        """获取状态文本"""
        status_mapping = {
            'stopped': '已停止',
            'running': '运行中',
            'error': '错误',
            'paused': '已暂停'
        }
        return status_mapping.get(status, status)
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _format_datetime(self, dt_str: str) -> str:
        """格式化日期时间"""
        if not dt_str:
            return '未设置'
        
        try:
            # 解析ISO格式的时间字符串
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            return dt.strftime('%m月%d日 %H:%M')
        except (ValueError, TypeError):
            # 修复：替换过于宽泛的except:为具体异常类型
            return dt_str
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _validate_time_format(self, time_str: str) -> bool:
        """验证时间格式"""
        # 使用模块级别导入的 re 模块
        pattern = r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'
        return bool(re.match(pattern, time_str))
    

    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _get_rank_type_text(self, rank_type: str) -> str:
        """获取排行榜类型的中文描述
        
        Args:
            rank_type: 排行榜类型字符串
            
        Returns:
            str: 排行榜类型的中文描述
        """
        type_mapping = {
            'total': '总排行榜',
            '总榜': '总排行榜',
            'daily': '今日排行榜',
            '今日榜': '今日排行榜',
            '日榜': '今日排行榜',
            'week': '本周排行榜',
            'weekly': '本周排行榜',
            '本周榜': '本周排行榜',
            '周榜': '本周排行榜',
            'month': '本月排行榜',
            'monthly': '本月排行榜',
            '本月榜': '本月排行榜',
            '月榜': '本月排行榜',
            'year': '本年排行榜',
            'yearly': '本年排行榜',
            '本年榜': '本年排行榜',
            '年榜': '本年排行榜',
            'lastyear': '去年排行榜',
            'last_year': '去年排行榜',
            '去年榜': '去年排行榜',
            '去年': '去年排行榜'
        }
        return type_mapping.get(rank_type, rank_type)
