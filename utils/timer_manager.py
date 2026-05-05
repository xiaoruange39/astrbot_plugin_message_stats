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

# croniter 是可选依赖，用于支持 cron 表达式
try:
    from croniter import croniter
    CRONITER_AVAILABLE = True
except ImportError:
    croniter = None
    CRONITER_AVAILABLE = False

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
# PlatformAdapterType 在 astrbot.api.event.filter 中
# 移除消息组件导入，使用MessageChain

from .models import RankType, UserData, GroupInfo
from .data_manager import DataManager
from .image_generator import ImageGenerator
from .llm_analyzer import LLMAnalyzer
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
    
    负责处理群组消息的发送，使用AstrBot主动消息API（Context.send_message）
    
    注意: AstrBot 的 Context 对象用于主动发送消息，
    它不包含 bot 属性，因此必须使用 unified_msg_origin 方式发送消息。
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
        
        使用 Context.send_message() 和 unified_msg_origin 实现主动消息发送。
        这是 AstrBot 官方推荐的主动消息发送方式。
        
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
            
            # 检查 context 是否存在
            if not self.context:
                self.logger.error(f"❌ 群组 {group_id} 推送失败: context 未初始化")
                return False
            
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
            self.logger.info(f"✅ 主动消息发送成功: 群组 {group_id}")
            return True
            
        except AttributeError as e:
            self.logger.error(f"推送消息到群组 {group_id} 时属性错误: {e}")
            self.logger.info("💡 请确保 context 对象已正确初始化")
            return False
        except (OSError, IOError) as e:
            self.logger.error(f"推送消息到群组 {group_id} 时文件错误: {e}")
            return False
        except (RuntimeError, ValueError, TypeError) as e:
            self.logger.error(f"推送消息到群组 {group_id} 时发生异常: {e}")
            return False


class TimerManager:
    """定时任务管理器 - 修复版本
    
    负责管理定时排行榜推送任务，采用正确的AstrBot API调用方式。
    
    主要改进：
    1. 使用PushService处理消息发送
    2. 修复API调用错误
    3. 实现真正的自动化推送
    4. 增强错误处理和诊断
    5. 使用类级别的全局锁防止多实例重复执行
    
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
    
    # 类级别的全局锁和标志，确保所有实例共享
    _global_execution_lock: Optional[asyncio.Lock] = None
    _global_is_executing: bool = False
    _global_next_push_time: Optional[datetime] = None
    _global_instance_id: int = 0  # 全局实例计数器，用于检测重复实例
    _global_active_task_id: Optional[int] = None  # 当前活跃的定时任务实例ID
    
    
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
        
        # 初始化类级别的全局锁（只在第一个实例时创建）
        if TimerManager._global_execution_lock is None:
            TimerManager._global_execution_lock = asyncio.Lock()
        
        # 分配唯一的实例ID
        TimerManager._global_instance_id += 1
        self._instance_id = TimerManager._global_instance_id
        self.logger.info(f"定时任务管理器实例 #{self._instance_id} 已创建")
        
        # 实例级别的锁（用于非关键操作）
        self._execution_lock = asyncio.Lock()
        self._is_executing = False
        
        # 群组名称缓存（group_id -> group_name）
        # 可以通过 update_group_name_cache 方法从外部更新
        self._group_name_cache: Dict[str, str] = {}
        
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
        # 使用执行锁防止并发启动
        async with self._execution_lock:
            # 如果任务已在运行，直接返回成功
            if self.status == TimerTaskStatus.RUNNING and self.timer_task and not self.timer_task.done():
                self.logger.debug("定时任务已在运行中，跳过重复启动")
                return True
            
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
            
            # 检查unified_msg_origin可用性（支持多种匹配方式）
            # timer_target_groups 可能存储的是：
            #   1. 群组ID（如 -1003715592711 或 1081839722）
            #   2. unified_msg_origin 字符串（如 Amy:GroupMessage:1081839722）
            missing_origins = []
            for group_id in config.timer_target_groups:
                group_id_str = str(group_id)
                
                # 方式1: 直接匹配键名
                if group_id_str in self.push_service.group_unified_msg_origins:
                    continue
                
                # 方式2: 匹配 unified_msg_origin 的值（提取最后一个:后的部分）
                found = False
                for origin_key, origin_value in self.push_service.group_unified_msg_origins.items():
                    try:
                        extracted_id = origin_value.rsplit(':', 1)[-1]
                        if extracted_id == group_id_str:
                            found = True
                            break
                    except (AttributeError, IndexError, ValueError):
                        continue
                
                # 方式3: 如果 group_id_str 本身是 unified_msg_origin 格式（如 Amy:GroupMessage:1081839722）
                # 尝试提取其中的群组ID并匹配
                if not found and ':' in group_id_str:
                    try:
                        extracted_from_target = group_id_str.rsplit(':', 1)[-1]
                        if extracted_from_target in self.push_service.group_unified_msg_origins:
                            found = True
                        # 方式4: 用提取的ID去匹配 unified_msg_origin 的值
                        if not found:
                            for origin_key, origin_value in self.push_service.group_unified_msg_origins.items():
                                try:
                                    origin_extracted = origin_value.rsplit(':', 1)[-1]
                                    if origin_extracted == extracted_from_target:
                                        found = True
                                        break
                                except (AttributeError, IndexError, ValueError):
                                    continue
                    except (AttributeError, IndexError, ValueError):
                        pass
                
                if not found:
                    missing_origins.append(group_id_str)
            
            if missing_origins:
                self.logger.info(f"📋 以下群组尚未收集unified_msg_origin: {', '.join(missing_origins)}")
                self.logger.info("💡 解决方案: 在对应群组中发送任意消息以收集unified_msg_origin")
                self.logger.info("📝 定时任务仍会启动，但推送时会失败直到unified_msg_origin被收集")
                self.logger.info("📋 提示: 可以使用 #手动推送发言榜 命令测试推送功能")
            
            # 如果任务已在运行，先停止（不再需要，因为已在锁内检查）
            if self.timer_task and not self.timer_task.done():
                self._stop_event.set()
                self.timer_task.cancel()
                try:
                    await self.timer_task
                except asyncio.CancelledError:
                    pass
                self._stop_event.clear()
            
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
        # 注册当前实例为活跃任务
        TimerManager._global_active_task_id = self._instance_id
        self.logger.info(f"定时任务循环 #{self._instance_id} 已启动")
        
        try:
            while not self._stop_event.is_set():
                # 检查是否为当前活跃的实例，如果不是则自动退出
                if TimerManager._global_active_task_id is not None and TimerManager._global_active_task_id != self._instance_id:
                    self.logger.info(f"检测到更新的实例 #{TimerManager._global_active_task_id} 已启动，实例 #{self._instance_id} 自动退出")
                    break
                
                if self.status == TimerTaskStatus.PAUSED:
                    # 暂停状态，等待恢复
                    await asyncio.sleep(60)  # 每分钟检查一次
                    continue
                
                if self.status != TimerTaskStatus.RUNNING:
                    break
                
                # 检查是否到达推送时间
                now = datetime.now()
                if self.next_push_time and now >= self.next_push_time:
                    # 使用全局执行标志快速检查（无锁）
                    if TimerManager._global_is_executing:
                        self.logger.debug("全局推送任务正在执行中，跳过本次触发")
                        await asyncio.sleep(10)  # 短暂等待后再检查
                        continue
                    
                    # 使用全局锁防止多实例重复推送
                    async with TimerManager._global_execution_lock:
                        # 双重检查：获取锁后再次确认是否需要执行
                        if TimerManager._global_is_executing:
                            self.logger.debug("全局推送任务正在执行中（锁内检查），跳过")
                            continue
                        
                        # 检查全局时间（可能已被其他实例更新）
                        if TimerManager._global_next_push_time and datetime.now() < TimerManager._global_next_push_time:
                            self.logger.debug("全局推送时间已被更新，跳过本次执行")
                            continue
                        
                        # 再次检查是否为活跃实例（防止在等待锁期间被新实例取代）
                        if TimerManager._global_active_task_id is not None and TimerManager._global_active_task_id != self._instance_id:
                            self.logger.info(f"锁内检查: 实例 #{self._instance_id} 已被取代，跳过执行")
                            break
                        
                        # 立即更新全局和实例的下次推送时间
                        next_time = self._calculate_next_push_time(config.timer_push_time)
                        self.next_push_time = next_time
                        TimerManager._global_next_push_time = next_time
                        TimerManager._global_is_executing = True
                    
                    try:
                        # 执行推送任务
                        self.logger.info(f"实例 #{self._instance_id} 开始执行定时推送任务")
                        success = await self._execute_push_task(config)
                        if success:
                            self.logger.info(f"✅ 实例 #{self._instance_id} 定时推送任务执行成功")
                        else:
                            self.logger.error(f"❌ 实例 #{self._instance_id} 定时推送任务执行失败")
                        
                        self.logger.info(f"下次推送时间: {self.next_push_time}")
                    finally:
                        # 确保释放全局执行标志
                        TimerManager._global_is_executing = False
                
                # 等待一段时间后再次检查
                await asyncio.sleep(60)  # 每分钟检查一次
                
        except asyncio.CancelledError:
            self.logger.info(f"实例 #{self._instance_id} 定时任务被取消")
            TimerManager._global_is_executing = False
            TimerManager._global_active_task_id = None
        except (OSError, IOError, RuntimeError, ValueError) as e:
            self.logger.error(f"实例 #{self._instance_id} 定时任务循环异常: {e}")
            self.status = TimerTaskStatus.ERROR
            TimerManager._global_is_executing = False
            # 限制重试次数，防止无限递归导致内存泄漏
            retry_count = getattr(self, '_timer_retry_count', 0)
            if retry_count >= 3:
                self.logger.critical(f"实例 #{self._instance_id} 定时任务已重试 {retry_count} 次仍失败，停止重试")
                TimerManager._global_active_task_id = None
                return
            self._timer_retry_count = retry_count + 1
            self.logger.info(f"实例 #{self._instance_id} 将在5分钟后重试 (第{self._timer_retry_count}次)")
            await asyncio.sleep(300)
            if not self._stop_event.is_set():
                self.logger.info(f"实例 #{self._instance_id} 尝试重启定时任务")
                # ❗ 修复：先取消旧任务再创建新任务，防止多个定时循环同时运行
                if self.timer_task and not self.timer_task.done():
                    self.timer_task.cancel()
                    try:
                        await self.timer_task
                    except asyncio.CancelledError:
                        pass
                self.timer_task = asyncio.create_task(self._timer_loop(config))
        except Exception as e:
            # 捕获所有其他异常，防止任务静默退出
            self.logger.error(f"实例 #{self._instance_id} 定时任务未知异常: {type(e).__name__}: {e}")
            self.status = TimerTaskStatus.ERROR
            TimerManager._global_is_executing = False
            # 未知异常不重试，避免无限递归
            self.logger.critical(f"实例 #{self._instance_id} 定时任务因未知异常停止，不再自动重试")
    
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
            
            group_id_str = str(group_id).strip()
            if not group_id_str:
                self.logger.warning(f"跳过空的群组ID")
                continue
            
            # timer_target_groups 可能存储的是：
            #   1. 群组ID（如 -1003715592711 或 1081839722）
            #   2. unified_msg_origin 字符串（如 Amy:GroupMessage:1081839722）
            # 如果是 unified_msg_origin 格式，尝试提取其中的群组ID
            actual_group_id = group_id_str
            if ':' in group_id_str:
                try:
                    extracted = group_id_str.rsplit(':', 1)[-1]
                    if extracted.lstrip('-').isdigit():
                        actual_group_id = extracted
                        self.logger.debug(f"从 unified_msg_origin 中提取群组ID: {group_id_str} -> {actual_group_id}")
                except (AttributeError, IndexError, ValueError):
                    pass
            
            # 验证是否为有效的数字ID（支持负数）
            if not actual_group_id.lstrip('-').isdigit():
                self.logger.warning(f"跳过无效的群组ID格式: {group_id}")
                continue
            
            # 推送到指定群组
            success = await self._push_to_group(actual_group_id, config)
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
    
    def update_group_name_cache(self, group_id: str, group_name: str):
        """更新群组名称缓存
        
        此方法供外部（如 main.py）调用，在获取到群组名称时更新缓存。
        
        Args:
            group_id: 群组ID
            group_name: 群组名称
        """
        if group_id and group_name:
            self._group_name_cache[str(group_id)] = str(group_name)
            self.logger.debug(f"群组名称缓存已更新: {group_id} -> {group_name}")
    
    def update_group_name_cache_batch(self, group_names: Dict[str, str]):
        """批量更新群组名称缓存
        
        Args:
            group_names: 群组ID到名称的映射字典
        """
        if group_names:
            self._group_name_cache.update(group_names)
            self.logger.debug(f"群组名称缓存批量更新: {len(group_names)} 个群组")

    async def _get_group_name(self, group_id: str) -> str:
        """获取群组名称
        
        优先级：
        1. 内存缓存（_group_name_cache）
        2. 数据文件中的 group_name 字段
        3. 默认格式（群+ID）
        
        Args:
            group_id: 群组ID
            
        Returns:
            str: 群组名称，如果获取失败则返回默认格式
        """
        group_id_str = str(group_id)
        
        # 1. 优先从内存缓存获取
        if group_id_str in self._group_name_cache:
            cached_name = self._group_name_cache[group_id_str]
            if cached_name:
                return cached_name
        
        try:
            # 2. 从数据文件获取群组名称
            group_file_path = self.data_manager.groups_dir / f"{group_id}.json"
            
            if await aiofiles.os.path.exists(group_file_path):
                async with aiofiles.open(group_file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content.strip():
                        data = json.loads(content)
                        
                        # 优先从 group_name 字段获取
                        if isinstance(data, dict) and data.get('group_name'):
                            group_name = str(data['group_name']).strip()
                            # 更新到内存缓存
                            self._group_name_cache[group_id_str] = group_name
                            return group_name
                        
                        # 尝试从用户数据中推断群组名称
                        if isinstance(data, list) and len(data) > 0:
                            first_user = data[0]
                            if isinstance(first_user, dict):
                                for key in ['group_name', 'group_name_cn', '群名', '群组名', 'name', 'title']:
                                    if key in first_user and first_user[key]:
                                        group_name = str(first_user[key]).strip()
                                        self._group_name_cache[group_id_str] = group_name
                                        return group_name
                        elif isinstance(data, dict):
                            for key in ['group_name', 'group_name_cn', '群名', '群组名', 'name', 'title']:
                                if key in data and data[key]:
                                    group_name = str(data[key]).strip()
                                    self._group_name_cache[group_id_str] = group_name
                                    return group_name
            
            # 3. 返回默认格式
            return f"群{group_id}"
            
        except (OSError, IOError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            self.logger.debug(f"获取群组 {group_id} 名称时发生错误: {e}")
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
        
        # 定时推送前强制刷新昵称缓存，确保显示最新昵称
        await self._refresh_nickname_cache_for_timer_push(group_id, group_data)
        
        # ========== 头衔处理：先清除旧头衔，再生成新头衔，最后保存到磁盘 ==========
        token_usage_info = None
        titles_map = None
        
        # 在 LLM 生成前先清除所有用户的旧头衔（避免旧头衔残留）
        for user in group_data:
            user.display_title = None
            user.display_title_color = None
        
        # 如果启用了 LLM 头衔分析，调用 LLM 生成新头衔
        if config.llm_enabled:
            try:
                # 使用 AstrBot 内部 Provider 系统调用 LLM
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
                
                # 获取群组名称用于提示词
                grp_name = await self._get_group_name(group_id)
                titles, token_usage = await llm_analyzer.analyze_users(
                    group_data, grp_name, min_daily_messages=min_daily
                )
                
                if token_usage and token_usage.get("total_tokens", 0) > 0:
                    token_usage_info = token_usage
                
                if titles:
                    self.logger.info(f"✅ LLM 头衔生成成功: 为 {len(titles)} 个用户生成了头衔")
                    titles_map = titles
                    for user in group_data:
                        if user.user_id in titles:
                            info = titles[user.user_id]
                            if isinstance(info, dict):
                                user.display_title = info.get("title")
                                user.display_title_color = info.get("color")
                            else:
                                user.display_title = info
                else:
                    self.logger.warning("⚠️ LLM 头衔生成结果为空，将使用不带头衔的排行榜")
            except Exception as e:
                self.logger.error(f"❌ LLM 头衔生成异常: {e}", exc_info=True)
                self.logger.info("将使用不带头衔的排行榜继续推送")
        
        # ✅ 将更新后的头衔（包括清除的旧头衔）持久化保存到磁盘
        await self.data_manager.save_group_data(group_id, group_data)
        
        # 根据排行榜类型筛选数据
        # 定时推送强制使用今日排行榜
        rank_type = RankType.DAILY
        filtered_data = await self._filter_data_by_rank_type(group_data, rank_type)
        if not filtered_data:

            # 今日无数据（如凌晨推送），回退到昨日数据
            yesterday = datetime.now().date() - timedelta(days=1)
            filtered_data = [(user, user.get_message_count_in_period(yesterday, yesterday)) for user in group_data if user.get_message_count_in_period(yesterday, yesterday) > 0]
            if filtered_data:
                self.logger.info(f"群组 {group_id} 今日无数据，回退到昨日({yesterday})数据")
            else:
                self.logger.warning(f"群组 {group_id} 今日和昨日均无数据")
                return False
        else:
            self.logger.info(f"群组 {group_id} 定时推送使用今日排行榜")
        
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
        image_path = await self._generate_rank_image(users_for_rank, group_info, title, config, token_usage_info)
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
    async def _generate_rank_image(self, users: List[UserData], group_info: GroupInfo, title: str, config, token_usage: Dict[str, int] = None) -> Optional[str]:
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
            
            # 从users中提取titles_map（已经由_push_to_group设置好了display_title和display_title_color）
            # 构造包含颜色信息的titles_map给图片生成器
            titles_map = {}
            for user in users:
                if user.display_title:
                    if user.display_title_color:
                        titles_map[user.user_id] = {"title": user.display_title, "color": user.display_title_color}
                    else:
                        titles_map[user.user_id] = user.display_title
            
            # 使用图片生成器生成图片
            temp_path = await self.image_generator.generate_rank_image(
                users, group_info, title, "0", token_usage, titles_map  # 系统推送，用户ID设为"0"
            )
            
            return temp_path

            
        except Exception as e:
            self.logger.error(f"生成排行榜图片失败: {e}")
            return None
    
    def _validate_timer_config(self, config) -> bool:
        """验证定时配置
        
        只验证配置格式是否正确，不检查 unified_msg_origin 可用性
        （unified_msg_origin 检查在 start_timer 中进行）
        
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
            
            # 验证排行榜类型
            try:
                self._parse_rank_type(config.timer_rank_type)
            except ValueError:
                self.logger.error(f"无效的排行榜类型: {config.timer_rank_type}")
                return False
            
            return True
            
        except (ValueError, TypeError, KeyError, RuntimeError) as e:
            self.logger.error(f"验证定时配置时发生错误: {e}")
            return False
    
    def _validate_time_format(self, time_str: str) -> bool:
        """验证时间格式
        
        Args:
            time_str: 时间字符串，支持两种格式：
                - 简单格式: "HH:MM" (每日指定时间推送)
                - Cron格式: "0 9 * * *" (需要安装 croniter)
            
        Returns:
            bool: 格式是否有效
        """
        # 首先尝试简单格式 HH:MM
        pattern = r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'
        if re.match(pattern, time_str):
            return True
        
        # 如果 croniter 可用，尝试 cron 格式
        if CRONITER_AVAILABLE and croniter:
            try:
                croniter(time_str)
                return True
            except (ValueError, TypeError):
                pass
        
        return False
    
    def _calculate_next_push_time(self, push_time: str) -> datetime:
        """计算下次推送时间
        
        Args:
            push_time: 推送时间，支持两种格式：
                - 简单格式: "HH:MM" (每日指定时间)
                - Cron格式: "0 9 * * *" (需要安装 croniter)
            
        Returns:
            datetime: 下次推送时间
        """
        try:
            # 获取当前时间
            now = datetime.now()
            
            # 首先尝试简单格式 "HH:MM"
            if ':' in push_time:
                parts = push_time.split(':')
                if len(parts) == 2:
                    try:
                        hour, minute = int(parts[0]), int(parts[1])
                        if 0 <= hour <= 23 and 0 <= minute <= 59:
                            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            # 如果今天的时间已过，则推到明天
                            if target_time <= now:
                                target_time += timedelta(days=1)
                            return target_time
                    except ValueError:
                        pass
            
            # 如果 croniter 可用，尝试 cron 格式
            if CRONITER_AVAILABLE and croniter:
                try:
                    cron = croniter(push_time, now)
                    next_time = cron.get_next(datetime)
                    return next_time
                except (ValueError, TypeError):
                    pass
            
            # 格式无效，返回默认时间
            raise ValueError(f"不支持的时间格式: {push_time}")
            
        except (ValueError, TypeError, OSError, IOError) as e:
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
            'monthly': RankType.MONTHLY,
            'year': RankType.YEARLY,
            'yearly': RankType.YEARLY,
            'lastyear': RankType.LAST_YEAR,
            'last_year': RankType.LAST_YEAR
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
                # 兼容新旧两种数据格式：_message_dates（新）或 history（旧）
                # 先确保 _message_dates 数据完整（兜底保护）
                user._ensure_message_dates()
                if not user._message_dates and not user.history:
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
        elif rank_type == RankType.YEARLY:
            # 获取本年开始日期
            year_start = current_date.replace(month=1, day=1)
            return year_start, current_date
        elif rank_type == RankType.LAST_YEAR:
            # 获取去年的时间范围（1月1日 - 12月31日）
            from datetime import date
            last_year = current_date.year - 1
            year_start = date(last_year, 1, 1)
            year_end = date(last_year, 12, 31)
            return year_start, year_end
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
        elif rank_type == RankType.YEARLY:
            return f"本年[{now.year}年]发言榜单"
        elif rank_type == RankType.LAST_YEAR:
            last_year = now.year - 1
            return f"去年[{last_year}年]发言榜单"
        else:
            return "发言榜单"
    
    async def _refresh_nickname_cache_for_timer_push(self, group_id: str, group_data):
        """定时推送前尝试刷新昵称缓存
        
        注意: 定时推送时无法直接访问 bot API（因为 Context 对象没有 bot 属性），
        因此这里只能使用已存储的数据。如果需要最新昵称，用户需要在群中发言以触发更新。
        """
        try:
            if not self.context:
                self.logger.debug("定时推送时缺少context，跳过昵称刷新")
                return
            
            # 定时推送时，Context 对象不包含 bot 属性
            # 无法直接调用 get_group_member_list API
            # 昵称会在用户发送消息时通过事件处理自动更新
            # 这里只记录日志，不进行实际刷新
            self.logger.debug(f"定时推送使用缓存的昵称数据，群组 {group_id} 共 {len(group_data)} 个用户")
            
            # 如果需要获取群成员信息，需要通过平台适配器
            # 但定时推送场景下通常没有可用的平台连接
            # 因此跳过昵称刷新，使用已存储的昵称
            
        except (AttributeError, TypeError, ValueError) as e:
            # 这是预期的情况，因为 Context 没有 bot 属性
            self.logger.debug(f"定时推送跳过昵称刷新: {e}")
    
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
        
        注意：此方法只更新 unified_msg_origin 映射表，不会重复启动已运行的定时任务。
        如果定时任务需要重启（如配置变更），会先停止再启动。
        
        Args:
            config: 新的插件配置对象
            group_unified_msg_origins: 新的群组unified_msg_origin映射表
            
        Returns:
            bool: 更新是否成功
        """
        try:
            # 更新群组unified_msg_origin映射表
            if group_unified_msg_origins is not None and self.push_service:
                self.push_service.group_unified_msg_origins = group_unified_msg_origins
                self.group_unified_msg_origins = group_unified_msg_origins
            
            # 检查定时任务状态
            is_running = self.status == TimerTaskStatus.RUNNING and self.timer_task and not self.timer_task.done()
            
            # 如果定时功能被禁用，停止已运行的任务
            if not config.timer_enabled:
                if is_running:
                    await self.stop_timer()
                    self.logger.info("定时功能已禁用，定时任务已停止")
                return True
            
            # 如果定时任务已在运行，只更新映射表，不重新启动
            if is_running:
                self.logger.info("定时配置已更新（unified_msg_origin映射表已刷新）")
                return True
            
            # 如果定时任务未运行，尝试启动
            if self.context and self.push_service:
                success = await self.start_timer(config)
                if success:
                    self.logger.info("定时配置已更新，定时任务启动成功")
                else:
                    self.logger.warning("定时配置已更新，但定时任务启动失败")
                return success
            else:
                self.logger.warning("定时功能已启用，但缺少上下文信息，无法执行实际推送")
                return True
            
        except (ValueError, TypeError, KeyError, RuntimeError, OSError, IOError) as e:
            self.logger.error(f"更新定时配置失败: {e}")
            return False