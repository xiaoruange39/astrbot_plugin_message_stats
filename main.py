"""
AstrBot 群发言统计插件
统计群成员发言次数,生成排行榜
"""

# 标准库导入
import asyncio
import os
import re
import aiofiles
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

# AstrBot框架导入
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger as astrbot_logger

# 本地模块导入
from .utils.data_manager import DataManager
from .utils.image_generator import ImageGenerator, ImageGenerationError
from .utils.validators import Validators
from .utils.platform_helper import PlatformHelper
from .utils.member_cache_manager import MemberCacheManager
from .utils.llm_analyzer import LLMAnalyzer

from .utils.models import (
    UserData, PluginConfig, GroupInfo, MessageDate, 
    RankType
)

# 异常处理装饰器导入
from .utils.exception_handlers import (
    exception_handler,
    data_operation_handler,
    file_operation_handler,
    safe_execute,
    log_exception,
    ExceptionConfig,
    safe_execute_with_context,
    safe_data_operation,
    safe_file_operation,
    safe_cache_operation,
    safe_config_operation,
    safe_calculation,
    safe_generation,
    safe_timer_operation
)

# ========== 全局常量定义 ==========
# 从集中管理的常量模块导入
from .utils.constants import (
    MAX_RANK_COUNT,
    USER_NICKNAME_CACHE_TTL,
    GROUP_MEMBERS_CACHE_TTL as CACHE_TTL_SECONDS
)

@register("astrbot_plugin_message_stats", "xiaoruange39", "群发言统计插件", "1.9.0")
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
            ["GET"],
            "删除群组发言数据",
        )
        self.logger = astrbot_logger
        
        # 使用StarTools获取插件数据目录
        data_dir = StarTools.get_data_dir('message_stats')
        
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
        
        # 成员缓存管理器 - 管理群成员列表缓存和用户昵称缓存
        # 使用分层缓存策略（昵称缓存 → 字典缓存 → API获取），
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
    def _load_unified_msg_origins(self):
        """从文件加载持久化的 unified_msg_origin 映射表"""
        try:
            if self._umo_file.exists():
                import json
                with open(str(self._umo_file), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.group_unified_msg_origins = data
                    self.logger.info(f"已加载 unified_msg_origin 映射表: {len(data)} 条记录")
        except Exception as e:
            self.logger.debug(f"加载 unified_msg_origin 文件失败: {e}")

    def _save_unified_msg_origins(self):
        """将 unified_msg_origin 映射表保存到文件"""
        try:
            self._umo_file.parent.mkdir(parents=True, exist_ok=True)
            import json
            with open(str(self._umo_file), 'w', encoding='utf-8') as f:
                json.dump(self.group_unified_msg_origins, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.debug(f"保存 unified_msg_origin 文件失败: {e}")

    def _load_group_names(self):
        """从文件加载持久化的群组名称缓存"""
        try:
            if self._group_names_file.exists():
                import json
                with open(str(self._group_names_file), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._web_group_name_cache = data
                    self.logger.info(f"已加载群组名称缓存: {len(data)} 条记录")
        except Exception as e:
            self.logger.debug(f"加载群组名称文件失败: {e}")

    def _save_group_names(self):
        """将群组名称持久化到文件"""
        try:
            self._group_names_file.parent.mkdir(parents=True, exist_ok=True)
            import json
            with open(str(self._group_names_file), 'w', encoding='utf-8') as f:
                json.dump(self._web_group_name_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.debug(f"保存群组名称文件失败: {e}")
    
    async def page_stats(self):
        try:
            from quart import request
            import os
            gid = request.args.get('group_id') if request else None
            if gid:
                users = await self.data_manager.get_group_data(gid)
                if not users:
                    return self._jsonify({"status":"ok","data":{"group":None}})
                act = [u for u in users if u.message_count>0]
                act.sort(key=lambda x:x.message_count,reverse=True)
                tm = sum(u.message_count for u in act)
                tu = []
                for u in act[:self.plugin_config.rand]:
                    pct = (u.message_count/tm*100) if tm>0 else 0
                    tu.append({"nickname":u.nickname,"message_count":u.message_count,"title":u.display_title or "","last_date":u.last_date or "","percentage":round(pct,1)})
                fp2 = self.data_manager.groups_dir / f"{gid}.json"
                fs2 = ""
                if fp2.exists():
                    s2 = os.path.getsize(str(fp2))
                    fs2 = f"{s2/1024:.1f}KB" if s2<1024*1024 else f"{s2/1024/1024:.1f}MB"
                gn = self._web_group_name_cache.get(str(gid), f"群{gid}")
                return self._jsonify({"status":"ok","data":{"group":{"group_id":gid,"group_name":gn,"display_name":f"{gn} - {gid}","file_size":fs2,"total_messages":tm,"user_count":len(act),"top_users":tu}}})
            gd = []
            ag = await self.data_manager.get_all_groups()
            for g2 in ag[:50]:
                us = await self.data_manager.get_group_data(g2)
                if not us: continue
                ac = [u for u in us if u.message_count>0]
                ac.sort(key=lambda x:x.message_count,reverse=True)
                fp = self.data_manager.groups_dir / f"{g2}.json"
                fs = ""
                if fp.exists():
                    s = os.path.getsize(str(fp))
                    fs = f"{s/1024:.1f}KB" if s<1024*1024 else f"{s/1024/1024:.1f}MB"
                gn = self._web_group_name_cache.get(str(g2), f"群{g2}")
                gd.append({"group_id":g2,"group_name":gn,"display_name":f"{gn} - {g2}","file_size":fs,"total_messages":sum(u.message_count for u in ac),"user_count":len(ac)})
            ts = None
            if self.timer_manager:
                s = await self.timer_manager.get_status()
                ts = {"running":s["status"]=="running","next_push":str(s.get("next_push_time","") or "")}
            c = self.plugin_config
            return self._jsonify({"status":"ok","data":{"groups":gd,"config":{"rand":c.rand,"if_send_pic":c.if_send_pic},"timer":ts}})
        except Exception as e:
            return self._jsonify({"status":"error","message":str(e)})
    
    async def page_delete(self):
        """Web API: 删除群组数据"""
        try:
            from quart import request
            gid = request.args.get('group_id') if request else None
            if not gid:
                return self._jsonify({"status":"error","message":"缺少group_id参数"})
            ok = await self.data_manager.clear_group_data(gid)
            if ok:
                self.logger.info(f"Web面板删除群组数据: {gid}")
                return self._jsonify({"status":"ok","message":"已删除"})
            return self._jsonify({"status":"error","message":"删除失败"})
        except Exception as e:
            return self._jsonify({"status":"error","message":str(e)})

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
            
            # 使用PluginConfig.from_dict()方法进行安全的配置转换
            config = PluginConfig.from_dict(config_dict)
            return config
        except Exception as e:
            self.logger.error(f"配置转换失败: {e}")
            self.logger.info("使用默认配置继续运行")
            return PluginConfig()
    
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
                
                # 从 unified_msg_origin 中提取群组ID（格式如 "Amydd:GroupMessage:-1003715592711"）
                # 提取最后一个 ":" 之后的部分作为备用键
                try:
                    extracted_id = unified_msg_origin.rsplit(':', 1)[-1]
                    if extracted_id and extracted_id != group_id_str:
                        self.group_unified_msg_origins[extracted_id] = unified_msg_origin
                except (AttributeError, IndexError, ValueError):
                    pass
                
                # 同时以 unified_msg_origin 本身作为键存储
                # 这样无论 timer_target_groups 中填的是群号还是 unified_msg_origin 都能匹配
                self.group_unified_msg_origins[unified_msg_origin] = unified_msg_origin
                
                # 持久化到文件（重启后自动恢复）
                self._save_unified_msg_origins()
                
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
                            if group_id_str == target_id or extracted_id == target_id or unified_msg_origin == target_id:
                                is_target_group = True
                                break
                        
                        if self.plugin_config.timer_enabled and is_target_group:
                            self.logger.info(f"检测到目标群组 {group_id} 的 unified_msg_origin 已更新，更新定时任务配置...")
                            # 确保unified_msg_origin映射表是最新的
                            self.timer_manager.push_service.group_unified_msg_origins = self.group_unified_msg_origins
                            success = await self.timer_manager.update_config(self.plugin_config, self.group_unified_msg_origins)
                            if success:
                                self.logger.info(f"定时任务配置更新成功")
                            else:
                                self.logger.warning(f"定时任务配置更新失败")
                

        except (AttributeError, KeyError, TypeError) as e:
            self.logger.error(f"收集群组unified_msg_origin失败: {e}")
        except (RuntimeError, OSError, IOError, ImportError, ValueError) as e:
            self.logger.error(f"收集群组unified_msg_origin失败(系统错误): {e}")
    async def _cache_group_name(self, event: Optional[AstrMessageEvent], group_id: str):
        """获取并缓存群组名称（跨平台通用）
        
        使用 PlatformHelper 统一获取群组名称，支持所有平台。
        同时更新 Web 页面缓存并持久化到文件，供 page_stats 直接读取。
        
        每次生成发言榜時都重新获取群名，群改名后立即同步。
        
        Args:
            event: 消息事件对象（可为 None，此时尝试从 context 获取 API 客户端）
            group_id: 群组ID
        """
        try:
            group_id_str = str(group_id)
            
            # 使用 PlatformHelper 统一获取群组名称（跨平台通用）
            helper = PlatformHelper(event, self.context)
            group_name = await helper.get_group_name(group_id)
            
            # 如果获取到群名，更新所有缓存
            if group_name:
                group_name = str(group_name).strip()
                
                # 只在群名发生变化时才保存和日志
                old_name = self._web_group_name_cache.get(group_id_str)
                if old_name != group_name:
                    self._web_group_name_cache[group_id_str] = group_name
                    self._save_group_names()
                    self.logger.info(f"已获取群组 {group_id} 的名称: {group_name}")
                
                # 更新到 timer_manager 的内存缓存
                if self.timer_manager:
                    self.timer_manager.update_group_name_cache(group_id, group_name)
                
        except (AttributeError, KeyError, TypeError, RuntimeError) as e:
            self.logger.debug(f"缓存群组名称失败: {e}")
    
    async def _collect_group_unified_msg_origins(self):
        """收集所有群组的unified_msg_origin（从缓存中获取）"""
        # 这个方法用于初始化时的批量收集
        # 由于没有event对象，我们先返回空字典
        # 实际的收集将在命令执行时进行
        return self.group_unified_msg_origins.copy()
    
    # ========== 类常量定义 ==========
    
    # 排行榜数量限制常量（使用模块级常量）
    RANK_COUNT_MIN = 1
    # MAX_RANK_COUNT 已从 constants 模块导入，不再重复定义
    
    # 图片模式别名常量
    IMAGE_MODE_ENABLE_ALIASES = {'1', 'true', '开', 'on', 'yes'}
    IMAGE_MODE_DISABLE_ALIASES = {'0', 'false', '关', 'off', 'no'}
    
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
        self.plugin_config = self._convert_to_plugin_config()
        
        # 创建图片生成器
        self.image_generator = ImageGenerator(self.plugin_config)
        
        # 初始化图片生成器
        try:
            await self.image_generator.initialize()
            self.logger.info("图片生成器初始化成功")
        except ImageGenerationError as e:
            self.logger.warning(f"图片生成器初始化失败: {e}")
        
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
    
    async def terminate(self):
        """插件卸载清理
        
        异步清理插件的所有资源,包括浏览器实例、缓存和临时文件.
        确保插件卸载时不会留下资源泄漏.
        
        Raises:
            OSError: 当清理文件或目录失败时抛出
            IOError: 当文件操作失败时抛出
            Exception: 其他清理相关的异常
            
        Returns:
            None: 无返回值,清理完成后设置initialized状态为False
            
        Example:
            >>> await plugin.terminate()
            >>> print(plugin.initialized)
            False
        """
        try:
            self.logger.info("群发言统计插件卸载中...")
            
            # 刷新所有脏数据到磁盘（延迟写入优化）
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
        # 跳过命令消息
        message_str = getattr(event, 'message_str', '')
        if not message_str or message_str.startswith(('%', '/')):
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
        
        # 获取用户昵称并记录统计
        nickname = await self._get_user_display_name(event, group_id, user_id)
        await self._record_message_stats(group_id, user_id, nickname)
    
    def _is_bot_message(self, event: AstrMessageEvent, user_id: str) -> bool:
        """检查是否为机器人消息"""
        try:
            self_id = event.get_self_id()
            return self_id and user_id == str(self_id)
        except (AttributeError, KeyError, TypeError):
            return False
    
    async def _record_message_stats(self, group_id: str, user_id: str, nickname: str):
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
            await self._process_message_stats(group_id, user_id, nickname)
            
        except ValueError as e:
            self.logger.error(f"记录消息统计失败(参数验证错误): {e}", exc_info=True)
        except TypeError as e:
            self.logger.error(f"记录消息统计失败(类型错误): {e}", exc_info=True)
        except KeyError as e:
            self.logger.error(f"记录消息统计失败(数据格式错误): {e}", exc_info=True)
        except asyncio.TimeoutError as e:
            self.logger.error(f"记录消息统计失败(超时错误): {e}", exc_info=True)
        except ConnectionError as e:
            self.logger.error(f"记录消息统计失败(连接错误): {e}", exc_info=True)
        except asyncio.CancelledError as e:
            self.logger.error(f"记录消息统计失败(操作取消): {e}", exc_info=True)
        except (IOError, OSError) as e:
            self.logger.error(f"记录消息统计失败(系统错误): {e}", exc_info=True)
        except AttributeError as e:
            self.logger.error(f"记录消息统计失败(属性错误): {e}", exc_info=True)
        except RuntimeError as e:
            self.logger.error(f"记录消息统计失败(运行时错误): {e}", exc_info=True)
        except ImportError as e:
            self.logger.error(f"记录消息统计失败(导入错误): {e}", exc_info=True)
        except (FileNotFoundError, PermissionError, UnicodeError, MemoryError, SystemError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"记录消息统计失败(系统资源错误): {e}", exc_info=True)
    
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
    
    async def _process_message_stats(self, group_id: str, user_id: str, nickname: str):
        """处理消息统计和记录
        
        执行实际的消息统计更新操作，并记录结果日志。
        智能缓存管理：检查昵称变化，只在必要时更新缓存。
        支持发言里程碑检测：当用户发言达到里程碑次数时自动推送排行榜。
        
        Args:
            group_id (str): 验证后的群组ID
            user_id (str): 验证后的用户ID
            nickname (str): 验证后的用户昵称
        """
        # 直接使用data_manager更新用户消息，同时获取更新后的总发言数
        success, message_count = await self.data_manager.update_user_message(group_id, user_id, nickname)
        
        if success:
            # 智能缓存管理：检查昵称变化
            cached_nickname = self.member_cache.get_nickname_from_cache(user_id)
            
            # 只在昵称变化时才更新缓存（节省API调用）
            if cached_nickname != nickname:
                self.member_cache.update_nickname_cache(user_id, nickname)
                
                if self.plugin_config.detailed_logging_enabled:
                    self.logger.debug(f"昵称发生变化，更新缓存: {cached_nickname} -> {nickname}")
                    self.logger.info(f"记录消息统计: {nickname}")
            else:
                # 昵称未变化，只记录基本日志
                if self.plugin_config.detailed_logging_enabled:
                    self.logger.debug(f"昵称未变化，保持缓存: {nickname}")
                    self.logger.info(f"记录消息统计: {nickname}")
            
            # 发言里程碑检测（使用update_user_message返回的message_count，无需额外查询）
            await self._check_milestone(group_id, user_id, nickname, message_count)
        else:
            self.logger.error(f"记录消息统计失败: {nickname}")
    
    async def _check_milestone(self, group_id: str, user_id: str, nickname: str, current_count: int):
        """检测用户发言是否达到里程碑，达到则自动推送个人成就卡片
        
        性能优化：仅在 milestone_enabled=True 且 current_count 在 milestone_targets 中时才执行后续操作。
        使用缓存防止同一里程碑重复推送。
        推送个人成就卡片而非整个排行榜，减少数据查询和渲染开销。
        
        Args:
            group_id (str): 群组ID
            user_id (str): 用户ID
            nickname (str): 用户昵称
            current_count (int): 用户当前总发言数（由 update_user_message 直接返回）
        """
        # 快速短路：里程碑功能未启用或目标列表为空，直接返回
        if not self.plugin_config.milestone_enabled or not self.plugin_config.milestone_targets:
            return
        
        # 快速检查：当前发言数是否在里程碑目标中（O(1) 集合查找）
        milestone_set = set(self.plugin_config.milestone_targets)
        if current_count not in milestone_set:
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
            
            # 计算用户的群内排名和群总发言数
            rank = 1
            group_total_messages = 0
            target_user_data = None
            
            for user_data_item in group_data:
                if not isinstance(user_data_item, UserData):
                    continue
                group_total_messages += user_data_item.message_count
                if user_data_item.message_count > current_count:
                    rank += 1
                if user_data_item.user_id == user_id:
                    target_user_data = user_data_item
            
            # 计算发言占比
            percentage = (current_count / group_total_messages * 100) if group_total_messages > 0 else 0
            
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
            group_info = GroupInfo(group_id=str(group_id))
            group_name = await self._get_group_name(None, group_id)
            group_info.group_name = group_name
            
            # 生成里程碑个人成就卡片
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
                group_info=group_info
            )
            
            if not image_path:
                self.logger.warning("里程碑推送：个人卡片生成失败")
                return
            
            # 构建消息并推送
            from astrbot.api.event import MessageChain
            message_chain = MessageChain()
            message_chain = message_chain.file_image(image_path)
            
            await self.context.send_message(unified_msg_origin, message_chain)
            self.logger.info(f"✅ 里程碑推送成功: {nickname} 发言 {current_count} 次")
            
            # 清理临时图片
            import aiofiles
            if await aiofiles.os.path.exists(image_path):
                try:
                    await aiofiles.os.unlink(image_path)
                except OSError as e:
                    self.logger.warning(f"清理里程碑图片失败: {e}")
                    
        except Exception as e:
            self.logger.error(f"里程碑推送失败: {e}", exc_info=True)
    
    # ========== 排行榜命令 ==========
    
    @filter.command("发言榜里程碑", alias={'发言里程碑'})
    async def show_my_milestone(self, event: AstrMessageEvent):
        """显示个人里程碑成就卡片，别名：发言里程碑"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        
        if not group_id or not user_id:
            yield event.plain_result("无法获取群组或用户信息,请在群聊中使用此命令！")
            return
            
        group_id, user_id = str(group_id), str(user_id)
        
        # 获取用户昵称
        nickname = await self._get_user_display_name(event, group_id, user_id)
        
        try:
            # 获取群组数据
            group_data = await self.data_manager.get_group_data(group_id)
            if not group_data:
                yield event.plain_result("该群暂无发言数据！")
                return
            
            # 计算用户的群内排名和群总发言数
            rank = 1
            group_total_messages = 0
            target_user_data = None
            
            for user_data_item in group_data:
                if not isinstance(user_data_item, UserData):
                    continue
                group_total_messages += user_data_item.message_count
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
            
            # 计算发言占比
            percentage = (current_count / group_total_messages * 100) if group_total_messages > 0 else 0
            
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
            
            # 创建群组信息
            group_info = GroupInfo(group_id=str(group_id))
            group_name = await self._get_group_name(event, group_id)
            group_info.group_name = group_name
            
            # 生成里程碑个人成就卡片
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
                group_info=group_info
            )
            
            if not image_path:
                yield event.plain_result("个人里程碑卡片生成失败！")
                return
            
            # 使用框架标准的 image_result 返回图片
            yield event.image_result(image_path)
            
            # 清理临时图片文件
            if await aiofiles.os.path.exists(image_path):
                try:
                    await aiofiles.os.unlink(image_path)
                except OSError as e:
                    self.logger.warning(f"清理里程碑临时图片失败: {image_path}, 错误: {e}")
                    
        except Exception as e:
            self.logger.error(f"里程碑获取失败: {e}", exc_info=True)
            yield event.plain_result(f"获取里程碑失败: {str(e)}")

    
    @filter.command("发言榜", alias={'水群榜', 'B话榜', '发言排行', '发言统计'})
    async def show_full_rank(self, event: AstrMessageEvent):
        """显示总排行榜，别名：水群榜/B话榜/发言排行/发言统计"""
        async for result in self._show_rank(event, RankType.TOTAL):
            yield result
    
    @filter.command("今日发言榜", alias={'今日水群榜', '今日发言排行', '今日B话榜'})
    async def show_daily_rank(self, event: AstrMessageEvent):
        """显示今日排行榜，别名：今日水群榜/今日发言排行/今日B话榜"""
        async for result in self._show_rank(event, RankType.DAILY):
            yield result
    
    @filter.command("本周发言榜", alias={'本周水群榜', '本周发言排行', '本周B话榜'})
    async def show_weekly_rank(self, event: AstrMessageEvent):
        """显示本周排行榜，别名：本周水群榜/本周发言排行/本周B话榜"""
        async for result in self._show_rank(event, RankType.WEEKLY):
            yield result
    
    @filter.command("本月发言榜", alias={'本月水群榜', '本月发言排行', '本月B话榜'})
    async def show_monthly_rank(self, event: AstrMessageEvent):
        """显示本月排行榜，别名：本月水群榜/本月发言排行/本月B话榜"""
        async for result in self._show_rank(event, RankType.MONTHLY):
            yield result
    
    @filter.command("本年发言榜", alias={'本年水群榜', '本年发言排行', '本年B话榜', '年榜'})
    async def show_yearly_rank(self, event: AstrMessageEvent):
        """显示本年排行榜，别名：本年水群榜/本年发言排行/本年B话榜/年榜"""
        async for result in self._show_rank(event, RankType.YEARLY):
            yield result
    
    @filter.command("去年发言榜", alias={'去年水群榜', '去年发言排行', '去年B话榜'})
    async def show_last_year_rank(self, event: AstrMessageEvent):
        """显示去年排行榜，别名：去年水群榜/去年发言排行/去年B话榜"""
        async for result in self._show_rank(event, RankType.LAST_YEAR):
            yield result
    
    # ========== 设置命令 ==========
    
    @filter.command("设置发言榜数量")
    async def set_rank_count(self, event: AstrMessageEvent):
        """设置排行榜显示人数"""
        try:
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
            
        except ValueError as e:
            self.logger.error(f"设置排行榜数量失败(参数错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except TypeError as e:
            self.logger.error(f"设置排行榜数量失败(类型错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except KeyError as e:
            self.logger.error(f"设置排行榜数量失败(数据格式错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except (IOError, OSError, FileNotFoundError) as e:
            self.logger.error(f"设置排行榜数量失败(文件操作错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except AttributeError as e:
            self.logger.error(f"设置排行榜数量失败(属性错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except RuntimeError as e:
            self.logger.error(f"设置排行榜数量失败(运行时错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except (ConnectionError, asyncio.TimeoutError, ImportError, PermissionError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"设置排行榜数量失败(网络或系统错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")

    @filter.command("设置发言榜图片")
    async def set_image_mode(self, event: AstrMessageEvent):
        """设置排行榜的显示模式（图片或文字）
        
        根据用户输入的参数设置排行榜的显示模式：
        - 1/true/开/on/yes: 设置为图片模式
        - 0/false/关/off/no: 设置为文字模式
        
        返回相应的设置成功提示信息。
        """
        try:
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
            
        except ValueError as e:
            self.logger.error(f"设置图片模式失败(参数错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except TypeError as e:
            self.logger.error(f"设置图片模式失败(类型错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except KeyError as e:
            self.logger.error(f"设置图片模式失败(数据格式错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except (IOError, OSError, FileNotFoundError) as e:
            self.logger.error(f"设置图片模式失败(文件操作错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except AttributeError as e:
            self.logger.error(f"设置图片模式失败(属性错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except RuntimeError as e:
            self.logger.error(f"设置图片模式失败(运行时错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
        except (ConnectionError, asyncio.TimeoutError, ImportError, PermissionError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"设置图片模式失败(网络或系统错误): {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
    
    @filter.command("清除发言榜单")
    async def clear_message_ranking(self, event: AstrMessageEvent):
        """清除发言榜单"""
        try:
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
    
    @filter.command("刷新发言榜群成员缓存")
    async def refresh_group_members_cache(self, event: AstrMessageEvent):
        """刷新群成员列表缓存"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            group_id = str(group_id)
            
            # 使用 MemberCacheManager 刷新缓存
            success = await self.member_cache.refresh_group_cache(event, group_id)
            
            if success:
                yield event.plain_result("群成员缓存、字典缓存和昵称缓存已全部刷新！")
            else:
                yield event.plain_result("刷新缓存失败,请稍后重试！")
            
        except AttributeError as e:
            self.logger.error(f"刷新群成员缓存失败(属性错误): {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
        except KeyError as e:
            self.logger.error(f"刷新群成员缓存失败(数据格式错误): {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
        except TypeError as e:
            self.logger.error(f"刷新群成员缓存失败(类型错误): {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
        except (IOError, OSError) as e:
            self.logger.error(f"刷新群成员缓存失败(系统错误): {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
        except RuntimeError as e:
            self.logger.error(f"刷新群成员缓存失败(运行时错误): {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
        except (ConnectionError, asyncio.TimeoutError, ImportError, PermissionError) as e:
            self.logger.error(f"刷新群成员缓存失败(网络或系统错误): {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
    
    @filter.command("发言榜缓存状态")
    async def show_cache_status(self, event: AstrMessageEvent):
        """显示缓存状态"""
        try:
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
                f"🏷️ 昵称缓存: {member_cache_stats['nickname_cache_size']}/{member_cache_stats['nickname_cache_maxsize']}",
                "━━━━━━━━━━━━━━",
                "🕐 数据缓存TTL: 5分钟",
                "🕐 配置缓存TTL: 1分钟", 
                "🕐 群成员缓存TTL: 5分钟",
                "🕐 昵称缓存TTL: 10分钟"
            ]
            
            yield event.plain_result('\n'.join(status_msg))
            
        except ValueError as e:
            self.logger.error(f"显示缓存状态失败(参数错误): {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
        except TypeError as e:
            self.logger.error(f"显示缓存状态失败(类型错误): {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
        except KeyError as e:
            self.logger.error(f"显示缓存状态失败(数据格式错误): {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
        except (IOError, OSError) as e:
            self.logger.error(f"显示缓存状态失败(系统错误): {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
        except AttributeError as e:
            self.logger.error(f"显示缓存状态失败(属性错误): {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
        except RuntimeError as e:
            self.logger.error(f"显示缓存状态失败(运行时错误): {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
        except (ConnectionError, asyncio.TimeoutError, ImportError, PermissionError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"显示缓存状态失败(网络或系统错误): {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
    
    # ========== 私有方法 ==========
    
    async def _get_user_display_name(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """获取用户的群昵称（委托给 MemberCacheManager）"""
        # 使用 MemberCacheManager 的统一入口获取昵称
        nickname = await self.member_cache.get_user_display_name(event, group_id, user_id)
        
        # 如果统一逻辑返回默认昵称，使用备用方案
        if nickname == f"用户{user_id}":
            return await self.member_cache.get_fallback_nickname(event, user_id)
        
        return nickname
    
    @data_operation_handler('extract', '群成员昵称数据')
    def _get_display_name_from_member(self, member: Dict[str, Any]) -> Optional[str]:
        """从群成员信息中提取显示昵称（委托给 MemberCacheManager）"""
        return self.member_cache.get_display_name_from_member(member)

    async def _get_user_nickname_unified(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """统一的用户昵称获取方法（委托给 MemberCacheManager）"""
        return await self.member_cache.get_user_display_name(event, group_id, user_id)
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    async def _get_from_nickname_cache(self, user_id: str) -> Optional[str]:
        """从昵称缓存获取昵称（委托给 MemberCacheManager）"""
        return self.member_cache.get_nickname_from_cache(user_id)
    
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

    @exception_handler(ExceptionConfig(log_exception=True, reraise=False))
    def clear_user_cache(self, user_id: str = None):
        """清理用户缓存（委托给 MemberCacheManager）"""
        self.member_cache.clear_user_cache(user_id)
    
    def _is_blocked_user(self, user_id: str) -> bool:
        """检查用户是否在屏蔽列表中
        
        Args:
            user_id (str): 用户ID
            
        Returns:
            bool: 如果用户在屏蔽列表中返回True，否则返回False
        """
        if not hasattr(self, 'plugin_config') or not self.plugin_config:
            return False
        
        blocked_users = getattr(self.plugin_config, 'blocked_users', [])
        if not blocked_users:
            return False
        
        # 将用户ID转换为字符串进行比较
        user_id_str = str(user_id)
        
        # 检查是否在屏蔽列表中
        return user_id_str in [str(uid) for uid in blocked_users]
    
    def _is_blocked_group(self, group_id: str) -> bool:
        """检查群聊是否在屏蔽列表中
        
        Args:
            group_id (str): 群聊ID
            
        Returns:
            bool: 如果群聊在屏蔽列表中返回True，否则返回False
        """
        if not hasattr(self, 'plugin_config') or not self.plugin_config:
            return False
        
        blocked_groups = getattr(self.plugin_config, 'blocked_groups', [])
        if not blocked_groups:
            return False
        
        # 将群聊ID转换为字符串进行比较
        group_id_str = str(group_id)
        
        # 检查是否在屏蔽列表中
        return group_id_str in [str(gid) for gid in blocked_groups]
    
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
        try:
            # 首先尝试通过事件对象获取群组信息（仅在 event 不为 None 时）
            if event is not None:
                group_data = await event.get_group(group_id)
                if group_data:
                    # 简化群名获取逻辑，直接尝试常用属性
                    return getattr(group_data, 'group_name', None) or \
                           getattr(group_data, 'name', None) or \
                           getattr(group_data, 'title', None) or \
                           getattr(group_data, 'group_title', None) or \
                           f"群{group_id}"
            
            # 如果事件对象获取失败或 event 为 None，使用 PlatformHelper 统一通过API获取（跨平台通用）
            helper = PlatformHelper(event, self.context)
            group_name = await helper.get_group_name(group_id)
            if group_name:
                return str(group_name).strip()
            
            return f"群{group_id}"
        except (AttributeError, KeyError, TypeError, OSError) as e:
            self.logger.warning(f"获取群名称失败，使用默认名称: {e}")
            return f"群{group_id}"
    
    async def _show_rank(self, event: AstrMessageEvent, rank_type: RankType):
        """显示排行榜 - 重构版本"""
        try:
            # 检查群聊是否在屏蔽列表中
            group_id = event.get_group_id()
            if group_id and self._is_blocked_group(str(group_id)):
                return
            
            # 准备数据
            rank_data = await self._prepare_rank_data(event, rank_type)
            if rank_data is None:
                yield event.plain_result("无法获取排行榜数据,请检查群组信息或稍后重试")
                return
            
            group_id, current_user_id, filtered_data, config, title, group_info = rank_data
            
            # 如果启用了手动LLM分析，调用LLM生成头衔
            token_usage_info = None
            titles_map = None
            need_llm = config.llm_enabled and config.llm_enable_on_manual
            if need_llm:
                try:
                    # 获取群组数据用于LLM分析
                    group_data = await self.data_manager.get_group_data(group_id)
                    if group_data:
                        provider_id = getattr(config, 'llm_provider_id', '')
                        system_prompt = getattr(config, 'llm_system_prompt', '')
                        max_retries = getattr(config, 'llm_max_retries', 2)
                        min_daily = getattr(config, 'llm_min_daily_messages', 0)
                        
                        llm_analyzer = LLMAnalyzer(
                            context=self.context,
                            provider_id=provider_id,
                            system_prompt=system_prompt,
                            max_retries=max_retries
                        )
                        
                        grp_name = group_info.group_name or f"群{group_id}"
                        
                        # 只分析排行榜上实际显示的用户（与 config.rand 绑定）
                        # 未上榜的用户生成的头衔不会显示，分析纯属浪费 Token
                        ranked_users_for_llm = [user for user, _ in filtered_data[:config.rand]]
                        titles, token_usage = await llm_analyzer.analyze_users(
                            ranked_users_for_llm, grp_name, min_daily_messages=min_daily
                        )
                        
                        if token_usage and token_usage.get("total_tokens", 0) > 0:
                            token_usage_info = token_usage
                        
                        if titles:
                            self.logger.info(f"✅ 手动LLM头衔生成成功: 为 {len(titles)} 个用户生成了头衔")
                            # 保存头衔映射，同时设置到 user.display_title
                            titles_map = titles
                            for user_data_item, _ in filtered_data:
                                if user_data_item.user_id in titles:
                                    info = titles[user_data_item.user_id]
                                    if isinstance(info, dict):
                                        user_data_item.display_title = info.get("title")
                                        user_data_item.display_title_color = info.get("color")
                                    else:
                                        user_data_item.display_title = info

                except Exception as e:
                    self.logger.error(f"❌ 手动LLM头衔生成异常: {e}", exc_info=True)
            
            # 根据配置选择显示模式
            if config.if_send_pic:
                async for result in self._render_rank_as_image(event, filtered_data, group_info, title, current_user_id, config, token_usage_info, titles_map):

                    yield result
            else:
                async for result in self._render_rank_as_text(event, filtered_data, group_info, title, config):
                    yield result
        
        except (IOError, OSError) as e:
            self.logger.error(f"文件操作失败: {e}")
            yield event.plain_result("文件操作失败,请检查权限")
        except (AttributeError, KeyError, TypeError) as e:
            self.logger.error(f"数据格式错误: {e}")
            yield event.plain_result("数据格式错误,请联系管理员")
        except (ConnectionError, TimeoutError) as e:
            self.logger.error(f"网络请求失败: {e}")
            yield event.plain_result("网络请求失败,请稍后重试")
        except ImportError as e:
            self.logger.error(f"导入错误: {e}")
            yield event.plain_result("系统错误,请联系管理员")
        except RuntimeError as e:
            self.logger.error(f"运行时错误: {e}")
            yield event.plain_result("系统错误,请联系管理员")
        except ValueError as e:
            self.logger.error(f"数据格式错误: {e}")
            yield event.plain_result("数据格式错误,请联系管理员")
    
    async def _prepare_rank_data(self, event: AstrMessageEvent, rank_type: RankType):
        """准备排行榜数据"""
        # 获取群组ID和用户ID
        group_id = event.get_group_id()
        current_user_id = event.get_sender_id()
        
        if not group_id:
            return None
            
        if not current_user_id:
            return None
        
        group_id = str(group_id)
        current_user_id = str(current_user_id)
        
        # 生成排行榜时缓存群组名称（供 Web 面板使用）
        await self._cache_group_name(event, group_id)
        
        # 获取群组数据
        group_data = await self.data_manager.get_group_data(group_id)
        
        if not group_data:
            return None
        
        # 显示排行榜前强制刷新昵称缓存，确保昵称准确性
        await self._refresh_nickname_cache_for_ranking(event, group_id, group_data)
        
        # 根据类型筛选数据并获取排序值
        filtered_data_with_values = await self._filter_data_by_rank_type(group_data, rank_type)
        
        if not filtered_data_with_values:
            return None
        
        # 对数据进行排序
        filtered_data = sorted(filtered_data_with_values, key=lambda x: x[1], reverse=True)
        
        # 获取配置
        config = self.plugin_config
        
        # 生成标题
        title = self._generate_title(rank_type)
        
        # 创建群组信息
        group_info = GroupInfo(group_id=group_id)
        
        # 获取群名称
        group_name = await self._get_group_name(event, group_id)
        group_info.group_name = group_name
        
        return group_id, current_user_id, filtered_data, config, title, group_info
    
    async def _refresh_nickname_cache_for_ranking(self, event: AstrMessageEvent, group_id: str, group_data):
        """排行榜显示前强制刷新昵称缓存，确保显示最新昵称"""
        try:
            # 获取最新群成员信息
            members_info = await self._fetch_group_members_from_api(event, group_id)
            if not members_info:
                return
            
            # 重建群成员字典缓存（使用 PlatformHelper 跨平台通用方式获取用户ID）
            dict_cache_key = f"group_members_dict_{group_id}"
            members_dict = {}
            for m in members_info:
                uid = PlatformHelper.get_user_id_from_member(m)
                if uid:
                    members_dict[uid] = m
            self.member_cache.group_members_dict_cache[dict_cache_key] = members_dict
            
            # 更新用户数据中的昵称
            updated_count = 0
            for user in group_data:
                user_id = user.user_id
                if user_id in members_dict:
                    member = members_dict[user_id]
                    display_name = self.member_cache.get_display_name_from_member(member)
                    if display_name and user.nickname != display_name:
                        # 更新昵称并同步到昵称缓存
                        old_nickname = user.nickname
                        user.nickname = display_name
                        updated_count += 1
                        
                        # 同时更新昵称缓存
                        self.member_cache.update_nickname_cache(user_id, display_name)
                        
                        if self.plugin_config.detailed_logging_enabled:
                            self.logger.debug(f"排行榜刷新昵称缓存: {old_nickname} → {display_name}")
            
            # 保存更新后的数据
            if updated_count > 0:
                await self.data_manager.save_group_data(group_id, group_data)
                if self.plugin_config.detailed_logging_enabled:
                    self.logger.info(f"排行榜显示前更新了 {updated_count} 个用户的昵称缓存")
            
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, IOError, OSError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.warning(f"排行榜前刷新昵称缓存失败: {e}")

    async def _render_rank_as_image(self, event: AstrMessageEvent, filtered_data: List[tuple], 
                                  group_info: GroupInfo, title: str, current_user_id: str, config: PluginConfig,
                                  llm_token_usage: Dict[str, int] = None,
                                  titles_map: Optional[Dict[str, str]] = None):
        """渲染排行榜为图片模式"""
        temp_path = None
        try:
            # 提取用户数据用于图片生成，并应用人数限制
            # 先限制数量，再提取用户数据
            limited_data = filtered_data[:config.rand]
            users_for_image = []
            
            # 为用户数据设置display_total属性，确保图片生成器使用正确的数据
            # 修复：直接命令版排行榜图片显示错误数据的问题
            for user_data, count in limited_data:
                # 设置display_total属性（时间段内的发言数）
                user_data.display_total = count
                users_for_image.append(user_data)
            
            # 使用图片生成器（传入titles_map）
            temp_path = await self.image_generator.generate_rank_image(
                users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map
            )

            
            # 检查图片文件是否存在
            if await aiofiles.os.path.exists(temp_path):
                yield event.image_result(str(temp_path))
            else:
                # 回退到文字模式
                text_msg = self._generate_text_message(filtered_data, group_info, title, config)
                yield event.plain_result(text_msg)
                
        except (IOError, OSError, FileNotFoundError) as e:
            self.logger.error(f"生成图片失败: {e}")
            # 回退到文字模式
            text_msg = self._generate_text_message(filtered_data, group_info, title, config)
            yield event.plain_result(text_msg)
        except ImportError as e:
            self.logger.error(f"图片渲染失败(导入错误): {e}")
            # 回退到文字模式
            text_msg = self._generate_text_message(filtered_data, group_info, title, config)
            yield event.plain_result(text_msg)
        except RuntimeError as e:
            self.logger.error(f"图片渲染失败(运行时错误): {e}")
            # 回退到文字模式
            text_msg = self._generate_text_message(filtered_data, group_info, title, config)
            yield event.plain_result(text_msg)
        except ValueError as e:
            self.logger.error(f"图片渲染失败(数据格式错误): {e}")
            # 回退到文字模式
            text_msg = self._generate_text_message(filtered_data, group_info, title, config)
            yield event.plain_result(text_msg)
        finally:
            # 清理临时文件，避免资源泄漏
            if temp_path and await aiofiles.os.path.exists(temp_path):
                try:
                    await aiofiles.os.unlink(temp_path)
                except OSError as e:
                    self.logger.warning(f"清理临时图片文件失败: {temp_path}, 错误: {e}")
    
    async def _render_rank_as_text(self, event: AstrMessageEvent, filtered_data: List[tuple], 
                                 group_info: GroupInfo, title: str, config: PluginConfig):
        """渲染排行榜为文字模式"""
        text_msg = self._generate_text_message(filtered_data, group_info, title, config)
        yield event.plain_result(text_msg)
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _get_time_period_for_rank_type(self, rank_type: RankType) -> tuple:
        """获取排行榜类型对应的时间段
        
        Args:
            rank_type (RankType): 排行榜类型
            
        Returns:
            tuple: (start_date, end_date, period_name)，如果不需要时间段过滤则返回(None, None, None)
        """
        current_date = datetime.now().date()
        
        if rank_type == RankType.TOTAL:
            return None, None, "total"
        elif rank_type == RankType.DAILY:
            return current_date, current_date, "daily"
        elif rank_type == RankType.WEEKLY:
            # 获取本周开始日期(周一)
            days_since_monday = current_date.weekday()
            week_start = current_date - timedelta(days=days_since_monday)
            return week_start, current_date, "weekly"
        elif rank_type == RankType.MONTHLY:
            # 获取本月开始日期
            month_start = current_date.replace(day=1)
            return month_start, current_date, "monthly"
        elif rank_type == RankType.YEARLY:
            # 获取本年开始日期
            year_start = current_date.replace(month=1, day=1)
            return year_start, current_date, "yearly"
        elif rank_type == RankType.LAST_YEAR:
            # 获取去年的时间范围（1月1日 - 12月31日）
            last_year = current_date.year - 1
            year_start = date(last_year, 1, 1)
            year_end = date(last_year, 12, 31)
            return year_start, year_end, "lastyear"
        else:
            return None, None, "unknown"
    
    async def _filter_data_by_rank_type(self, group_data: List[UserData], rank_type: RankType) -> List[tuple]:
        """根据排行榜类型筛选数据并计算时间段内的发言次数 - 性能优化版本"""
        start_date, end_date, period_name = self._get_time_period_for_rank_type(rank_type)
        
        if rank_type == RankType.TOTAL:
            # 总榜：返回每个用户及其总发言数的元组，但过滤掉从未发言的用户和屏蔽用户
            return [(user, user.message_count) for user in group_data 
                   if user.message_count > 0 and not self._is_blocked_user(user.user_id)]
        
        # 时间段过滤：优化版本，使用预聚合策略减少双重循环
        # 策略：如果时间段较短（日榜），直接计算；如果时间段较长（周榜/月榜），使用缓存
        
        # 对于日榜，直接计算（因为时间段短，性能影响小）
        if rank_type == RankType.DAILY:
            return self._calculate_daily_rank(group_data, start_date, end_date)
        
        # 对于周榜和月榜，使用优化策略（现在是异步方法）
        elif rank_type in [RankType.WEEKLY, RankType.MONTHLY, RankType.YEARLY, RankType.LAST_YEAR]:
            return await self._calculate_period_rank_optimized(group_data, start_date, end_date)
        
        return []
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _calculate_daily_rank(self, group_data: List[UserData], start_date, end_date) -> List[tuple]:
        """计算日榜（直接计算策略）"""
        filtered_users = []
        for user in group_data:
            # 过滤屏蔽用户
            if self._is_blocked_user(user.user_id):
                continue
            
            # 检查是否有发言记录（兼容新旧两种存储格式）
            if not user._message_dates and not user.history:
                continue
            
            # 计算指定时间段的发言次数
            period_count = user.get_message_count_in_period(start_date, end_date)
            if period_count > 0:
                filtered_users.append((user, period_count))
        
        return filtered_users
    
    async def _calculate_period_rank_optimized(self, group_data: List[UserData], start_date, end_date) -> List[tuple]:
        """计算周榜/月榜（优化策略）"""
        # 优化策略：先筛选出有发言记录的用户，然后批量计算
        # 兼容新旧两种存储格式（_message_dates 或 history）
        active_users = [user for user in group_data if user._message_dates or user.history]
        
        if not active_users:
            return []
        
        # 批量计算，减少函数调用开销
        filtered_users = []
        for user in active_users:
            # 过滤屏蔽用户
            if self._is_blocked_user(user.user_id):
                continue
                
            # 使用 UserData.get_message_count_in_period 方法计算
            # 该方法内部使用 _message_dates 字典 O(1) 查询，比遍历 history 列表快得多
            # 并且有 _ensure_message_dates 兜底保护，兼容旧数据格式
            period_count = user.get_message_count_in_period(start_date, end_date)
            if period_count > 0:
                filtered_users.append((user, period_count))
        
        return filtered_users
    
    async def _count_messages_in_period_fast(self, history: List, start_date, end_date) -> int:
        """快速计算指定时间段内的消息数量（优化版本）
        
        如果历史记录未排序，将自动排序后进行计算。
        对于已排序的记录，使用高效的早停算法。
        """
        # 如果历史记录为空，直接返回0
        if not history:
            return 0
        
        # 完整遍历检查列表是否真正有序，避免采样检查的误判问题
        is_sorted = True
        if len(history) > 1:
            try:
                # 完整遍历检查：确保列表真正有序（优化版本）
                for current_item, next_item in zip(history[:-1], history[1:]):
                    current_date = current_item.to_date() if hasattr(current_item, 'to_date') else current_item
                    next_date = next_item.to_date() if hasattr(next_item, 'to_date') else next_item
                    if current_date > next_date:
                        is_sorted = False
                        break
                        
            except (AttributeError, TypeError):
                # 如果无法比较，假设未排序
                is_sorted = False
        
        # 如果检测到列表确实有序，使用早停算法
        if is_sorted:
            count = 0
            for hist_date in history:
                # 转换为日期对象
                hist_date_obj = hist_date.to_date() if hasattr(hist_date, 'to_date') else hist_date
                
                # 检查是否在指定时间段内
                if hist_date_obj < start_date:
                    continue
                if hist_date_obj > end_date:
                    # 已排序，可以提前跳出循环
                    break
                count += 1
            
            return count
        
        # 如果检测到列表无序，直接使用无序版本计算
        else:
            return self._count_messages_in_period_unordered(history, start_date, end_date)
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _count_messages_in_period_unordered(self, history: List, start_date, end_date) -> int:
        """计算指定时间段内的消息数量（适用于未排序的历史记录）"""
        if not history:
            return 0
        
        count = 0
        for hist_date in history:
            hist_date_obj = hist_date.to_date() if hasattr(hist_date, 'to_date') else hist_date
            if start_date <= hist_date_obj <= end_date:
                count += 1
        
        return count
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _generate_title(self, rank_type: RankType) -> str:
        """生成标题"""
        now = datetime.now()
        
        if rank_type == RankType.TOTAL:
            return "总发言排行榜"
        elif rank_type == RankType.DAILY:
            return f"今日[{now.year}年{now.month}月{now.day}日]发言榜单"
        elif rank_type == RankType.WEEKLY:
            # 计算周数
            week_num = now.isocalendar().week
            return f"本周[{now.year}年{now.month}月第{week_num}周]发言榜单"
        elif rank_type == RankType.MONTHLY:
            return f"本月[{now.year}年{now.month}月]发言榜单"
        elif rank_type == RankType.YEARLY:
            return f"本年[{now.year}年]发言榜单"
        elif rank_type == RankType.LAST_YEAR:
            last_year = now.year - 1
            return f"去年[{last_year}年]发言榜单"
        else:
            return "发言榜单"
    
    def _generate_text_message(self, users_with_values: List[tuple], group_info: GroupInfo, title: str, config: PluginConfig) -> str:
        """生成文字消息
        
        Args:
            users_with_values: 包含(UserData, sort_value)元组的列表
            group_info: 群组信息
            title: 排行榜标题
            config: 插件配置
            
        Returns:
            str: 格式化的文字消息
        """
        # 计算时间段内的总发言数
        total_messages = sum(sort_value for _, sort_value in users_with_values)
        
        # 数据已经在_show_rank中排好序，直接使用并限制数量
        top_users = users_with_values[:config.rand]
        
        msg = [f"{title}\n发言总数: {total_messages}\n━━━━━━━━━━━━━━\n"]
        
        for i, (user, user_messages) in enumerate(top_users):
            # 使用时间段内的发言数计算百分比
            percentage = ((user_messages / total_messages) * 100) if total_messages > 0 else 0
            msg.append(f"第{i + 1}名:{user.nickname}·{user_messages}次(占比{percentage:.2f}%)\n")
        
        return ''.join(msg)
    
    # ========== 定时功能管理命令 ==========
    
    @filter.command("发言榜定时状态")
    async def timer_status(self, event: AstrMessageEvent):
        """查看定时任务状态"""
        try:
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
            
        except (IOError, OSError, KeyError) as e:
            self.logger.error(f"获取定时状态失败: {e}")
            yield event.plain_result("获取定时状态失败，请稍后重试！")
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"获取定时状态失败(运行时错误): {e}")
            yield event.plain_result("获取定时状态失败，请稍后重试！")
    
    @filter.command("手动推送发言榜")
    async def manual_push(self, event: AstrMessageEvent):
        """手动推送排行榜"""
        try:
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
                yield event.plain_result("❌ 手动推送执行失败！\n\n💡 可能的原因：\n• 缺少 unified_msg_origin\n• 群组权限不足\n\n🔧 解决方案：\n• 在群组中发送任意消息以收集 unified_msg_origin\n• 检查机器人是否有群组发言权限")
            
        except (AttributeError, TypeError) as e:
            self.logger.error(f"处理手动推送请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
        except (RuntimeError, ValueError, KeyError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"处理手动推送请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("设置发言榜定时时间")
    async def set_timer_time(self, event: AstrMessageEvent):
        """设置定时推送时间
        
        自动设置当前群组为定时群组并启用定时功能
        """
        try:
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
            
            # 更新定时任务
            rank_type_text = self._get_rank_type_text(config.timer_rank_type)
            if self.timer_manager:
                success = await self.timer_manager.update_config(config, self.group_unified_msg_origins)
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
            
        except ValueError as e:
            self.logger.error(f"处理设置定时时间请求失败: {e}")
            yield event.plain_result("时间格式错误，请使用 HH:MM 格式！")
        except (IOError, OSError) as e:
            self.logger.error(f"处理设置定时时间请求失败: {e}")
            yield event.plain_result("保存配置失败，请稍后重试！")
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"处理设置定时时间请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("设置发言榜定时群组")
    async def set_timer_groups(self, event: AstrMessageEvent):
        """设置定时推送目标群组"""
        try:
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
            
            # 更新定时任务
            if self.timer_manager and config.timer_enabled:
                await self.timer_manager.update_config(config, self.group_unified_msg_origins)
            
            groups_text = "\n".join([f"   • {group_id}" for group_id in valid_groups])
            yield event.plain_result(f"✅ 定时推送目标群组已设置：\n{groups_text}")
            
        except ValueError as e:
            self.logger.error(f"处理设置定时群组请求失败: {e}")
            yield event.plain_result("群组ID格式错误，请输入有效的群组ID！")
        except (IOError, OSError) as e:
            self.logger.error(f"处理设置定时群组请求失败: {e}")
            yield event.plain_result("保存配置失败，请稍后重试！")
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"处理设置定时群组请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("删除发言榜定时群组")
    async def remove_timer_groups(self, event: AstrMessageEvent):
        """删除定时推送目标群组"""
        try:
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            current_groups = config.timer_target_groups
            
            if not args:
                # 清空所有定时群组
                config.timer_target_groups = []
                
                # 更新定时任务
                if self.timer_manager and config.timer_enabled:
                    await self.timer_manager.update_config(config, self.group_unified_msg_origins)
                
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
            await self.data_manager.save_config(config)
            
            # 更新定时任务
            if self.timer_manager and config.timer_enabled:
                await self.timer_manager.update_config(config, self.group_unified_msg_origins)
            
            if groups_to_remove:
                removed_text = "\n".join([f"   • {group_id}" for group_id in groups_to_remove])
                remaining_text = "\n".join([f"   • {group_id}" for group_id in remaining_groups]) if remaining_groups else "   无"
                yield event.plain_result(f"✅ 已删除定时推送目标群组：\n{removed_text}\n\n📋 剩余群组：\n{remaining_text}")
            else:
                yield event.plain_result("⚠️ 未找到要删除的群组")
            
        except ValueError as e:
            self.logger.error(f"处理删除定时群组请求失败: {e}")
            yield event.plain_result("群组ID格式错误，请输入有效的群组ID！")
        except (IOError, OSError) as e:
            self.logger.error(f"处理删除定时群组请求失败: {e}")
            yield event.plain_result("保存配置失败，请稍后重试！")
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"处理删除定时群组请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("启用发言榜定时")
    async def enable_timer(self, event: AstrMessageEvent):
        """启用定时推送功能"""
        try:
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            
            # 检查配置
            if not config.timer_target_groups:
                yield event.plain_result("请先设置目标群组！用法:#设置定时群组 群组ID")
                return
            
            # 启用定时功能
            config.timer_enabled = True
            
            # 更新定时任务（使用update_config确保group_unified_msg_origins被正确传递）
            if self.timer_manager:
                # 检查TimerManager是否有有效的context
                if not hasattr(self.timer_manager, 'context') or not self.timer_manager.context:
                    yield event.plain_result("⚠️ 定时管理器未完全初始化！\n\n💡 可能的原因：\n• 插件初始化过程中出现异常\n• 上下文信息缺失\n\n🔧 解决方案：\n• 重启机器人或重新加载插件\n• 检查插件配置是否正确")
                    return
                
                success = await self.timer_manager.update_config(config, self.group_unified_msg_origins)
                if success:
                    yield event.plain_result("✅ 定时推送功能已启用！")
                else:
                    yield event.plain_result("⚠️ 定时推送功能启用失败，请检查配置！")
            else:
                yield event.plain_result("⚠️ 定时管理器未初始化！")
            
        except (IOError, OSError) as e:
            self.logger.error(f"处理启用定时请求失败: {e}")
            yield event.plain_result("保存配置失败，请稍后重试！")
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"处理启用定时请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("禁用发言榜定时")
    async def disable_timer(self, event: AstrMessageEvent):
        """禁用定时推送功能"""
        try:
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            
            # 禁用定时功能
            config.timer_enabled = False
            
            # 停止定时任务
            if self.timer_manager:
                await self.timer_manager.stop_timer()
            
            yield event.plain_result("✅ 定时推送功能已禁用！")
            
        except (IOError, OSError) as e:
            self.logger.error(f"处理禁用定时请求失败: {e}")
            yield event.plain_result("保存配置失败，请稍后重试！")
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"处理禁用定时请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("设置发言榜定时类型")
    async def set_timer_type(self, event: AstrMessageEvent):
        """设置定时推送的排行榜类型"""
        try:
            # 获取参数
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            
            if not args:
                yield event.plain_result("请指定排行榜类型！用法:#设置定时类型 total/daily/week/month")
                return
            
            rank_type = args[0].lower()
            
            # 验证排行榜类型
            valid_types = ['total', 'daily', 'week', 'weekly', 'month', 'monthly']
            if rank_type not in valid_types:
                yield event.plain_result(f"排行榜类型错误！可用类型: {', '.join(valid_types)}")
                return
            
            # 获取当前配置（使用转换后的配置）
            config = self.plugin_config
            config.timer_rank_type = rank_type
            
            # 更新定时任务
            if self.timer_manager and config.timer_enabled:
                await self.timer_manager.update_config(config, self.group_unified_msg_origins)
            
            type_text = self._get_rank_type_text(rank_type)
            yield event.plain_result(f"✅ 定时推送排行榜类型已设置为 {type_text}！")
            
        except ValueError as e:
            self.logger.error(f"处理设置定时类型请求失败: {e}")
            yield event.plain_result("排行榜类型错误，请使用：total/daily/weekly/monthly")
        except (IOError, OSError) as e:
            self.logger.error(f"处理设置定时类型请求失败: {e}")
            yield event.plain_result("保存配置失败，请稍后重试！")
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            # 修复：替换过于宽泛的Exception为具体异常类型
            self.logger.error(f"处理设置定时类型请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    # ========== 辅助方法 ==========
    
    def _handle_command_exception(self, event: AstrMessageEvent, operation_name: str, exception: Exception) -> None:
        """公共的异常处理方法

        注意：此方法是同步方法。由于 event.plain_result() 在 async generator 上下文中
        需要通过 yield 来发送消息，此方法只能记录日志并返回错误字符串供上层 yield。
        直接调用 event.plain_result() 无法发送消息到聊天中。

        Args:
            event: 消息事件对象
            operation_name: 操作名称，用于日志记录
            exception: 异常对象

        Returns:
            None: 仅记录日志，消息通过返回的字符串由上层 yield 发送
        """
        if isinstance(exception, (KeyError, TypeError)):
            self.logger.error(f"{operation_name}失败(数据格式错误): {exception}", exc_info=True)
        elif isinstance(exception, (IOError, OSError, FileNotFoundError)):
            self.logger.error(f"{operation_name}失败(文件操作错误): {exception}", exc_info=True)
        elif isinstance(exception, ValueError):
            self.logger.error(f"{operation_name}失败(参数错误): {exception}", exc_info=True)
        elif isinstance(exception, RuntimeError):
            self.logger.error(f"{operation_name}失败(运行时错误): {exception}", exc_info=True)
        elif isinstance(exception, (ConnectionError, asyncio.TimeoutError, ImportError, PermissionError)):
            self.logger.error(f"{operation_name}失败(网络或系统错误): {exception}", exc_info=True)
        else:
            self.logger.error(f"{operation_name}失败(未预期的错误类型 {type(exception).__name__}): {exception}", exc_info=True)
    
    def _log_operation_result(self, operation_name: str, success: bool, details: str = ""):
        """公共的操作结果日志记录方法，减少代码重复
        
        Args:
            operation_name: 操作名称
            success: 是否成功
            details: 详细信息
        """
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
            'daily': '今日排行榜', 
            'week': '本周排行榜',
            'weekly': '本周排行榜',
            'month': '本月排行榜',
            'monthly': '本月排行榜'
        }
        return type_mapping.get(rank_type, rank_type)