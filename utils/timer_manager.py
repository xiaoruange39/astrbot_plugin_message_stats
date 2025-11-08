"""
定时任务管理器 - 最终修复版本
实现定时排行榜推送功能，采用正确的AstrBot主动消息API

主要修复：
1. 使用Context.send_message()和unified_msg_origin实现主动消息
2. 修复所有API调用错误
3. 实现真正的自动化消息发送
4. 确保定时推送完全自动化，不需要手动执行命令
"""

import asyncio
import re
import json
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from enum import Enum
from pathlib import Path
import aiofiles
from croniter import croniter
from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
# PlatformAdapterType 在 astrbot.api.event.filter 中
# 移除消息组件导入，使用MessageChain

from .models import RankType, UserData, GroupInfo
from .data_manager import DataManager
from .image_generator import ImageGenerator
from .date_utils import get_current_date, get_week_start, get_month_start
from .exception_handlers import safe_timer_operation, safe_generation, safe_data_operation


class TimerTaskStatus(Enum):
    """定时任务状态枚举"""
    STOPPED = "stopped"
    RUNNING = "running"
    ERROR = "error"
    PAUSED = "paused"


class PushService:
    """专门的推送服务类
    
    负责处理群组消息的发送，使用AstrBot主动消息API
    """
    
    def __init__(self, context, group_unified_msg_origins: Dict[str, str] = None):
        """初始化推送服务
        
        Args:
            context: AstrBot上下文对象
            group_unified_msg_origins: 群组unified_msg_origin映射表
        """
        self.context = context
        self.logger = astrbot_logger
        self.group_unified_msg_origins = group_unified_msg_origins or {}
        
    async def push_to_group(self, group_id: str, message: str, image_path: str = None) -> bool:
        """向指定群组推送消息 - 使用主动消息API
        
        Args:
            group_id: 群组ID
            message: 消息内容
            image_path: 可选的图片路径
            
        Returns:
            bool: 推送是否成功
        """
        try:
            # 记录推送尝试
            self.logger.info(f"开始推送消息到群组 {group_id}")
            
            # 获取群组的unified_msg_origin
            unified_msg_origin = self.group_unified_msg_origins.get(str(group_id))
            if not unified_msg_origin:
                self.logger.error(f"❌ 群组 {group_id} 推送失败: 缺少 unified_msg_origin")
                self.logger.info("💡 解决方案: 在该群组中发送任意消息以收集 unified_msg_origin")
                self.logger.info("📋 提示: 收集后再次尝试推送")
                return False
            
            # 构建MessageChain
            message_chain = MessageChain()
            
            # 如果有图片，添加到MessageChain
            if image_path and await aiofiles.os.path.exists(image_path):
                message_chain = message_chain.file_image(image_path)
            
            # 如果有文字消息，添加到MessageChain
            if message and message.strip():
                message_chain = message_chain.message(message)
            
            # 使用主动消息API发送
            await self.context.send_message(unified_msg_origin, message_chain)
            self.logger.info(f"主动消息发送成功: 群组 {group_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"推送消息到群组 {group_id} 时发生异常: {e}")
            return False
    
    async def _try_send_via_context_bot(self, group_id: str, message: str, image_path: str = None) -> bool:
        """尝试通过context.bot.api直接发送消息"""
        try:
            # 检查context.bot.api是否存在
            if hasattr(self.context, 'bot') and hasattr(self.context.bot, 'api'):
                # 准备消息内容
                message_content = message
                if image_path and await aiofiles.os.path.exists(image_path):
                    # 如果有图片，添加CQ码
                    message_content = f"[CQ:image,file={image_path}]\n{message}"
                
                # 直接调用api.send_group_msg
                api = self.context.bot.api
                if hasattr(api, 'send_group_msg'):
                    await api.send_group_msg(
                        group_id=int(group_id),
                        message=str(message_content)
                    )
                    self.logger.info(f"context.bot.api.send_group_msg 成功: 群组 {group_id}")
                    return True
                elif hasattr(api, 'send_group_message'):
                    await api.send_group_message(
                        group_id=int(group_id),
                        message=str(message_content)
                    )
                    self.logger.info(f"context.bot.api.send_group_message 成功: 群组 {group_id}")
                    return True
                elif hasattr(api, 'send_msg'):
                    await api.send_msg(
                        group_id=int(group_id),
                        message=str(message_content)
                    )
                    self.logger.info(f"context.bot.api.send_msg 成功: 群组 {group_id}")
                    return True
                    
            return False
            
        except Exception as e:
            self.logger.warning(f"context.bot.api 发送失败: {e}")
            return False
    
    async def _try_send_via_bot_api(self, group_id: str, message: str, image_path: str = None) -> bool:
        """尝试通过Bot API发送消息 - 重新优化版本"""
        try:
            if hasattr(self.context, 'bot') and hasattr(self.context.bot, 'api'):
                # 准备消息内容
                message_content = message
                if image_path and await aiofiles.os.path.exists(image_path):
                    # 如果有图片，添加CQ码
                    message_content = f"[CQ:image,file={image_path}]\n{message}"
                
                # 尝试不同的API方法
                api_methods = [
                    ('call_action', {'action': 'send_group_msg', 'params': {'group_id': int(group_id), 'message': message_content}}),
                    ('call_action', {'action': 'send_group_message', 'params': {'group_id': int(group_id), 'message': message_content}}),
                    ('call_action', {'action': 'send_msg', 'params': {'group_id': int(group_id), 'message': message_content}}),
                ]
                
                for method_name, call_params in api_methods:
                    try:
                        if hasattr(self.context.bot.api, method_name):
                            method = getattr(self.context.bot.api, method_name)
                            if method_name == 'call_action':
                                result = await method(**call_params)
                            else:
                                result = await method(**call_params['params'])
                            self.logger.info(f"Bot API {method_name} 成功: 群组 {group_id}")
                            return True
                    except Exception as method_error:
                        self.logger.warning(f"Bot API {method_name} 失败: {method_error}")
                        continue
                        
            return False
            
        except Exception as e:
            self.logger.warning(f"Bot API 发送失败: {e}")
            return False
    
    async def _try_send_via_call_action(self, group_id: str, message: str, image_path: str = None) -> bool:
        """尝试通过call_action发送消息 - 重新设计版本"""
        try:
            if hasattr(self.context, 'bot') and hasattr(self.context.bot, 'api'):
                # 准备消息内容
                message_content = message
                if image_path and await aiofiles.os.path.exists(image_path):
                    # 如果有图片，添加CQ码
                    message_content = f"[CQ:image,file={image_path}]\n{message}"
                
                # 使用call_action方法发送群组消息
                await self.context.bot.api.call_action(
                    'send_group_msg',
                    group_id=int(group_id),
                    message=str(message_content)
                )
                self.logger.info(f"call_action 成功: 群组 {group_id}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.warning(f"call_action 失败: {e}")
            return False
    
    async def _try_send_via_reply(self, group_id: str, message: str, image_path: str = None) -> bool:
        """尝试通过reply方法发送消息"""
        try:
            # 这个方法可能不适用于群组消息，但作为备选方案
            return False
                
        except Exception as e:
            self.logger.warning(f"reply 方法失败: {e}")
            return False


class TimerManager:
    """定时任务管理器 - 修复版本
    
    负责管理定时排行榜推送任务，采用正确的AstrBot API调用方式。
    
    主要改进：
    1. 使用PushService处理消息发送
    2. 修复API调用错误
    3. 实现真正的自动化推送
    4. 增强错误处理和诊断
    
    Attributes:
        data_manager (DataManager): 数据管理器实例
        image_generator (ImageGenerator): 图片生成器实例
        push_service (PushService): 推送服务实例
        timer_task (Optional[asyncio.Task]): 定时任务句柄
        status (TimerTaskStatus): 当前任务状态
        next_push_time (Optional[datetime]): 下次推送时间
        logger: 日志记录器
        
    Example:
        >>> timer_manager = TimerManager(data_manager, image_generator, context)
        >>> await timer_manager.start_timer(config)
        >>> status = await timer_manager.get_status()
    """
    
    def __init__(self, data_manager: DataManager, image_generator: ImageGenerator, context=None, group_unified_msg_origins: Dict[str, str] = None):
        """初始化定时任务管理器
        
        Args:
            data_manager (DataManager): 数据管理器实例
            image_generator (ImageGenerator): 图片生成器实例
            context: AstrBot上下文对象
            group_unified_msg_origins: 群组unified_msg_origin映射表
        """
        self.data_manager = data_manager
        self.image_generator = image_generator
        self.context = context
        self.group_unified_msg_origins = group_unified_msg_origins or {}
        
        # 初始化推送服务（即使context为None也创建实例）
        self.push_service = PushService(context, self.group_unified_msg_origins)
        
        self.timer_task: Optional[asyncio.Task] = None
        self.status = TimerTaskStatus.STOPPED
        self.next_push_time: Optional[datetime] = None
        self.logger = astrbot_logger
        self._stop_event = asyncio.Event()
        
        # 记录初始化状态
        if context:
            self.logger.info("定时任务管理器初始化成功（完整功能）")
        else:
            self.logger.info("定时任务管理器初始化成功（受限模式）")
        
    @safe_timer_operation(default_return=False)
    async def start_timer(self, config) -> bool:
        """启动定时任务
        
        Args:
            config: 插件配置对象
            
        Returns:
            bool: 启动是否成功
        """
        # 检查推送服务是否初始化
        if not self.push_service:
            self.logger.error("推送服务未初始化，无法启动定时任务")
            return False
        
        # 检查定时功能是否启用
        if not config.timer_enabled:
            self.logger.info("定时功能未启用，跳过启动")
            return False
        
        # 验证配置
        if not self._validate_timer_config(config):
            self.logger.error("定时配置验证失败")
            return False
        
        # 检查unified_msg_origin可用性
        missing_origins = []
        for group_id in config.timer_target_groups:
            if str(group_id) not in self.push_service.group_unified_msg_origins:
                missing_origins.append(str(group_id))
        
        if missing_origins:
            self.logger.warning(f"⚠️ 以下群组缺少unified_msg_origin: {', '.join(missing_origins)}")
            self.logger.info("💡 解决方案: 在对应群组中发送任意消息以收集unified_msg_origin")
            self.logger.info("📝 定时任务仍会启动，但推送时会失败直到unified_msg_origin被收集")
            self.logger.info("📋 提示: 可以使用 #手动推送 命令测试推送功能")
        
        # 如果任务已在运行，先停止
        if self.timer_task and not self.timer_task.done():
            await self.stop_timer()
        
        # 设置状态
        self.status = TimerTaskStatus.RUNNING
        
        # 计算下次推送时间
        self.next_push_time = self._calculate_next_push_time(config.timer_push_time)
        
        # 启动定时任务
        self.timer_task = asyncio.create_task(self._timer_loop(config))
        
        self.logger.info(f"定时任务已启动，下次推送时间: {self.next_push_time}")
        return True
    
    @safe_timer_operation(default_return=False)
    async def stop_timer(self) -> bool:
        """停止定时任务
        
        Returns:
            bool: 停止是否成功
        """
        # 设置停止事件
        self._stop_event.set()
        
        # 取消定时任务
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass
        
        # 重置状态
        self.status = TimerTaskStatus.STOPPED
        self.next_push_time = None
        self._stop_event.clear()
        
        self.logger.info("定时任务已停止")
        return True
    
    async def pause_timer(self) -> bool:
        """暂停定时任务
        
        Returns:
            bool: 暂停是否成功
        """
        try:
            if self.status == TimerTaskStatus.RUNNING:
                self.status = TimerTaskStatus.PAUSED
                self.logger.info("定时任务已暂停")
                return True
            return False
        except Exception as e:
            self.logger.error(f"暂停定时任务失败: {e}")
            return False
    
    async def resume_timer(self) -> bool:
        """恢复定时任务
        
        Returns:
            bool: 恢复是否成功
        """
        try:
            if self.status == TimerTaskStatus.PAUSED:
                self.status = TimerTaskStatus.RUNNING
                self.logger.info("定时任务已恢复")
                return True
            return False
        except Exception as e:
            self.logger.error(f"恢复定时任务失败: {e}")
            return False
    
    async def _timer_loop(self, config):
        """定时任务主循环
        
        Args:
            config: 插件配置对象
        """
        try:
            while not self._stop_event.is_set():
                if self.status == TimerTaskStatus.PAUSED:
                    # 暂停状态，等待恢复
                    await asyncio.sleep(60)  # 每分钟检查一次
                    continue
                
                if self.status != TimerTaskStatus.RUNNING:
                    break
                
                # 检查是否到达推送时间
                now = datetime.now()
                if self.next_push_time and now >= self.next_push_time:
                    # 执行推送任务
                    self.logger.info("开始执行定时推送任务")
                    success = await self._execute_push_task(config)
                    if success:
                        self.logger.info("✅ 定时推送任务执行成功")
                    else:
                        self.logger.error("❌ 定时推送任务执行失败")
                    
                    # 计算下次推送时间
                    self.next_push_time = self._calculate_next_push_time(config.timer_push_time)
                    self.logger.info(f"下次推送时间: {self.next_push_time}")
                
                # 等待一段时间后再次检查
                await asyncio.sleep(60)  # 每分钟检查一次
                
        except asyncio.CancelledError:
            self.logger.info("定时任务被取消")
        except (OSError, IOError, RuntimeError, ValueError) as e:
            # 捕获定时任务循环中的系统级、运行时和数值错误
            self.logger.error(f"定时任务循环异常: {e}")
            self.status = TimerTaskStatus.ERROR
            # 5分钟后重试
            await asyncio.sleep(300)
            if not self._stop_event.is_set():
                self.logger.info("尝试重启定时任务")
                self.timer_task = asyncio.create_task(self._timer_loop(config))
    
    @safe_timer_operation(default_return=False)
    async def _execute_push_task(self, config) -> bool:
        """执行推送任务
        
        Args:
            config: 插件配置对象
            
        Returns:
            bool: 推送是否成功
        """
        success_count = 0
        total_count = len(config.timer_target_groups)
        
        self.logger.info(f"开始推送到 {total_count} 个群组")
        
        # 遍历所有目标群组
        for group_id in config.timer_target_groups:
            # 验证群组ID格式 - 确保是字符串类型
            if not isinstance(group_id, str):
                self.logger.warning(f"跳过无效的群组ID类型: {type(group_id)}")
                continue
            
            if not group_id.isdigit():
                self.logger.warning(f"跳过无效的群组ID格式: {group_id}")
                continue
            
            # 推送到指定群组
            success = await self._push_to_group(group_id, config)
            if success:
                success_count += 1
                self.logger.info(f"✅ 群组 {group_id} 推送成功")
            else:
                self.logger.warning(f"❌ 群组 {group_id} 推送失败")
        
        # 记录推送结果
        if success_count == total_count:
            self.logger.info(f"🎉 定时推送完全成功: {success_count}/{total_count} 个群组推送成功")
            return True
        elif success_count > 0:
            self.logger.warning(f"⚠️ 定时推送部分成功: {success_count}/{total_count} 个群组推送成功")
            return True
        else:
            self.logger.error(f"💥 定时推送完全失败: 0/{total_count} 个群组推送成功")
            return False
    
    async def _get_group_name(self, group_id: str) -> str:
        """获取群组名称
        
        Args:
            group_id: 群组ID
            
        Returns:
            str: 群组名称，如果获取失败则返回默认格式
        """
        try:
            # 首先尝试从缓存文件获取群组名称
            group_file_path = self.data_manager.groups_dir / f"{group_id}.json"
            
            if await aiofiles.os.path.exists(group_file_path):
                async with aiofiles.open(group_file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content.strip():
                        data = json.loads(content)
                        # 尝试从用户数据中推断群组名称
                        if isinstance(data, list) and len(data) > 0:
                            # 从第一个用户的数据中尝试获取群组信息
                            first_user = data[0]
                            if isinstance(first_user, dict):
                                # 尝试各种可能的群组名称字段
                                for key in ['group_name', 'group_name_cn', '群名', '群组名', 'name', 'title']:
                                    if key in first_user and first_user[key]:
                                        return str(first_user[key]).strip()
                                # 如果有群组信息字段，尝试从中提取
                                if 'group_info' in first_user and isinstance(first_user['group_info'], dict):
                                    for key in ['name', 'title', 'group_name']:
                                        if key in first_user['group_info'] and first_user['group_info'][key]:
                                            return str(first_user['group_info'][key]).strip()
                        elif isinstance(data, dict):
                            # 如果数据是字典格式，尝试从中获取群组名称
                            for key in ['group_name', 'group_name_cn', '群名', '群组名', 'name', 'title']:
                                if key in data and data[key]:
                                    return str(data[key]).strip()
            
            # 如果缓存中没有，尝试通过API获取群组信息
            if self.context:
                try:
                    # 检查是否为aiocqhttp平台
                    if hasattr(self.context, 'get_platform'):
                        platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                        if platform and hasattr(platform, 'get_client'):
                            client = platform.get_client()
                            if client and hasattr(client, 'api'):
                                # 调用get_group_info API
                                group_info = await client.api.call_action('get_group_info', group_id=group_id)
                                if group_info and isinstance(group_info, dict):
                                    # 尝试从返回的群组信息中获取群名
                                    group_name = group_info.get('group_name') or group_info.get('group_title') or group_info.get('name')
                                    if group_name:
                                        return str(group_name).strip()
                except Exception as api_error:
                    self.logger.warning(f"通过API获取群组 {group_id} 名称失败: {api_error}")
            
            # 如果所有方法都失败，返回默认格式
            return f"群{group_id}"
            
        except (OSError, IOError, ValueError, TypeError, KeyError) as e:
            # 捕获获取群组名称时的文件、系统、数值、类型和键错误
            self.logger.warning(f"获取群组 {group_id} 名称时发生错误: {e}")
            return f"群{group_id}"
    
    @safe_data_operation(default_return=False)
    async def _push_to_group(self, group_id: str, config) -> bool:
        """向指定群组推送排行榜
        
        Args:
            group_id: 群组ID
            config: 插件配置对象
            
        Returns:
            bool: 推送是否成功
        """
        # 获取群组数据
        group_data = await self.data_manager.get_group_data(group_id)
        if not group_data:
            self.logger.warning(f"群组 {group_id} 没有数据")
            return False
        
        # 根据排行榜类型筛选数据
        # 定时推送强制使用今日排行榜
        rank_type = RankType.DAILY
        self.logger.info(f"群组 {group_id} 定时推送使用今日排行榜")
        
        filtered_data = await self._filter_data_by_rank_type(group_data, rank_type)
        if not filtered_data:
            self.logger.warning(f"群组 {group_id} 没有符合条件的用户数据")
            return False
        
        # 排序数据
        filtered_data.sort(key=lambda x: x[1], reverse=True)
        
        # 限制数量
        limited_data = filtered_data[:config.rand]
        users_for_rank = []
        
        # 为用户数据设置display_total属性，确保图片生成器使用正确的数据
        # 修复：图片版排行榜显示昨日数据的问题
        for user_data, count in limited_data:
            # 设置display_total属性（时间段内的发言数）
            user_data.display_total = count
            users_for_rank.append(user_data)
        
        # 创建群组信息
        group_info = GroupInfo(group_id=str(group_id))
        # 获取群组名称
        group_name = await self._get_group_name(group_id)
        group_info.group_name = group_name
        
        # 生成标题
        title = self._generate_title(rank_type)
        
        # 定时推送只发送图片版本
        image_path = await self._generate_rank_image(users_for_rank, group_info, title, config)
        if not image_path:
            self.logger.warning(f"群组 {group_id} 图片生成失败")
            return False
        
        # 定时推送只发送图片，不发送文字消息
        success = await self.push_service.push_to_group(group_id, "", image_path)
        
        # 清理临时图片文件
        if image_path and await aiofiles.os.path.exists(image_path):
            try:
                await aiofiles.os.unlink(image_path)
            except OSError as e:
                self.logger.warning(f"清理临时图片文件失败: {image_path}, 错误: {e}")
        
        return success
    
    @safe_generation(default_return=None)
    async def _generate_rank_image(self, users: List[UserData], group_info: GroupInfo, title: str, config) -> Optional[str]:
        """生成排行榜图片
        
        Args:
            users: 用户数据列表
            group_info: 群组信息
            title: 排行榜标题
            config: 插件配置对象
            
        Returns:
            Optional[str]: 图片路径，失败时返回None
        """
        try:
            if not self.image_generator:
                return None
            
            # 使用图片生成器生成图片
            temp_path = await self.image_generator.generate_rank_image(
                users, group_info, title, "0"  # 系统推送，用户ID设为"0"
            )
            
            return temp_path
            
        except Exception as e:
            self.logger.error(f"生成排行榜图片失败: {e}")
            return None
    
    def _validate_timer_config(self, config) -> bool:
        """验证定时配置
        
        Args:
            config: 插件配置对象
            
        Returns:
            bool: 验证是否通过
        """
        try:
            # 验证推送时间格式
            if not self._validate_time_format(config.timer_push_time):
                self.logger.error(f"无效的推送时间格式: {config.timer_push_time}")
                return False
            
            # 验证目标群组
            if not config.timer_target_groups:
                self.logger.error("未配置目标群组")
                return False
            
            # 验证群组的unified_msg_origin可用性
            missing_origins = []
            for group_id in config.timer_target_groups:
                if str(group_id) not in self.push_service.group_unified_msg_origins:
                    missing_origins.append(str(group_id))
            
            if missing_origins:
                self.logger.warning(f"⚠️ 以下群组缺少unified_msg_origin: {', '.join(missing_origins)}")
                self.logger.info("💡 解决方案: 在对应群组中发送任意消息以收集unified_msg_origin")
                self.logger.info("📋 提示: 可以使用 #手动推送 命令测试推送功能")
                self.logger.info("📝 定时任务仍会启动，但推送时会失败直到unified_msg_origin被收集")
            
            # 验证排行榜类型
            try:
                self._parse_rank_type(config.timer_rank_type)
            except ValueError:
                self.logger.error(f"无效的排行榜类型: {config.timer_rank_type}")
                return False
            
            return True
            
        except (ValueError, TypeError, KeyError, RuntimeError) as e:
            # 捕获配置验证时的数值、类型、键和运行时错误
            self.logger.error(f"验证定时配置时发生错误: {e}")
            return False
    
    def _validate_time_format(self, time_str: str) -> bool:
        """验证时间格式
        
        Args:
            time_str: 时间字符串，支持两种格式：
                - 简单格式: "HH:MM" (每日指定时间推送)
                - Cron格式: "0 9 * * *" (支持复杂的定时表达式)
            
        Returns:
            bool: 格式是否有效
        """
        # 首先尝试 cron 格式
        try:
            croniter(time_str)
            return True
        except (ValueError, TypeError):
            # cron 格式失败后尝试简单格式
            pattern = r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'
            return bool(re.match(pattern, time_str))
    
    def _calculate_next_push_time(self, push_time: str) -> datetime:
        """计算下次推送时间
        
        Args:
            push_time: 推送时间，支持两种格式：
                - 简单格式: "HH:MM" (每日指定时间)
                - Cron格式: "0 9 * * *" (支持复杂定时表达式)
            
        Returns:
            datetime: 下次推送时间
        """
        try:
            # 获取当前时间
            now = datetime.now()
            
            # 首先尝试使用 cron 格式
            try:
                cron = croniter(push_time, now)
                next_time = cron.get_next(datetime)
                return next_time
            except (ValueError, TypeError):
                # 如果 cron 格式失败，则使用简单格式 "HH:MM"
                if not ':' in push_time:
                    raise ValueError("不支持的时间格式")
                
                hour, minute = map(int, push_time.split(':'))
                target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                
                # 如果今天的时间已过，则推到明天
                if target_time <= now:
                    target_time += timedelta(days=1)
                
                return target_time
            
        except (ValueError, TypeError, OSError, IOError) as e:
            # 捕获计算推送时间时的数值、类型和系统错误
            self.logger.error(f"计算下次推送时间失败: {e}")
            # 返回默认时间（明早9点）
            tomorrow = datetime.now() + timedelta(days=1)
            return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
    
    def _parse_rank_type(self, rank_type_str: str) -> RankType:
        """解析排行榜类型
        
        Args:
            rank_type_str: 排行榜类型字符串
            
        Returns:
            RankType: 排行榜类型枚举
            
        Raises:
            ValueError: 当类型字符串无效时抛出
        """
        rank_type_mapping = {
            'total': RankType.TOTAL,
            'daily': RankType.DAILY,
            'week': RankType.WEEKLY,
            'weekly': RankType.WEEKLY,
            'month': RankType.MONTHLY,
            'monthly': RankType.MONTHLY
        }
        
        rank_type_str = rank_type_str.lower()
        if rank_type_str in rank_type_mapping:
            return rank_type_mapping[rank_type_str]
        else:
            raise ValueError(f"无效的排行榜类型: {rank_type_str}")
    
    async def _filter_data_by_rank_type(self, group_data: List[UserData], rank_type: RankType) -> List[tuple]:
        """根据排行榜类型筛选数据
        
        Args:
            group_data: 群组用户数据
            rank_type: 排行榜类型
            
        Returns:
            List[tuple]: 筛选后的数据，格式为[(UserData, count)]
        """
        try:
            current_date = get_current_date().to_date()
            
            if rank_type == RankType.TOTAL:
                # 总榜：返回每个用户及其总发言数的元组，但过滤掉从未发言的用户
                return [(user, user.message_count) for user in group_data if user.message_count > 0]
            
            # 时间段过滤
            filtered_users = []
            for user in group_data:
                if not user.history:
                    continue
                
                # 计算指定时间段的发言次数
                period_count = user.get_message_count_in_period(
                    *self._get_time_period_for_rank_type(rank_type, current_date)
                )
                if period_count > 0:
                    filtered_users.append((user, period_count))
            
            return filtered_users
            
        except Exception as e:
            self.logger.error(f"筛选数据时发生错误: {e}")
            return []
    
    def _get_time_period_for_rank_type(self, rank_type: RankType, current_date) -> tuple:
        """获取排行榜类型对应的时间段
        
        Args:
            rank_type: 排行榜类型
            current_date: 当前日期
            
        Returns:
            tuple: (start_date, end_date)
        """
        if rank_type == RankType.DAILY:
            return current_date, current_date
        elif rank_type == RankType.WEEKLY:
            # 获取本周开始日期(周一)
            week_start = get_week_start(current_date)
            return week_start, current_date
        elif rank_type == RankType.MONTHLY:
            # 获取本月开始日期
            month_start = get_month_start(current_date)
            return month_start, current_date
        else:
            # 总榜不需要时间段过滤
            return None, None
    
    def _generate_title(self, rank_type: RankType) -> str:
        """生成标题
        
        Args:
            rank_type: 排行榜类型
            
        Returns:
            str: 排行榜标题
        """
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
    
    def _generate_text_message(self, users_with_values: List[tuple], group_info: GroupInfo, title: str, config) -> str:
        """生成文字消息
        
        Args:
            users_with_values: 包含(UserData, sort_value)元组的列表
            group_info: 群组信息
            title: 排行榜标题
            config: 插件配置对象
            
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
            
            # 添加排名表情
            if i == 0:
                emoji = "🥇"
            elif i == 1:
                emoji = "🥈"
            elif i == 2:
                emoji = "🥉"
            else:
                emoji = f"{i + 1}."
            
            msg.append(f"{emoji} {user.nickname}·{user_messages}次(占比{percentage:.2f}%)\n")
        
        # 添加推送标识
        msg.append(f"\n🤖 定时推送 | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        
        return ''.join(msg)
    
    async def get_status(self) -> Dict[str, Any]:
        """获取定时任务状态
        
        Returns:
            Dict[str, Any]: 状态信息字典
        """
        try:
            status_info = {
                "status": self.status.value,
                "next_push_time": self.next_push_time.isoformat() if self.next_push_time else None,
                "time_until_next": None,
                "is_running": self.status == TimerTaskStatus.RUNNING,
                "task_exists": self.timer_task is not None and not self.timer_task.done(),
                "push_service_initialized": self.push_service is not None
            }
            
            # 计算距离下次推送的时间
            if self.next_push_time:
                now = datetime.now()
                if self.next_push_time > now:
                    delta = self.next_push_time - now
                    status_info["time_until_next"] = str(delta)
                else:
                    status_info["time_until_next"] = "已过期"
            
            return status_info
            
        except Exception as e:
            self.logger.error(f"获取定时任务状态失败: {e}")
            return {
                "status": "error",
                "next_push_time": None,
                "time_until_next": None,
                "is_running": False,
                "task_exists": False,
                "push_service_initialized": False
            }
    
    async def manual_push(self, config, group_id: str = None) -> bool:
        """手动推送排行榜
        
        Args:
            config: 插件配置对象
            group_id: 目标群组ID，如果为None则推送到所有配置群组
            
        Returns:
            bool: 推送是否成功
        """
        try:
            if not self.push_service:
                self.logger.error("推送服务未初始化")
                return False
            
            if group_id:
                # 推送到指定群组
                return await self._push_to_group(group_id, config)
            else:
                # 推送到所有配置群组
                success_count = 0
                for target_group in config.timer_target_groups:
                    if await self._push_to_group(target_group, config):
                        success_count += 1
                
                return success_count > 0
                
        except (OSError, IOError, RuntimeError, ValueError, TypeError) as e:
            # 捕获手动推送时的系统、运行时、数值和类型错误
            self.logger.error(f"手动推送失败: {e}")
            return False
    
    async def update_config(self, config, group_unified_msg_origins: Dict[str, str] = None) -> bool:
        """更新定时配置
        
        Args:
            config: 新的插件配置对象
            group_unified_msg_origins: 新的群组unified_msg_origin映射表
            
        Returns:
            bool: 更新是否成功
        """
        try:
            # 更新群组unified_msg_origin映射表
            if group_unified_msg_origins and self.push_service:
                self.push_service.group_unified_msg_origins = group_unified_msg_origins
            
            # 如果定时任务正在运行，需要重新启动
            was_running = self.status == TimerTaskStatus.RUNNING
            
            if was_running:
                await self.stop_timer()
            
            # 重新启动定时任务
            if config.timer_enabled:
                if self.context and self.push_service:
                    # 完整功能模式
                    success = await self.start_timer(config)
                    if success:
                        self.logger.info("定时配置已更新，定时任务重启成功")
                    else:
                        self.logger.warning("定时配置已更新，但定时任务重启失败")
                    return success
                else:
                    # 受限模式（无context）
                    self.logger.warning("定时功能已启用，但缺少上下文信息，无法执行实际推送")
                    self.status = TimerTaskStatus.STOPPED
                    return True  # 返回成功，因为配置更新本身是成功的
            else:
                self.logger.info("定时功能未启用")
                return True
            
        except (ValueError, TypeError, KeyError, RuntimeError, OSError, IOError) as e:
            # 捕获更新配置时的数值、类型、键、运行时和系统错误
            self.logger.error(f"更新定时配置失败: {e}")
            return False