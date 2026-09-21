import os
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
from datetime import date, datetime, timedelta
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
from .group_id_utils import get_fallback_group_name, is_placeholder_group_name


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
            
            send_target = self.group_unified_msg_origins.get(str(group_id)) or str(group_id)
            if send_target == str(group_id):
                self.logger.debug(f"群组 {group_id} 未找到 unified_msg_origin，直接使用群组ID推送")

            # 构建MessageChain
            message_chain = MessageChain()
            
            # 如果有图片，添加到MessageChain
            if image_path and os.path.exists(image_path):
                message_chain = message_chain.file_image(image_path)
            
            # 如果有文字消息，添加到MessageChain
            if message and message.strip():
                message_chain = message_chain.message(message)
            
            # 使用主动消息API发送
            await self.context.send_message(send_target, message_chain)
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
        
    Note:
        文件锁机制：每次创建新实例时生成递增的 generation ID 并写入文件。
        旧实例每秒检查一次，发现自己不是最新时自动退出。
        锁文件只有1个，永远被最新实例覆写。
        调用 stop_timer() 时如果本实例是最新的，会清理 lock 文件。
    """
    
    # 文件锁机制：使用磁盘文件记录当前最新的 generation ID
    # 每个新实例写入自的 generation，旧实例循环中检查发现自己不再是
    # 最新的 generation 时自动退出，防止重装后旧实例继续发送
    # 锁文件永不累积（始终只有1个文件，被最新实例不断覆写）
    _lock_file_base: Optional[str] = None
    
    
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
        self._timer_tasks: List[asyncio.Task] = []
        self._timer_tasks_config: List[Any] = []
        self.status = TimerTaskStatus.STOPPED
        self.next_push_time: Optional[datetime] = None
        self.logger = astrbot_logger
        self._stop_event = asyncio.Event()
        
        # 设置文件锁基础路径
        if TimerManager._lock_file_base is None:
            TimerManager._lock_file_base = str(data_manager.data_dir)
        
        # 分配唯一的 generation ID（使用 UUID 确保跨进程唯一）
        import uuid
        self._generation = uuid.uuid4().hex
        self.logger.info(f"定时任务管理器 Generation #{self._generation} 已创建")
        
        # 写入文件锁，标记当前为最新 generation
        self._write_lock_file()
        
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
        
    def _get_lock_file_path(self) -> Optional[Path]:
        """获取文件锁路径（始终只有1个文件，被覆写，不会累积垃圾）"""
        base = TimerManager._lock_file_base
        if not base:
            return None
        return Path(base) / ".timer_generation.lock"

    def _write_lock_file(self):
        """写入文件锁，覆写为当前 generation
        
        锁文件始终只有1个，永远被当前的 generation 覆写，
        不会产生垃圾文件。
        """
        lock_path = self._get_lock_file_path()
        if not lock_path:
            return
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(str(self._generation), encoding='utf-8')
        except Exception as e:
            self.logger.warning(f"写入定时任务文件锁失败: {e}")

    def _is_latest_generation(self) -> bool:
        """检查当前 generation 是否最新（读取文件锁）
        
        文件锁中存的是最新的 generation ID。
        如果和本实例不一致（有更新实例写入新的 generation），
        说明本实例已过期，应自动退出。
        """
        lock_path = self._get_lock_file_path()
        if not lock_path:
            return True
        try:
            if not lock_path.exists():
                return True
            content = lock_path.read_text(encoding='utf-8').strip()
            return self._generation == content
        except (ValueError, OSError, IOError):
            return True

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
            if not self._get_enabled_timer_tasks(config):
                self.logger.info("定时功能未启用，跳过启动")
                return False
            
            # 验证配置
            if not self._validate_timer_config(config):
                self.logger.error("定时配置验证失败")
                return False
            
            # 如果任务已在运行，先停止（不再需要，因为已在锁内检查）
            if self.timer_task and not self.timer_task.done():
                self._stop_event.set()
                self.timer_task.cancel()
                try:
                    await self.timer_task
                except asyncio.CancelledError:
                    pass
                self._stop_event.clear()
            for task in self._timer_tasks:
                if not task.done():
                    self._stop_event.set()
                    task.cancel()
            if self._timer_tasks:
                await asyncio.gather(*self._timer_tasks, return_exceptions=True)
                self._timer_tasks.clear()
                self._timer_tasks_config = []
                self._stop_event.clear()

            enabled_tasks = self._get_enabled_timer_tasks(config)
            if not enabled_tasks:
                self.logger.error("未配置启用的定时任务")
                return False

            self.status = TimerTaskStatus.RUNNING
            for task in enabled_tasks:
                setattr(task, '_next_push_time', self._calculate_next_push_time(task.push_time))
            self._timer_tasks_config = enabled_tasks
            self._refresh_next_push_time()
            self._timer_tasks = [asyncio.create_task(self._timer_loop(config, task_config)) for task_config in enabled_tasks]
            self.timer_task = self._timer_tasks[0]

            task_times = self._format_timer_task_times(enabled_tasks)
            self.logger.info(f"定时任务已启动，共 {len(enabled_tasks)} 个任务，下次推送时间: {self.next_push_time}；全部任务: {task_times}")
            return True
    
    @safe_timer_operation(default_return=False)
    async def stop_timer(self) -> bool:
        """停止定时任务
        
        如果当前实例是最新的 generation，会清理 lock 文件。
        
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
        
        for task in self._timer_tasks:
            if not task.done():
                task.cancel()
        if self._timer_tasks:
            await asyncio.gather(*self._timer_tasks, return_exceptions=True)
            self._timer_tasks.clear()
            self._timer_tasks_config = []

        # 重置状态
        self.status = TimerTaskStatus.STOPPED
        self.next_push_time = None
        self._stop_event.clear()
        
        # 如果是当前最新 generation，清理锁文件
        try:
            if self._is_latest_generation():
                lock_path = self._get_lock_file_path()
                if lock_path and lock_path.exists():
                    lock_path.unlink()
                    self.logger.debug("已清理定时任务文件锁")
        except Exception as e:
            self.logger.debug(f"清理文件锁忽略: {e}")
        
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
    
    async def _timer_loop(self, config, task_config=None):
        """定时任务主循环
        
        Args:
            config: 插件配置对象
        """
        task_config = task_config or self._get_primary_timer_task(config)
        if not task_config:
            self.logger.error("缺少定时任务配置，定时循环退出")
            return
        task_name = getattr(task_config, '__template_key', 'rank_push')
        task_label = self._format_timer_task_label(task_config)
        # 更新文件锁，标记当前为最新 generation
        self._write_lock_file()
        self.logger.info(f"定时任务循环 Generation {self._generation} 已启动: {task_label}")
        
        try:
            while not self._stop_event.is_set():
                # 检查文件锁：如果当前不是最新 generation，自动退出
                if not self._is_latest_generation():
                    self.logger.info(f"检测到更新的 Generation 已启动，当前 #{self._generation} 自动退出")
                    break
                
                if self.status == TimerTaskStatus.PAUSED:
                    await asyncio.sleep(60)
                    continue
                
                if self.status != TimerTaskStatus.RUNNING:
                    break
                
                # 检查是否到达推送时间
                now = datetime.now()
                task_next_push_time = getattr(task_config, '_next_push_time', None)
                if task_next_push_time and now >= task_next_push_time:
                    # 执行前再次确认
                    if not self._is_latest_generation():
                        self.logger.info(f"推送前检测到更新的 Generation，当前 #{self._generation} 退出")
                        break
                    
                    # 计算下次推送时间
                    next_time = self._calculate_next_push_time(task_config.push_time)
                    setattr(task_config, '_next_push_time', next_time)
                    self._refresh_next_push_time()
                    
                    try:
                        self.logger.info(f"Generation {self._generation} 开始执行定时推送任务: {task_label}")
                        success = await self._execute_push_task(config, task_config)
                        if success:
                            self.logger.info(f"✅ Generation {self._generation} 定时推送任务执行成功")
                        else:
                            self.logger.error(f"❌ Generation {self._generation} 定时推送任务执行失败")
                        self.logger.info(f"全部任务下次推送时间: {self._format_timer_task_times(self._timer_tasks_config)}")
                    except Exception as e:
                        self.logger.error(f"Generation {self._generation} 定时推送异常: {e}")
                
                await asyncio.sleep(60)
                
        except asyncio.CancelledError:
            self.logger.info(f"Generation {self._generation} 定时任务被取消")
            pass
        except (OSError, IOError, RuntimeError, ValueError) as e:
            self.logger.error(f"Generation {self._generation} 定时任务循环异常: {e}")
            self.status = TimerTaskStatus.ERROR
            retry_count = getattr(self, '_timer_retry_count', 0)
            if retry_count >= 3:
                self.logger.critical(f"Generation {self._generation} 定时任务已重试 {retry_count} 次仍失败，停止重试")
                return
            self._timer_retry_count = retry_count + 1
            self.logger.info(f"Generation {self._generation} 将在5分钟后重试 (第{self._timer_retry_count}次)")
            await asyncio.sleep(300)
            if not self._stop_event.is_set():
                self.logger.info(f"Generation {self._generation} 尝试重启定时任务")
                self.timer_task = asyncio.create_task(self._timer_loop(config, task_config))
        except Exception as e:
            self.logger.error(f"Generation {self._generation} 定时任务未知异常: {type(e).__name__}: {e}")
            self.status = TimerTaskStatus.ERROR
    
    @safe_timer_operation(default_return=False)
    async def _execute_push_task(self, config, task_config=None) -> bool:
        """执行推送任务
        
        Args:
            config: 插件配置对象
            
        Returns:
            bool: 推送是否成功
        """
        task_config = task_config or self._get_primary_timer_task(config)
        target_groups = task_config.target_groups if task_config else config.timer_target_groups
        rank_type_value = task_config.rank_type if task_config else config.timer_rank_type
        success_count = 0
        total_count = len(target_groups)

        self.logger.info(f"开始推送到 {total_count} 个群组")
        
        # 遍历所有目标群组
        for group_id in target_groups:
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
            success = await self._push_to_group(actual_group_id, config, rank_type_value)
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
            if cached_name and not is_placeholder_group_name(cached_name, group_id_str):
                return cached_name
        
        try:
            # 2. 从群名持久化缓存获取
            group_names_file = self.data_manager.data_dir / "group_names.json"
            if os.path.exists(group_names_file):
                async with aiofiles.open(group_names_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content.strip():
                        group_names = json.loads(content)
                        if isinstance(group_names, dict) and group_names.get(group_id_str):
                            group_name = str(group_names[group_id_str]).strip()
                            if not is_placeholder_group_name(group_name, group_id_str):
                                self._group_name_cache[group_id_str] = group_name
                                return group_name

            # 3. 从数据文件获取群组名称
            group_file_path = self.data_manager.group_store._get_group_file_path(group_id)
            
            if os.path.exists(group_file_path):
                async with aiofiles.open(group_file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content.strip():
                        data = json.loads(content)
                        
                        # 优先从 group_name 字段获取
                        if isinstance(data, dict) and data.get('group_name'):
                            group_name = str(data['group_name']).strip()
                            # 更新到内存缓存
                            if not is_placeholder_group_name(group_name, group_id_str):
                                self._group_name_cache[group_id_str] = group_name
                                return group_name
                        
                        # 尝试从用户数据中推断群组名称
                        if isinstance(data, list) and len(data) > 0:
                            first_user = data[0]
                            if isinstance(first_user, dict):
                                for key in ['group_name', 'group_name_cn', '群名', '群组名', 'name', 'title']:
                                    if key in first_user and first_user[key]:
                                        group_name = str(first_user[key]).strip()
                                        if not is_placeholder_group_name(group_name, group_id_str):
                                            self._group_name_cache[group_id_str] = group_name
                                            return group_name
                        elif isinstance(data, dict):
                            for key in ['group_name', 'group_name_cn', '群名', '群组名', 'name', 'title']:
                                if key in data and data[key]:
                                    group_name = str(data[key]).strip()
                                    if not is_placeholder_group_name(group_name, group_id_str):
                                        self._group_name_cache[group_id_str] = group_name
                                        return group_name
            
            # 4. 返回默认格式
            return get_fallback_group_name(group_id)
            
        except (OSError, IOError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            self.logger.debug(f"获取群组 {group_id} 名称时发生错误: {e}")
            return get_fallback_group_name(group_id)
    
    @safe_data_operation(default_return=False)
    async def _push_to_group(self, group_id: str, config, rank_type_value: str = None) -> bool:
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
        
        # 加载持久化的头衔到运行时字段
        # 确保即使本次不触发LLM分析，排行榜也能显示已有的头衔
        for user in group_data:
            if user.llm_title:
                user.display_title = user.llm_title
                if user.llm_title_color:
                    user.display_title_color = user.llm_title_color
        
        # 定时/手动推送前补齐缺失昵称，避免图片里显示空昵称
        await self._ensure_nicknames_for_push(group_id, group_data)

        
        # 如果启用了 LLM 头衔分析，先调用 LLM 生成头衔
        token_usage_info = None
        titles_map = None
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
                
                # 修复：llm_title 可能存为空字符串 ""（旧数据污染），统一视为无头衔处理
                # 无头衔用户也需要满足 min_daily_messages 才触发 LLM
                users_need_llm = []
                users_with_title = []
                min_daily_msgs = getattr(config, 'llm_min_daily_messages', 0)
                for u in group_data:
                    if u.message_count <= 0:
                        continue
                    # 判断是否有有效头衔（兼容空字符串脏数据）
                    has_title = bool(u.llm_title and u.llm_title.strip())
                    
                    if not has_title:
                        # 无头衔用户：必须满足 min_daily 才触发 LLM
                        if min_daily_msgs > 0 and u.message_count < min_daily_msgs:
                            users_with_title.append(u)
                            continue
                        users_need_llm.append(u)
                    elif u.llm_title_message_count == 0:
                        if min_daily_msgs > 0 and u.message_count >= min_daily_msgs:
                            users_need_llm.append(u)
                        else:
                            users_with_title.append(u)
                    elif min_daily_msgs > 0 and (u.message_count - u.llm_title_message_count) >= min_daily_msgs:
                        users_need_llm.append(u)
                    else:
                        users_with_title.append(u)

                if users_with_title:
                    self.logger.info(f"跳过 {len(users_with_title)} 个增量不足的用户，保留现有头衔")
                
                titles = None
                token_usage = None
                if users_need_llm:
                    self.logger.info(f"为 {len(users_need_llm)} 个无头衔用户调用LLM生成头衔")
                    grp_name = await self._get_group_name(group_id)
                    titles, token_usage = await llm_analyzer.analyze_users(
                        users_need_llm, grp_name, min_daily_messages=min_daily
                    )
                
                if token_usage and token_usage.get("total_tokens", 0) > 0:
                    token_usage_info = token_usage
                
                # 构建titles_map：已有头衔 + 新生成的头衔
                titles_map = {}
                for user in group_data:
                    if user.llm_title:
                        titles_map[user.user_id] = {
                            "title": user.llm_title,
                            "color": user.llm_title_color or "#7C3AED"
                        }
                
                if titles:
                    self.logger.info(f"✅ LLM头衔生成成功: 为 {len(titles)} 个新用户生成了头衔")
                    for user in group_data:
                        if user.user_id in titles:
                            info = titles[user.user_id]
                            if isinstance(info, dict):
                                title_text = info.get("title")
                                title_color = info.get("color")
                                user.display_title = title_text
                                user.display_title_color = title_color
                                user.llm_title = title_text
                                user.llm_title_color = title_color
                                user.llm_title_message_count = user.message_count
                            else:
                                title_text = info
                                user.display_title = title_text
                                user.display_title_color = None
                                user.llm_title = title_text
                                user.llm_title_color = None
                            titles_map[user.user_id] = {
                                "title": user.llm_title,
                                "color": user.llm_title_color or "#7C3AED"
                            }
                    # 持久化头衔到文件
                    await self.data_manager.save_group_data(group_id, group_data)
                    self.logger.info("定时推送头衔数据已持久化保存到文件")
                else:
                    self.logger.info(f"所有用户已有持久化头衔，无需LLM分析，使用已有头衔")
            except Exception as e:
                self.logger.error(f"❌ LLM 头衔生成异常: {e}", exc_info=True)
                self.logger.info("将使用不带头衔的排行榜继续推送")

        
        # 根据排行榜类型筛选数据
        rank_type = self._parse_rank_type(rank_type_value or config.timer_rank_type)
        effective_rank_type = rank_type
        rank_title = self._generate_title(rank_type)
        filtered_data = await self._filter_data_by_rank_type(group_data, rank_type)
        if not filtered_data:
            if rank_type == RankType.DAILY:
                # 今日无数据（如凌晨推送），回退到昨日数据
                yesterday = datetime.now().date() - timedelta(days=1)
                for user in group_data:
                    count = user.get_message_count_in_period(yesterday, yesterday)
                    if count > 0:
                        filtered_data.append((user, count))
                if filtered_data:
                    effective_rank_type = RankType.YESTERDAY
                    rank_title = self._generate_title(RankType.YESTERDAY)
                    self.logger.info(f"群组 {group_id} 今日无数据，回退到昨日({yesterday})数据")
                else:
                    self.logger.warning(f"群组 {group_id} 今日和昨日均无数据")
                    return False
            else:
                self.logger.warning(f"群组 {group_id} {rank_title}无数据")
                return False
        else:
            self.logger.info(f"群组 {group_id} 定时推送使用{rank_title}")

        # 排序数据
        filtered_data.sort(key=lambda x: x[1], reverse=True)
        self._apply_rank_trend_labels(group_data, filtered_data, effective_rank_type, config)

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
        unified_msg_origin = self.group_unified_msg_origins.get(str(group_id), "")
        group_info = GroupInfo(group_id=str(group_id), unified_msg_origin=unified_msg_origin)
        # 获取群组名称
        group_name = await self._get_group_name(group_id)
        group_info.group_name = group_name
        
        # 生成标题
        title = rank_title
        
        # 定时推送只发送图片版本
        image_path = await self._generate_rank_image(users_for_rank, group_info, title, config, token_usage_info)
        if not image_path:
            self.logger.warning(f"群组 {group_id} 图片生成失败")
            return False
        
        # 定时推送只发送图片，不发送文字消息
        try:
            success = await self.push_service.push_to_group(group_id, "", image_path)
        finally:
            # 清理临时图片文件（确保无论push_to_group是否异常都执行）
            if image_path:
                try:
                    if os.path.exists(image_path):
                        os.unlink(image_path)
                except Exception as e:
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
    
    def _format_timer_task_label(self, task) -> str:
        template_key = getattr(task, '__template_key', 'rank_push')
        push_time = getattr(task, 'push_time', '09:00')
        rank_type = getattr(task, 'rank_type', 'daily')
        groups = getattr(task, 'target_groups', []) or []
        next_time = getattr(task, '_next_push_time', None)
        next_text = next_time.strftime('%Y-%m-%d %H:%M:%S') if next_time else '未计算'
        return f"{template_key} time={push_time} rank={rank_type} groups={len(groups)} next={next_text}"

    def _format_timer_task_times(self, tasks) -> str:
        return "; ".join(self._format_timer_task_label(task) for task in tasks) or "无"

    def _refresh_next_push_time(self):
        next_times = []
        for task in getattr(self, '_timer_tasks_config', []):
            next_time = getattr(task, '_next_push_time', None)
            if next_time:
                next_times.append(next_time)
        self.next_push_time = min(next_times) if next_times else None

    def _get_enabled_timer_tasks(self, config) -> List[Any]:
        tasks = getattr(config, 'timer_tasks', None)
        if isinstance(tasks, list) and tasks:
            return [task for task in tasks if getattr(task, 'enabled', False)]
        if getattr(config, 'timer_enabled', False):
            return [self._get_primary_timer_task(config)]
        return []

    def _get_primary_timer_task(self, config):
        tasks = getattr(config, 'timer_tasks', None)
        if isinstance(tasks, list) and tasks:
            enabled_tasks = [task for task in tasks if getattr(task, 'enabled', False)]
            return enabled_tasks[0] if enabled_tasks else tasks[0]

        class LegacyTimerTask:
            pass

        task = LegacyTimerTask()
        task.__template_key = 'rank_push'
        task.enabled = getattr(config, 'timer_enabled', False)
        task.push_time = getattr(config, 'timer_push_time', '09:00')
        task.target_groups = getattr(config, 'timer_target_groups', [])
        task.rank_type = getattr(config, 'timer_rank_type', 'daily')
        return task

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
            enabled_tasks = self._get_enabled_timer_tasks(config)
            if not enabled_tasks:
                self.logger.error("未配置启用的定时任务")
                return False

            for task in enabled_tasks:
                if not self._validate_time_format(task.push_time):
                    self.logger.error(f"无效的推送时间格式: {task.push_time}")
                    return False
                if not task.target_groups:
                    self.logger.error("未配置目标群组")
                    return False
                try:
                    self._parse_rank_type(task.rank_type)
                except ValueError:
                    self.logger.error(f"无效的排行榜类型: {task.rank_type}")
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
            '总榜': RankType.TOTAL,
            'daily': RankType.DAILY,
            '今日榜': RankType.DAILY,
            '日榜': RankType.DAILY,
            '今天': RankType.DAILY,
            'yesterday': RankType.YESTERDAY,
            '昨日榜': RankType.YESTERDAY,
            '昨日': RankType.YESTERDAY,
            '昨天': RankType.YESTERDAY,
            'week': RankType.WEEKLY,
            'weekly': RankType.WEEKLY,
            '本周榜': RankType.WEEKLY,
            '周榜': RankType.WEEKLY,
            'month': RankType.MONTHLY,
            'monthly': RankType.MONTHLY,
            '本月榜': RankType.MONTHLY,
            '月榜': RankType.MONTHLY,
            'year': RankType.YEARLY,
            'yearly': RankType.YEARLY,
            '本年榜': RankType.YEARLY,
            '年榜': RankType.YEARLY,
            'lastyear': RankType.LAST_YEAR,
            'last_year': RankType.LAST_YEAR,
            '去年榜': RankType.LAST_YEAR,
            '去年': RankType.LAST_YEAR
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
        elif rank_type == RankType.YESTERDAY:
            yesterday = current_date - timedelta(days=1)
            return yesterday, yesterday
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

    def _apply_rank_trend_labels(
        self,
        group_data: List[UserData],
        filtered_data: List[tuple],
        rank_type: RankType,
        config,
    ) -> None:
        """为定时推送排行榜填充与手动榜一致的趋势文案。"""
        for user in group_data:
            user.display_trend_label = None
            user.display_trend_type = None

        if not filtered_data or not getattr(config, "rank_trend_enabled", True):
            return

        if rank_type == RankType.TOTAL:
            trend_days = getattr(config, "rank_trend_days", 7)
            try:
                trend_days = max(2, min(30, int(trend_days)))
            except (TypeError, ValueError):
                trend_days = 7
            self._apply_total_rank_trends(filtered_data, trend_days)
            return

        current_date = datetime.now().date()
        start_date, end_date = self._get_time_period_for_rank_type(
            rank_type,
            current_date,
        )
        if not start_date or not end_date:
            return

        comparison_label = {
            RankType.DAILY: "昨天",
            RankType.YESTERDAY: "前天",
            RankType.WEEKLY: "上周",
            RankType.MONTHLY: "上月",
            RankType.YEARLY: "去年",
            RankType.LAST_YEAR: "前年",
        }.get(rank_type, "上一周期")

        previous_start, previous_end = self._get_previous_period_range(
            start_date,
            end_date,
        )
        previous_rank = self._calculate_period_rank_sync(
            group_data,
            previous_start,
            previous_end,
        )
        previous_rank.sort(key=lambda item: item[1], reverse=True)
        previous_counts = {user.user_id: count for user, count in previous_rank}
        previous_ranks = {
            user.user_id: index + 1
            for index, (user, _) in enumerate(previous_rank)
        }

        for index, (user, current_count) in enumerate(filtered_data):
            previous_count = previous_counts.get(user.user_id, 0)
            if previous_count <= 0:
                user.display_trend_label = f"相比{comparison_label}新上榜"
                user.display_trend_type = "new"
                continue

            count_label, trend_type = self._format_count_delta_label(
                current_count,
                previous_count,
                comparison_label,
            )
            rank_label = self._format_rank_delta_label(
                index + 1,
                previous_ranks.get(user.user_id),
            )
            user.display_trend_label = " · ".join(
                part for part in (count_label, rank_label) if part
            )
            user.display_trend_type = trend_type

    @staticmethod
    def _get_previous_period_range(
        start_date: date,
        end_date: date,
    ) -> tuple[date, date]:
        period_days = (end_date - start_date).days + 1
        previous_end = start_date - timedelta(days=1)
        previous_start = previous_end - timedelta(days=period_days - 1)
        return previous_start, previous_end

    def _calculate_period_rank_sync(
        self,
        group_data: List[UserData],
        start_date: date,
        end_date: date,
    ) -> List[tuple]:
        ranked = []
        for user in group_data:
            count = user.get_message_count_in_period(start_date, end_date)
            if count > 0:
                ranked.append((user, count))
        return ranked

    @staticmethod
    def _format_count_delta_label(
        current_count: int,
        previous_count: int,
        comparison_label: str = "上一周期",
    ) -> tuple[str, str]:
        delta = current_count - previous_count
        if delta == 0:
            return f"相比{comparison_label}持平", "flat"

        percent = round(abs(delta) / previous_count * 100)
        if delta > 0:
            return f"相比{comparison_label}增长{percent}%", "up"
        return f"相比{comparison_label}下降{percent}%", "down"

    @staticmethod
    def _format_rank_delta_label(
        current_rank: int,
        previous_rank: Optional[int],
    ) -> str:
        if not previous_rank:
            return ""

        delta = previous_rank - current_rank
        if delta > 0:
            return f"名次上升{delta}位"
        if delta < 0:
            return f"名次下降{abs(delta)}位"
        return "名次不变"

    def _apply_total_rank_trends(
        self,
        filtered_data: List[tuple],
        trend_days: int,
    ) -> None:
        today = date.today()
        for user, _ in filtered_data:
            daily_counts = [
                user.get_message_count_in_period(
                    today - timedelta(days=offset),
                    today - timedelta(days=offset),
                )
                for offset in range(trend_days - 1, -1, -1)
            ]
            trend_type = self._classify_recent_activity_trend(daily_counts)
            user.display_trend_type = trend_type
            user.display_trend_label = {
                "up": "近期趋势：上升",
                "down": "近期趋势：下降",
                "flat": "近期趋势：平稳",
            }.get(trend_type)

    @staticmethod
    def _classify_recent_activity_trend(daily_counts: List[int]) -> str:
        if len(daily_counts) < 2 or sum(daily_counts) == 0:
            return "flat"

        midpoint = len(daily_counts) // 2
        early = daily_counts[:midpoint]
        recent = daily_counts[-midpoint:]
        early_avg = sum(early) / len(early)
        recent_avg = sum(recent) / len(recent)
        threshold = max(1.0, (sum(daily_counts) / len(daily_counts)) * 0.2)

        if recent_avg - early_avg > threshold:
            return "up"
        if early_avg - recent_avg > threshold:
            return "down"
        return "flat"

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
            return f"[{now.year}年{now.month}月{now.day}日]发言榜单"
        elif rank_type == RankType.YESTERDAY:
            yesterday = now - timedelta(days=1)
            return f"[{yesterday.year}年{yesterday.month}月{yesterday.day}日]昨日发言榜单"
        elif rank_type == RankType.WEEKLY:
            # 计算周数
            week_num = now.isocalendar().week
            return f"[{now.year}年{now.month}月第{week_num}周]发言榜单"
        elif rank_type == RankType.MONTHLY:
            return f"[{now.year}年{now.month}月]发言榜单"
        elif rank_type == RankType.YEARLY:
            return f"[{now.year}年]发言榜单"
        elif rank_type == RankType.LAST_YEAR:
            last_year = now.year - 1
            return f"[{last_year}年]发言榜单"
        else:
            return "发言榜单"
    
    async def _ensure_nicknames_for_push(self, group_id: str, group_data):
        """定时/手动推送前补齐缺失昵称"""
        changed = False
        for user in group_data:
            nickname = str(user.nickname).strip() if user.nickname is not None else ""
            if nickname:
                continue
            user.nickname = f"用户{user.user_id}"
            changed = True
        if changed:
            await self.data_manager.save_group_data(group_id, group_data)
            self.logger.info(f"群组 {group_id} 推送前已补齐缺失昵称")

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
                "task_exists": any(not task.done() for task in self._timer_tasks) if self._timer_tasks else (self.timer_task is not None and not self.timer_task.done()),
                "task_count": len([task for task in self._timer_tasks if not task.done()]),
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
                seen_pushes = set()
                for task_config in self._get_enabled_timer_tasks(config):
                    for target_group in task_config.target_groups:
                        group_id_str = str(target_group).strip()
                        actual_group_id = group_id_str
                        if ':' in group_id_str:
                            try:
                                extracted = group_id_str.rsplit(':', 1)[-1]
                                if extracted.lstrip('-').isdigit():
                                    actual_group_id = extracted
                            except (AttributeError, IndexError, ValueError):
                                pass
                        push_key = (actual_group_id, task_config.rank_type)
                        if push_key in seen_pushes:
                            continue
                        seen_pushes.add(push_key)
                        if await self._push_to_group(actual_group_id, config, task_config.rank_type):
                            success_count += 1
                
                return success_count > 0
                
        except (OSError, IOError, RuntimeError, ValueError, TypeError) as e:
            # 捕获手动推送时的系统、运行时、数值和类型错误
            self.logger.error(f"手动推送失败: {e}")
            return False
    
    async def update_config(self, config, group_unified_msg_origins: Dict[str, str] = None, force_restart: bool = False) -> bool:
        """更新定时配置
        
        默认只更新 unified_msg_origin 映射表，不会重复启动已运行的定时任务。
        如果定时任务需要重启（如配置变更），调用方传入 force_restart=True。
        
        Args:
            config: 新的插件配置对象
            group_unified_msg_origins: 新的群组unified_msg_origin映射表
            force_restart: 是否强制重启正在运行的定时任务
            
        Returns:
            bool: 更新是否成功
        """
        try:
            # 更新群组unified_msg_origin映射表
            if group_unified_msg_origins is not None and self.push_service:
                self.push_service.group_unified_msg_origins = group_unified_msg_origins
                self.group_unified_msg_origins = group_unified_msg_origins
            
            # 检查定时任务状态
            is_running = self.status == TimerTaskStatus.RUNNING and any(not task.done() for task in self._timer_tasks)
            if not is_running:
                is_running = self.status == TimerTaskStatus.RUNNING and self.timer_task and not self.timer_task.done()
            
            # 如果定时功能被禁用，停止已运行的任务
            if not self._get_enabled_timer_tasks(config):
                if is_running:
                    await self.stop_timer()
                    self.logger.info("定时功能已禁用，定时任务已停止")
                return True
            
            if is_running and not force_restart:
                self.logger.debug("定时任务正在运行，仅刷新 unified_msg_origin 映射表")
                return True

            # 配置变更时，重启任务以应用 template_list 中的时间/群组/类型
            if is_running and force_restart:
                await self.stop_timer()
                is_running = False

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
