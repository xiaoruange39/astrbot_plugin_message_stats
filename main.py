"""
AstrBot 群发言统计插件
统计群成员发言次数,生成排行榜
"""

# 标准库导入
import asyncio
import json
import os
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict, Any

# 第三方库导入
from cachetools import TTLCache

# AstrBot框架导入
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger as astrbot_logger
import astrbot.api.message_components as Comp

# 本地模块导入
from .utils.data_manager import DataManager
from .utils.image_generator import ImageGenerator, ImageGenerationError
from .utils.validators import Validators, ValidationError
from .utils.models import (
    UserData, PluginConfig, GroupInfo, MessageDate, 
    RankType
)




@register("message_stats", "xiaoruange39", "群发言统计插件", "1.0")
class MessageStatsPlugin(Star):
    """群发言统计插件
    
    该插件用于统计群组成员的发言次数,并生成多种类型的排行榜.
    支持自动监听群消息、手动记录、总榜/日榜/周榜/月榜等功能.
    
    主要功能:
        - 自动监听和记录群成员发言统计
        - 支持多种排行榜类型(总榜、日榜、周榜、月榜)
        - 提供图片和文字两种显示模式
        - 完整的配置管理系统
        - 权限控制和安全管理
        - 群成员昵称智能获取
        - 高效的缓存机制
        
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
    
    def __init__(self, context: Context, config = None):
        """初始化插件实例
        
        Args:
            context (Context): AstrBot上下文对象,包含插件运行环境信息
            config (Optional[Any]): 插件配置对象,如果为None则使用默认配置
        """
        super().__init__(context)
        self.logger = astrbot_logger
        
        # 使用StarTools获取插件数据目录
        data_dir = StarTools.get_data_dir('message_stats')
        
        # 初始化组件
        self.data_manager = DataManager(data_dir)
        
        # 插件配置将在初始化时从DataManager获取
        self.plugin_config = None
        self.image_generator = None
        
        # 群成员列表缓存 - 5分钟TTL,减少API调用
        self.group_members_cache = TTLCache(maxsize=100, ttl=300)
        
        # 用户昵称缓存 - 缓存用户ID到昵称的映射，减少重复查找
        self.user_nickname_cache = TTLCache(maxsize=500, ttl=600)
        
        # 群成员字典缓存 - 缓存群成员ID到成员信息的映射
        self.group_members_dict_cache = TTLCache(maxsize=50, ttl=300)
        
        # 插件状态
        self.initialized = False
    
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
            
            # 初始化数据管理器
            await self.data_manager.initialize()
            
            # 从DataManager获取插件配置(确保config.json存在,如果不存在则创建默认配置)
            self.plugin_config = await self.data_manager.get_config()
            
            # 创建图片生成器
            self.image_generator = ImageGenerator(self.plugin_config)
            
            # 初始化图片生成器
            try:
                await self.image_generator.initialize()
                self.logger.info("图片生成器初始化成功")
            except ImageGenerationError as e:
                self.logger.warning(f"图片生成器初始化失败: {e}")
            
            self.initialized = True
            self.logger.info("群发言统计插件初始化完成")
            
        except (OSError, IOError) as e:
            self.logger.error(f"插件初始化失败: {e}")
            raise
    
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
            
            # 清理图片生成器
            if self.image_generator:
                await self.image_generator.cleanup()
            
            # 清理数据缓存
            await self.data_manager.clear_cache()
            
            # 清理群成员列表缓存
            self.group_members_cache.clear()
            self.logger.info("群成员列表缓存已清理")
            
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
        if message_str.startswith(('%', '/')):
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
            # 验证数据
            group_id = Validators.validate_group_id(group_id)
            user_id = Validators.validate_user_id(user_id)
            nickname = Validators.validate_nickname(nickname)
            
            # 直接使用data_manager更新用户消息
            success = await self.data_manager.update_user_message(group_id, user_id, nickname)
            
            if success:
                self.logger.info(f"记录消息统计: {nickname}")
            else:
                self.logger.error(f"记录消息统计失败: {nickname}")
            
        except (ValueError, TypeError, KeyError) as e:
            self.logger.error(f"记录消息统计失败: {e}")
    
    # ========== 排行榜命令 ==========
    
    @filter.command("更新发言统计")
    async def update_message_stats(self, event: AstrMessageEvent):
        """手动更新发言统计"""
        try:
            # 使用AstrBot官方API获取群组ID和用户ID
            group_id = event.get_group_id()
            user_id = event.get_sender_id()
            
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
                
            if not user_id:
                yield event.plain_result("无法获取用户信息！")
                return
            
            group_id = str(group_id)
            user_id = str(user_id)
            
            # 获取用户显示名称(优先使用群昵称)
            user_name = await self._get_user_display_name(event, group_id, user_id)
            
            # 记录当前用户的发言
            await self.data_manager.update_user_message(group_id, user_id, user_name)
            
            yield event.plain_result(f"已记录 {user_name} 的发言统计！")
            
        except (AttributeError, KeyError, TypeError) as e:
            self.logger.error(f"更新发言统计失败: {e}")
            yield event.plain_result("更新发言统计失败,请稍后重试")
    
    @filter.command("发言榜")
    async def show_full_rank(self, event: AstrMessageEvent):
        """显示总排行榜"""
        async for result in self._show_rank(event, RankType.TOTAL):
            yield result
    
    @filter.command("水群榜")
    async def show_water_group_rank(self, event: AstrMessageEvent):
        """显示水群排行榜(发言榜别名)"""
        async for result in self._show_rank(event, RankType.TOTAL):
            yield result
    
    @filter.command("B话榜")
    async def show_bhua_rank(self, event: AstrMessageEvent):
        """显示B话排行榜(发言榜别名)"""
        async for result in self._show_rank(event, RankType.TOTAL):
            yield result
    
    @filter.command("今日发言榜")
    async def show_daily_rank(self, event: AstrMessageEvent):
        """显示今日排行榜"""
        async for result in self._show_rank(event, RankType.DAILY):
            yield result
    
    @filter.command("本周发言榜")
    async def show_weekly_rank(self, event: AstrMessageEvent):
        """显示本周排行榜"""
        async for result in self._show_rank(event, RankType.WEEKLY):
            yield result
    
    @filter.command("本月发言榜")
    async def show_monthly_rank(self, event: AstrMessageEvent):
        """显示本月排行榜"""
        async for result in self._show_rank(event, RankType.MONTHLY):
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
                if count <= 0 or count > 100:
                    yield event.plain_result("数量必须在1-100之间！")
                    return
            except ValueError:
                yield event.plain_result("数量必须是数字！")
                return
            
            # 保存配置
            config = await self.data_manager.get_config()
            config.rand = count
            await self.data_manager.save_config(config)
            
            yield event.plain_result(f"排行榜显示人数已设置为 {count} 人！")
            
        except (ValueError, TypeError, KeyError) as e:
            self.logger.error(f"设置排行榜数量失败: {e}")
            yield event.plain_result("设置失败,请稍后重试")
    
    # 图片模式常量定义
    IMAGE_MODE_ENABLE_ALIASES = {'1', 'true', '开', 'on', 'yes'}
    IMAGE_MODE_DISABLE_ALIASES = {'0', 'false', '关', 'off', 'no'}
    
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
            config.send_pic = send_pic
            await self.data_manager.save_config(config)
            
            yield event.plain_result(f"排行榜显示模式已设置为 {mode_text}！")
            
        except (ValueError, TypeError, KeyError) as e:
            self.logger.error(f"设置图片模式失败: {e}")
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
    
    @filter.command("刷新群成员缓存")
    async def refresh_group_members_cache(self, event: AstrMessageEvent):
        """刷新群成员列表缓存"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            group_id = str(group_id)
            
            # 清除特定群的成员缓存
            cache_key = f"group_members_{group_id}"
            if cache_key in self.group_members_cache:
                del self.group_members_cache[cache_key]
                self.logger.info(f"刷新群 {group_id} 成员缓存")
                yield event.plain_result("群成员缓存已刷新！")
            else:
                yield event.plain_result("该群没有缓存的成员信息！")
            
        except (AttributeError, KeyError, TypeError) as e:
            self.logger.error(f"刷新群成员缓存失败: {e}")
            yield event.plain_result("刷新缓存失败,请稍后重试！")
    
    @filter.command("缓存状态")
    async def show_cache_status(self, event: AstrMessageEvent):
        """显示缓存状态"""
        try:
            # 获取数据管理器缓存统计
            cache_stats = await self.data_manager.get_cache_stats()
            
            # 获取群成员缓存信息
            members_cache_size = len(self.group_members_cache)
            members_cache_maxsize = self.group_members_cache.maxsize
            
            status_msg = [
                "📊 缓存状态报告",
                "━━━━━━━━━━━━━━",
                f"💾 数据缓存: {cache_stats['data_cache_size']}/{cache_stats['data_cache_maxsize']}",
                f"⚙️ 配置缓存: {cache_stats['config_cache_size']}/{cache_stats['config_cache_maxsize']}",
                f"👥 群成员缓存: {members_cache_size}/{members_cache_maxsize}",
                "━━━━━━━━━━━━━━",
                "🕐 数据缓存TTL: 5分钟",
                "🕐 配置缓存TTL: 1分钟", 
                "🕐 群成员缓存TTL: 5分钟"
            ]
            
            yield event.plain_result('\n'.join(status_msg))
            
        except (ValueError, TypeError, KeyError) as e:
            self.logger.error(f"显示缓存状态失败: {e}")
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
    
    # ========== 私有方法 ==========
    
    async def _get_user_display_name(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """获取用户的群昵称,优先使用群昵称,其次使用QQ昵称（重构版 - 跨平台兼容）"""
        # 优先使用统一的昵称获取逻辑
        nickname = await self._get_user_nickname_unified(event, group_id, user_id)
        
        # 如果统一逻辑失败，使用备用方案
        if nickname == f"用户{user_id}":
            return await self._get_fallback_nickname(event, user_id)
        
        return nickname
    
    def _get_display_name_from_member(self, member: Dict[str, Any]) -> Optional[str]:
        """从群成员信息中提取显示昵称
        
        提取用户昵称的辅助函数，避免重复的逻辑
        
        Args:
            member (Dict[str, Any]): 群成员信息字典
            
        Returns:
            Optional[str]: 用户的显示昵称，如果获取失败则返回None
        """
        return member.get("card") or member.get("nickname")

    async def _get_user_nickname_unified(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        """统一的用户昵称获取方法 - 简化版缓存查找逻辑
        
        按优先级检查缓存，提供清晰的查找流程：
        1. 检查昵称缓存
        2. 检查群成员字典缓存  
        3. 从API获取并缓存
        4. 返回默认昵称
        
        Args:
            event (AstrMessageEvent): 消息事件对象
            group_id (str): 群组ID
            user_id (str): 用户ID
            
        Returns:
            str: 用户的显示昵称，如果都失败则返回 "用户{user_id}"
        """
        nickname_cache_key = f"nickname_{user_id}"
        
        # 步骤1: 检查昵称缓存（最高优先级）
        if nickname_cache_key in self.user_nickname_cache:
            return self.user_nickname_cache[nickname_cache_key]
        
        # 步骤2: 检查群成员字典缓存
        dict_cache_key = f"group_members_dict_{group_id}"
        if dict_cache_key in self.group_members_dict_cache:
            members_dict = self.group_members_dict_cache[dict_cache_key]
            if user_id in members_dict:
                member = members_dict[user_id]
                display_name = self._get_display_name_from_member(member)
                if display_name:
                    self.user_nickname_cache[nickname_cache_key] = display_name
                    return display_name
        
        # 步骤3: 从API获取群成员信息
        try:
            members_info = await self._fetch_group_members_from_api(event, group_id)
            if members_info:
                # 重建字典缓存
                members_dict = {str(m.get("user_id", "")): m for m in members_info if m.get("user_id")}
                self.group_members_dict_cache[dict_cache_key] = members_dict
                
                # 查找用户
                if user_id in members_dict:
                    member = members_dict[user_id]
                    display_name = self._get_display_name_from_member(member)
                    if display_name:
                        self.user_nickname_cache[nickname_cache_key] = display_name
                        return display_name
        except (AttributeError, KeyError, TypeError) as e:
            self.logger.warning(f"获取群成员信息失败(数据格式错误): {e}")
        except (ConnectionError, TimeoutError, OSError) as e:
            self.logger.warning(f"获取群成员信息失败(网络错误): {e}")
        except (ImportError, RuntimeError) as e:
            self.logger.warning(f"获取群成员信息失败(系统错误): {e}")
        
        # 步骤4: 返回默认昵称
        return f"用户{user_id}"
    
    async def _get_fallback_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取备用昵称
        
        当无法从群成员列表获取昵称时的备用方案,使用事件对象中的发送者名称.
        
        Args:
            event (AstrMessageEvent): AstrBot消息事件对象
            user_id (str): 用户ID
            
        Returns:
            str: 用户的显示名称,如果获取失败则返回 "用户{user_id}" 格式
            
        Raises:
            AttributeError: 当事件对象缺少必要属性时抛出
            KeyError: 当数据格式错误时抛出
            TypeError: 当参数类型错误时抛出
            
        Example:
            >>> nickname = await self._get_fallback_nickname(event, "123456")
            >>> print(nickname)
            '用户123456'
        """
        try:
            nickname = event.get_sender_name()
            return nickname or f"用户{user_id}"
        except (AttributeError, KeyError, TypeError) as e:
            self.logger.error(f"获取备用昵称失败: {e}")
            return f"用户{user_id}"
    

    
    def clear_user_cache(self, user_id: str = None):
        """清理用户缓存"""
        if user_id:
            # 清理特定用户的缓存
            nickname_cache_key = f"nickname_{user_id}"
            if nickname_cache_key in self.user_nickname_cache:
                del self.user_nickname_cache[nickname_cache_key]
        else:
            # 清理所有用户缓存
            self.user_nickname_cache.clear()
        
        self.logger.info(f"清理用户缓存: {user_id or '全部'}")
    
    async def _get_group_members_cache(self, event: AstrMessageEvent, group_id: str) -> Optional[List[Dict[str, Any]]]:
        """获取群成员缓存"""
        cache_key = f"group_members_{group_id}"
        
        if cache_key in self.group_members_cache:
            return self.group_members_cache[cache_key]
        else:
            # 缓存未命中,从API获取
            return await self._fetch_group_members_from_api(event, group_id)
    
    async def _fetch_group_members_from_api(self, event: AstrMessageEvent, group_id: str) -> Optional[List[Dict[str, Any]]]:
        """从API获取群成员"""
        client = event.bot
        params = {"group_id": group_id}
        
        try:
            members_info = await client.api.call_action('get_group_member_list', **params)
            if members_info:
                # 缓存群成员列表,设置合理的过期时间
                cache_key = f"group_members_{group_id}"
                self.group_members_cache[cache_key] = members_info
                
                # 对于大群(成员数>500),记录警告
                if len(members_info) > 500:
                    self.logger.warning(f"群 {group_id} 成员数较多({len(members_info)}),建议调整缓存策略")
                
                return members_info
        except (AttributeError, KeyError, TypeError) as e:
            self.logger.warning(f"获取群成员列表失败(数据格式错误): {e}")
        except (ConnectionError, TimeoutError, OSError) as e:
            self.logger.warning(f"获取群成员列表失败(网络错误): {e}")
        except ImportError as e:
            self.logger.warning(f"获取群成员列表失败(导入错误): {e}")
        except RuntimeError as e:
            self.logger.warning(f"获取群成员列表失败(运行时错误): {e}")
        except ValueError as e:
            self.logger.warning(f"获取群成员列表失败(数据格式错误): {e}")
        
        return None
    

    
    async def _get_group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        """获取群名称 - 简化版本"""
        try:
            group_data = await event.get_group(group_id)
            if group_data:
                # 简化群名获取逻辑，直接尝试常用属性
                return getattr(group_data, 'group_name', None) or \
                       getattr(group_data, 'name', None) or \
                       getattr(group_data, 'title', None) or \
                       f"群{group_id}"
            return f"群{group_id}"
        except (AttributeError, KeyError, TypeError, OSError) as e:
            self.logger.warning(f"获取群名称失败，使用默认名称: {e}")
            return f"群{group_id}"
    
    async def _show_rank(self, event: AstrMessageEvent, rank_type: RankType):
        """显示排行榜 - 重构版本"""
        try:
            # 准备数据
            rank_data = await self._prepare_rank_data(event, rank_type)
            if rank_data is None:
                yield event.plain_result("无法获取排行榜数据,请检查群组信息或稍后重试")
                return
            
            group_id, current_user_id, filtered_data, config, title, group_info = rank_data
            
            # 根据配置选择显示模式
            if config.send_pic:
                async for result in self._render_rank_as_image(event, filtered_data, group_info, title, current_user_id, config):
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
        
        # 获取群组数据
        group_data = await self.data_manager.get_group_data(group_id)
        
        if not group_data:
            return None
        
        # 根据类型筛选数据并获取排序值
        filtered_data_with_values = await self._filter_data_by_rank_type(group_data, rank_type)
        
        if not filtered_data_with_values:
            return None
        
        # 对数据进行排序
        filtered_data = sorted(filtered_data_with_values, key=lambda x: x[1], reverse=True)
        
        # 获取配置
        config = await self.data_manager.get_config()
        
        # 生成标题
        title = self._generate_title(rank_type)
        
        # 创建群组信息
        group_info = GroupInfo(group_id=group_id)
        
        # 获取群名称
        group_name = await self._get_group_name(event, group_id)
        group_info.group_name = group_name
        
        return group_id, current_user_id, filtered_data, config, title, group_info
    
    async def _render_rank_as_image(self, event: AstrMessageEvent, filtered_data: List[tuple], 
                                  group_info: GroupInfo, title: str, current_user_id: str, config: PluginConfig):
        """渲染排行榜为图片模式"""
        temp_path = None
        try:
            # 提取用户数据用于图片生成，并应用人数限制
            # 先限制数量，再提取用户数据
            limited_data = filtered_data[:config.rand]
            users_for_image = [user_data for user_data, _ in limited_data]
            
            # 使用图片生成器
            temp_path = await self.image_generator.generate_rank_image(
                users_for_image, group_info, title, current_user_id
            )
            
            # 检查图片文件是否存在
            if os.path.exists(temp_path):
                yield event.image_result(temp_path)
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
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    self.logger.debug(f"临时图片文件已清理: {temp_path}")
                except OSError as e:
                    self.logger.warning(f"清理临时图片文件失败: {temp_path}, 错误: {e}")
    
    async def _render_rank_as_text(self, event: AstrMessageEvent, filtered_data: List[tuple], 
                                 group_info: GroupInfo, title: str, config: PluginConfig):
        """渲染排行榜为文字模式"""
        text_msg = self._generate_text_message(filtered_data, group_info, title, config)
        yield event.plain_result(text_msg)
    
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
        else:
            return None, None, "unknown"
    
    async def _filter_data_by_rank_type(self, group_data: List[UserData], rank_type: RankType) -> List[tuple]:
        """根据排行榜类型筛选数据并计算时间段内的发言次数 - 重构版本"""
        start_date, end_date, period_name = self._get_time_period_for_rank_type(rank_type)
        
        if rank_type == RankType.TOTAL:
            # 总榜：返回每个用户及其总发言数的元组，但过滤掉从未发言的用户
            return [(user, user.message_count) for user in group_data if user.message_count > 0]
        
        # 时间段过滤：统一处理日/周/月榜
        filtered_users = []
        for user in group_data:
            if not user.history:
                continue
            
            # 计算指定时间段的发言次数
            period_count = user.get_message_count_in_period(start_date, end_date)
            if period_count > 0:
                filtered_users.append((user, period_count))
        
        return filtered_users
    
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