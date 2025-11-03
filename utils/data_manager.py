"""
数据管理模块

提供群组数据的存储、读取、更新和管理功能。
支持异步操作和缓存机制。
"""

import json
import re
import traceback
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
import aiofiles
import asyncio
from datetime import datetime, timedelta
from cachetools import TTLCache
from astrbot.api import logger as astrbot_logger
from collections import defaultdict

from .models import UserData, PluginConfig, MessageDate
from .data_stores import GroupDataStore, ConfigManager, PluginCache

# 常量定义
DATA_CACHE_MAXSIZE = 1000
DATA_CACHE_TTL = 300  # 5分钟
CONFIG_CACHE_MAXSIZE = 10
CONFIG_CACHE_TTL = 60  # 1分钟


class DataManager:
    """数据管理器（重构版本）
    
    协调各个专门的组件，提供统一的数据管理接口。
    将原来的单一职责拆分为多个专门的组件：
    - GroupDataStore: 群组数据存储
    - ConfigManager: 配置管理
    - PluginCache: 缓存管理
    
    Example:
        >>> dm = DataManager("/path/to/data")
        >>> await dm.initialize()
        >>> users = await dm.get_group_data("123456789")
    """
    
    def __init__(self, data_dir: Optional[str] = None):
        """初始化数据管理器

        Args:
            data_dir (str): 数据目录路径，由StarTools.get_data_dir()确定
        """
        if data_dir is None:
            raise ValueError("data_dir不能为None。请在插件初始化时提供正确的数据目录路径")
        else:
            self.data_dir = Path(data_dir)
        
        # 初始化各个组件
        self.groups_dir = self.data_dir / "groups"
        self.config_file = self.data_dir / "config.json"
        self.cache_dir = self.data_dir / "cache" / "rank_images"
        
        self.logger = astrbot_logger
        
        # 初始化各个专门的组件
        self.group_store = GroupDataStore(self.groups_dir, self.logger)
        self.config_manager = ConfigManager(self.config_file, self.logger)
        self.cache_manager = PluginCache(self.logger)
        
        # 群组级别的锁机制，防止并发安全问题
        self._group_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        
        # 确保目录存在
        self._ensure_directories()
        
        # 获取缓存实例的引用，方便使用
        self.data_cache = self.cache_manager.get_data_cache()
        self.config_cache = self.cache_manager.get_config_cache()
    
    async def _ensure_directories(self):
        """确保所有必要的目录存在"""
        directories = [self.data_dir, self.groups_dir, self.cache_dir]
        for directory in directories:
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    
    async def initialize(self):
        """初始化数据管理器
        
        异步初始化数据管理器，创建默认配置文件（如果不存在）。
        
        Returns:
            None: 无返回值，初始化结果通过日志输出
            
        Raises:
            OSError: 当目录创建失败时抛出
            IOError: 当配置文件读写失败时抛出
            
        Example:
            >>> dm = DataManager()
            >>> await dm.initialize()
            # 将创建默认配置文件
        """
        self.logger.info("数据管理器初始化中...")
        
        # 创建默认配置
        if not await asyncio.to_thread(self.config_file.exists):
            await self._create_default_config()
        
        self.logger.info("数据管理器初始化完成")
    
    async def _create_default_config(self):
        """创建默认配置
        
        创建插件的默认配置文件，包含所有必要的配置项和默认值。
        
        Returns:
            None: 无返回值，配置文件创建结果通过日志输出
            
        Raises:
            IOError: 当文件写入失败时抛出
            
        Example:
            >>> await self._create_default_config()
            # 将在data目录下创建config.json文件
        """
        default_config = PluginConfig()
        await self.save_config(default_config)
        self.logger.info("已创建默认配置文件")
    
    def _validate_json_content(self, content: str) -> bool:
        """验证JSON内容格式
        
        Args:
            content (str): JSON内容字符串
            
        Returns:
            bool: 格式是否有效
        """
        try:
            json.loads(content)
            return True
        except json.JSONDecodeError:
            return False
    
    async def _repair_corrupted_json(self, file_path: Path, content: str) -> List[Dict]:
        """尝试修复损坏的JSON数据
        
        稳健的修复策略：
        1. 尝试简单修复（清理多余逗号）
        2. 如果失败，备份损坏的文件并创建新的空数据文件
        
        Args:
            file_path (Path): 文件路径，用于备份
            content (str): 损坏的JSON内容
            
        Returns:
            List[Dict]: 修复后的数据（空列表表示创建了新文件）
        """
        try:
            # 首先尝试直接解析
            return await asyncio.to_thread(json.loads, content)
        except json.JSONDecodeError:
            pass
        
        try:
            # 尝试简单修复：清理多余的逗号
            cleaned_content = re.sub(r',(\s*[}\]])', r'\1', content)
            return await asyncio.to_thread(json.loads, cleaned_content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 简单修复失败，采用稳健策略：备份并重建
            self.logger.warning(f"JSON文件 {file_path} 损坏，创建备份并重建")
            
            try:
                # 创建备份文件
                backup_path = file_path.with_suffix('.backup')
                async with aiofiles.open(backup_path, 'w', encoding='utf-8') as f:
                    await f.write(content)
                
                # 创建新的空数据文件
                async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                    await f.write('[]')
                
                self.logger.info(f"已备份损坏文件到 {backup_path}，并创建新的空数据文件")
                return []
                
            except (IOError, OSError, json.JSONDecodeError) as e:
                self.logger.error(f"备份和重建文件失败: {e}")
                return []
    
    async def _save_json_safely(self, file_path: Path, data: List[Dict]) -> bool:
        """安全地保存JSON数据
        
        使用临时文件确保原子性写入。
        
        Args:
            file_path (Path): 目标文件路径
            data (List[Dict]): 要保存的数据
            
        Returns:
            bool: 保存是否成功
        """
        try:
            # 创建临时文件
            temp_file = file_path.with_suffix('.tmp')
            
            # 写入临时文件
            async with aiofiles.open(temp_file, 'w', encoding='utf-8') as f:
                json_content = await asyncio.to_thread(json.dumps, data, ensure_ascii=False, indent=2)
                await f.write(json_content)
            
            # 原子性移动到目标文件
            await asyncio.to_thread(temp_file.replace, file_path)
            return True
            
        except (IOError, OSError) as e:
            self.logger.error(f"安全保存文件失败: {e}")
            # 清理临时文件
            temp_file = file_path.with_suffix('.tmp')
            if await asyncio.to_thread(temp_file.exists):
                await asyncio.to_thread(temp_file.unlink)
            return False
    
    # ========== 群组数据管理 ==========
    
    async def get_group_data(self, group_id: str) -> List[UserData]:
        """获取群组数据
        
        从缓存或文件读取指定群组的用户数据。
        
        Args:
            group_id (str): 群组ID，必须是有效的数字字符串
            
        Returns:
            List[UserData]: 用户数据列表，如果读取失败则返回空列表
            
        Raises:
            ValueError: 当group_id格式不正确时
        """
        if not group_id.isdigit():
            raise ValueError(f"群组ID必须是数字字符串，当前值: {group_id}")
        
        cache_key = f"group_data_{group_id}"
        
        # 检查缓存
        if cache_key in self.data_cache:
            return self.data_cache[cache_key]
        
        # 使用GroupDataStore加载数据
        users = await self.group_store.load_group_data(group_id)
        
        # 缓存结果
        self.data_cache[cache_key] = users
        return users
    
    async def save_group_data(self, group_id: str, users: List[UserData]):
        """保存群组数据
        
        异步保存指定群组的用户数据到JSON文件，并清除相关缓存。
        
        Args:
            group_id (str): 群组ID，必须是有效的数字字符串
            users (List[UserData]): 用户数据列表，将被序列化为JSON格式保存
            
        Returns:
            None: 无返回值，保存结果通过日志输出
            
        Raises:
            ValueError: 当group_id格式不正确时
        """
        if not group_id.isdigit():
            raise ValueError(f"群组ID必须是数字字符串，当前值: {group_id}")
        
        # 使用GroupDataStore保存数据
        success = await self.group_store.save_group_data(group_id, users)
        
        if success:
            # 清除缓存
            cache_key = f"group_data_{group_id}"
            if cache_key in self.data_cache:
                del self.data_cache[cache_key]
            
            self.logger.info(f"群组 {group_id} 数据已安全保存，共 {len(users)} 个用户")
        else:
            self.logger.error(f"群组 {group_id} 数据保存失败")
    
    async def update_user_message(self, group_id: str, user_id: str, nickname: str) -> bool:
        """更新用户消息统计
        
        异步更新指定用户在群组中的消息统计，包括新增用户和更新现有用户。
        使用基于群组ID的锁机制防止并发安全问题。
        
        Args:
            group_id (str): 群组ID
            user_id (str): 用户ID
            nickname (str): 用户昵称
            
        Returns:
            bool: 更新是否成功
            
        Raises:
            ValueError: 当参数格式不正确时
        """
        if not group_id.isdigit():
            raise ValueError(f"群组ID必须是数字字符串，当前值: {group_id}")
        
        if not user_id.isdigit():
            raise ValueError(f"用户ID必须是数字字符串，当前值: {user_id}")
        
        # 获取群组级别的锁，确保同一群组的数据操作串行化
        group_lock = self._group_locks[group_id]
        
        async with group_lock:
            try:
                # 获取现有数据
                users = await self.get_group_data(group_id)
                current_timestamp = int(datetime.now().timestamp())
                
                # 优化性能：将用户列表转换为字典，以 user_id 为键
                users_dict = {user.user_id: user for user in users}
                
                # 查找用户（O(1) 操作）
                if user_id in users_dict:
                    # 更新现有用户 - 使用add_message方法正确记录历史
                    user = users_dict[user_id]
                    today = datetime.now().date()
                    message_date = MessageDate.from_date(today)
                    user.add_message(message_date)
                    user.last_message_time = current_timestamp
                    if user.first_message_time is None:
                        user.first_message_time = current_timestamp
                else:
                    # 如果用户不存在，创建新用户
                    # 为新用户创建消息记录
                    today = datetime.now().date()
                    message_date = MessageDate.from_date(today)
                    
                    new_user = UserData(
                        user_id=user_id,
                        nickname=nickname,
                        message_count=0,  # 先设为0，add_message会增加到1
                        first_message_time=current_timestamp,
                        last_message_time=current_timestamp
                    )
                    # 添加第一条消息记录（这会将message_count增加到1）
                    new_user.add_message(message_date)
                    users_dict[user_id] = new_user
                
                # 将字典转换回列表
                updated_users = list(users_dict.values())
                
                # 保存更新后的数据
                await self.save_group_data(group_id, updated_users)
                return True
                
            except (IOError, OSError) as e:
                self.logger.error(f"更新用户 {user_id} 消息统计时文件操作失败: {e}")
                return False
            except (KeyError, TypeError, ValueError) as e:
                self.logger.error(f"用户 {user_id} 数据格式错误: {e}")
                return False
            except asyncio.TimeoutError as e:
                self.logger.error(f"更新用户 {user_id} 消息统计超时: {e}")
                return False
            except Exception as e:
                self.logger.error(f"更新用户 {user_id} 消息统计时发生未知错误: {e}")
                return False
    
    async def clear_group_data(self, group_id: str) -> bool:
        """清空群组数据
        
        删除指定群组的所有数据。
        
        Args:
            group_id (str): 群组ID
            
        Returns:
            bool: 操作是否成功
        """
        try:
            file_path = self.groups_dir / f"{group_id}.json"
            
            if await aiofiles.os.path.exists(file_path):
                await aiofiles.os.remove(file_path)
            
            # 清除缓存
            cache_key = f"group_data_{group_id}"
            if cache_key in self.data_cache:
                del self.data_cache[cache_key]
            
            self.logger.info(f"群组 {group_id} 数据已清空")
            return True
            
        except (IOError, OSError) as e:
            self.logger.error(f"清空群组 {group_id} 文件操作失败: {e}")
            return False
        except Exception as e:
            self.logger.error(f"清空群组 {group_id} 数据时发生未知错误: {e}")
            return False
    
    async def get_user_in_group(self, group_id: str, user_id: str) -> Optional[UserData]:
        """获取群组中的用户信息
        
        获取指定用户在群组中的详细信息。
        
        Args:
            group_id (str): 群组ID
            user_id (str): 用户ID
            
        Returns:
            Optional[UserData]: 用户信息，如果用户不存在则返回None
        """
        try:
            users = await self.get_group_data(group_id)
            for user in users:
                if user.user_id == user_id:
                    return user
            return None
        except (IOError, OSError) as e:
            self.logger.error(f"获取用户 {user_id} 在群组 {group_id} 中的信息时文件操作失败: {e}")
            return None
        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"用户 {user_id} 数据格式错误: {e}")
            return None
        except Exception as e:
            self.logger.error(f"获取用户 {user_id} 在群组 {group_id} 中的信息时发生未知错误: {e}")
            return None
    
    async def get_all_groups(self) -> List[str]:
        """获取所有群组ID列表
        
        扫描数据目录，返回所有已记录群组的ID列表。
        
        Returns:
            List[str]: 群组ID列表
        """
        try:
            group_files = list(self.groups_dir.glob("*.json"))
            group_ids = [file.stem for file in group_files if file.is_file()]
            return group_ids
        except (IOError, OSError) as e:
            self.logger.error(f"获取群组列表时文件操作失败: {e}")
            return []
        except Exception as e:
            self.logger.error(f"获取群组列表时发生未知错误: {e}")
            return []
    
    # ========== 配置管理 ==========
    
    async def get_config(self) -> PluginConfig:
        """获取插件配置
        
        从配置文件读取插件配置，支持缓存机制。
        
        Returns:
            PluginConfig: 插件配置对象
            
        Raises:
            IOError: 当配置文件读取失败时抛出
            json.JSONDecodeError: 当配置文件格式错误时抛出
        """
        cache_key = "plugin_config"
        
        # 检查缓存
        if cache_key in self.config_cache:
            return self.config_cache[cache_key]
        
        try:
            if await asyncio.to_thread(self.config_file.exists):
                async with aiofiles.open(self.config_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    config_data = await asyncio.to_thread(json.loads, content)
                
                config = PluginConfig.from_dict(config_data)
                
                # 缓存配置
                self.config_cache[cache_key] = config
                return config
            else:
                # 如果配置文件不存在，创建默认配置
                default_config = PluginConfig()
                await self.save_config(default_config)
                return default_config
                
        except (IOError, OSError, json.JSONDecodeError) as e:
            self.logger.error(f"读取配置文件失败: {e}")
            # 返回默认配置
            return PluginConfig()
    
    async def save_config(self, config: PluginConfig):
        """保存插件配置
        
        将插件配置保存到配置文件，并清除配置缓存。
        
        Args:
            config (PluginConfig): 要保存的配置对象
            
        Raises:
            IOError: 当配置文件写入失败时抛出
        """
        # 使用ConfigManager保存配置
        success = await self.config_manager.save_config(config)
        
        if success:
            # 清除配置缓存
            cache_key = "plugin_config"
            if cache_key in self.config_cache:
                del self.config_cache[cache_key]
            
            self.logger.info("插件配置已保存")
        else:
            raise IOError("保存配置文件失败")
    
    async def update_config(self, updates: Dict[str, Any]) -> bool:
        """更新配置
        
        更新插件配置的指定字段。
        
        Args:
            updates (Dict[str, Any]): 配置更新字典
            
        Returns:
            bool: 更新是否成功
        """
        try:
            config = await self.get_config()
            
            # 更新配置字段
            for key, value in updates.items():
                if hasattr(config, key):
                    setattr(config, key, value)
                else:
                    self.logger.warning(f"未知的配置项: {key}")
            
            # 保存更新后的配置
            await self.save_config(config)
            return True
            
        except (IOError, OSError) as e:
            self.logger.error(f"更新配置时文件操作失败: {e}")
            return False
        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"配置数据格式错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"更新配置时发生未知错误: {e}")
            return False
    
    # ========== 缓存管理 ==========
    
    async def get_cached_image(self, cache_key: str) -> Optional[str]:
        """获取缓存图片
        
        根据缓存键获取已缓存的图片路径。
        
        Args:
            cache_key (str): 缓存键
            
        Returns:
            Optional[str]: 图片路径，如果缓存不存在则返回None
        """
        try:
            image_cache_key = f"image_{cache_key}"
            if image_cache_key in self.data_cache:
                return self.data_cache[image_cache_key]
            return None
        except (KeyError, TypeError) as e:
            self.logger.error(f"缓存键格式错误: {e}")
            return None
        except Exception as e:
            self.logger.error(f"获取缓存图片时发生未知错误: {e}")
            return None
    
    async def cache_image(self, cache_key: str, image_path: str) -> bool:
        """缓存图片
        
        将生成的图片路径缓存起来。
        
        Args:
            cache_key (str): 缓存键
            image_path (str): 图片路径
            
        Returns:
            bool: 缓存是否成功
        """
        try:
            image_cache_key = f"image_{cache_key}"
            self.data_cache[image_cache_key] = image_path
            return True
        except (KeyError, TypeError) as e:
            self.logger.error(f"缓存键格式错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"缓存图片时发生未知错误: {e}")
            return False
    
    async def clear_cache(self, cache_type: str = "all"):
        """清空缓存
        
        清空指定类型的缓存。
        
        Args:
            cache_type (str): 缓存类型，'data', 'config', 'image', 或 'all'
        """
        try:
            if cache_type in ["all", "data"]:
                self.data_cache.clear()
                self.logger.info("数据缓存已清空")
            
            if cache_type in ["all", "config"]:
                self.config_cache.clear()
                self.logger.info("配置缓存已清空")
                
            if cache_type in ["all", "image"]:
                # 清除图片缓存
                image_keys = [key for key in self.data_cache.keys() if key.startswith("image_")]
                for key in image_keys:
                    del self.data_cache[key]
                self.logger.info("图片缓存已清空")
                
        except (KeyError, TypeError) as e:
            self.logger.error(f"缓存操作参数错误: {e}")
        except Exception as e:
            self.logger.error(f"清空缓存时发生未知错误: {e}")
    
    def _generate_cache_key(self, prefix: str, *args) -> str:
        """生成缓存键
        
        根据前缀和参数生成唯一的缓存键。
        
        Args:
            prefix (str): 前缀
            *args: 参数
            
        Returns:
            str: 生成的缓存键
        """
        return f"{prefix}_{'_'.join(str(arg) for arg in args)}"
    
    async def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计
        
        返回缓存的使用统计信息。
        
        Returns:
            Dict[str, Any]: 缓存统计信息
        """
        try:
            return {
                "data_cache_size": len(self.data_cache),
                "data_cache_maxsize": self.data_cache.maxsize,
                "config_cache_size": len(self.config_cache),
                "config_cache_maxsize": self.config_cache.maxsize,
                "total_cache_size": len(self.data_cache) + len(self.config_cache)
            }
        except (KeyError, TypeError, AttributeError) as e:
            self.logger.error(f"缓存统计信息获取错误: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"获取缓存统计时发生未知错误: {e}")
            return {}
    
    # 移除重复的get_cache_statistics方法，统一使用get_cache_stats
    
    # ========== 统计功能 ==========
    
    async def get_group_statistics(self, group_id: str) -> Dict[str, Any]:
        """获取群组统计信息
        
        获取指定群组的详细统计信息。
        
        Args:
            group_id (str): 群组ID
            
        Returns:
            Dict[str, Any]: 群组统计信息
        """
        try:
            users = await self.get_group_data(group_id)
            
            if not users:
                return {
                    "total_users": 0,
                    "total_messages": 0,
                    "active_users": 0,
                    "average_messages": 0,
                    "top_user": None
                }
            
            total_messages = sum(user.message_count for user in users)
            active_users = len([user for user in users if user.message_count > 0])
            top_user = max(users, key=lambda x: x.message_count) if users else None
            
            return {
                "total_users": len(users),
                "total_messages": total_messages,
                "active_users": active_users,
                "average_messages": total_messages / len(users) if users else 0,
                "top_user": {
                    "user_id": top_user.user_id,
                    "nickname": top_user.nickname,
                    "message_count": top_user.message_count
                } if top_user else None
            }
        except (IOError, OSError) as e:
            self.logger.error(f"获取群组 {group_id} 统计信息时文件操作失败: {e}")
            return {
                "total_users": 0,
                "total_messages": 0,
                "active_users": 0,
                "average_messages": 0,
                "top_user": None
            }
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self.logger.error(f"群组 {group_id} 统计数据格式错误: {e}")
            return {
                "total_users": 0,
                "total_messages": 0,
                "active_users": 0,
                "average_messages": 0,
                "top_user": None
            }
        except Exception as e:
            self.logger.error(f"获取群组 {group_id} 统计信息时发生未知错误: {e}")
            return {
                "total_users": 0,
                "total_messages": 0,
                "active_users": 0,
                "average_messages": 0,
                "top_user": None
            }
    
    async def get_top_users(self, group_id: str, limit: int = 10) -> List[UserData]:
        """获取排行榜用户
        
        获取群组中消息数最多的用户列表。
        
        Args:
            group_id (str): 群组ID
            limit (int): 返回用户数量限制
            
        Returns:
            List[UserData]: 排序后的用户列表
        """
        try:
            users = await self.get_group_data(group_id)
            # 过滤掉0次发言的用户，然后按消息数降序排序
            active_users = [user for user in users if user.message_count > 0]
            sorted_users = sorted(active_users, key=lambda x: x.message_count, reverse=True)
            return sorted_users[:limit]
        except (IOError, OSError) as e:
            self.logger.error(f"获取群组 {group_id} 排行榜时文件操作失败: {e}")
            return []
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self.logger.error(f"群组 {group_id} 用户数据格式错误: {e}")
            return []
        except Exception as e:
            self.logger.error(f"获取群组 {group_id} 排行榜时发生未知错误: {e}")
            return []
    
    async def get_users_by_time_period(self, group_id: str, period: str) -> List[tuple]:
        """按时间段获取用户
        
        根据时间段获取活跃用户列表，返回用户对象和该时间段内消息数的元组列表。
        
        Args:
            group_id (str): 群组ID
            period (str): 时间段，'day', 'week', 'month'
            
        Returns:
            List[tuple]: 包含用户对象和消息数的元组列表，格式为[(UserData, count)]，按消息数降序排序
            
        Raises:
            ValueError: 当period参数无效时抛出
        """
        try:
            users = await self.get_group_data(group_id)
            
            if not users:
                return []
            
            # 计算时间范围
            current_date = datetime.now().date()
            
            if period == 'day':
                # 今日用户
                start_date = current_date
                end_date = current_date
            elif period == 'week':
                # 本周用户（从周一开始到当前日期）
                days_since_monday = current_date.weekday()
                start_date = current_date - timedelta(days=days_since_monday)
                end_date = current_date
            elif period == 'month':
                # 本月用户（从月初到当前日期）
                start_date = current_date.replace(day=1)
                end_date = current_date
            else:
                raise ValueError(f"无效的时间段参数: {period}，支持的值为: 'day', 'week', 'month'")
            
            # 过滤在指定时间段内有发言的用户，返回用户和计数的元组列表
            user_count_pairs = []
            for user in users:
                message_count_in_period = user.get_message_count_in_period(start_date, end_date)
                if message_count_in_period > 0:
                    user_count_pairs.append((user, message_count_in_period))
            
            # 按时间段内的消息数降序排序
            user_count_pairs.sort(key=lambda x: x[1], reverse=True)
            
            self.logger.debug(f"群组 {group_id} 在时间段 {period} 内找到 {len(user_count_pairs)} 个活跃用户")
            return user_count_pairs
            
        except ValueError as e:
            # 参数错误，直接抛出
            self.logger.error(f"时间段参数错误: {e}")
            raise
        except (IOError, OSError) as e:
            self.logger.error(f"获取群组 {group_id} 时间段用户时文件操作失败: {e}")
            return []
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self.logger.error(f"群组 {group_id} 用户数据格式错误: {e}")
            return []
        except Exception as e:
            self.logger.error(f"获取群组 {group_id} 时间段用户时发生未知错误: {e}")
            return []
    
    # ========== 数据导入导出 ==========
    
    async def export_group_data(self, group_id: str) -> Optional[Dict[str, Any]]:
        """导出群组数据
        
        将群组数据导出为字典格式。
        
        Args:
            group_id (str): 群组ID
            
        Returns:
            Optional[Dict[str, Any]]: 导出的数据，失败时返回None
        """
        try:
            users = await self.get_group_data(group_id)
            return {
                "group_id": group_id,
                "export_time": time.time(),
                "users": [user.to_dict() for user in users],
                "statistics": await self.get_group_statistics(group_id)
            }
        except (IOError, OSError) as e:
            self.logger.error(f"导出群组 {group_id} 数据时文件操作失败: {e}")
            return None
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self.logger.error(f"群组 {group_id} 数据格式错误: {e}")
            return None
        except Exception as e:
            self.logger.error(f"导出群组 {group_id} 数据时发生未知错误: {e}")
            return None
    
    async def import_group_data(self, group_id: str, data: Dict[str, Any]) -> bool:
        """导入群组数据
        
        从字典格式导入群组数据。
        
        Args:
            group_id (str): 群组ID
            data (Dict[str, Any]): 要导入的数据
            
        Returns:
            bool: 导入是否成功
        """
        try:
            # 处理不同的数据格式
            if isinstance(data, list):
                # 如果数据是列表格式，直接使用
                users_data = data
            elif isinstance(data, dict):
                # 如果数据是字典格式，获取users字段
                users_data = data.get("users", [])
            else:
                # 如果数据格式不正确，抛出异常
                raise ValueError(f"不支持的数据格式: {type(data)}")
            
            users = []
            
            for user_data in users_data:
                try:
                    user = UserData.from_dict(user_data)
                    users.append(user)
                except (KeyError, TypeError, ValueError) as e:
                    self.logger.warning(f"跳过无效的用户数据: {e}")
                    continue
            
            await self.save_group_data(group_id, users)
            self.logger.info(f"群组 {group_id} 数据导入成功，共 {len(users)} 个用户")
            return True
            
        except (IOError, OSError) as e:
            self.logger.error(f"导入群组 {group_id} 数据时文件操作失败: {e}")
            return False
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self.logger.error(f"导入数据格式错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"导入群组 {group_id} 数据时发生未知错误: {e}")
            return False
    
    async def cleanup_old_data(self, days: int = 30):
        """清理旧数据
        
        清理指定天数之前的群组数据。
        
        Args:
            days (int): 保留天数，默认为30天
        """
        try:
            current_time = time.time()
            cutoff_time = current_time - (days * 24 * 60 * 60)
            
            cleaned_count = 0
            
            # 获取所有群组文件
            group_files = list(self.groups_dir.glob("*.json"))
            
            # 并发执行 stat 检查
            async def check_and_clean(file_path):
                try:
                    # 使用 asyncio.to_thread 将同步的 stat() 调用移到工作线程
                    file_stat = await asyncio.to_thread(file_path.stat)
                    if file_stat.st_mtime < cutoff_time:
                        group_id = file_path.stem
                        await self.clear_group_data(group_id)
                        self.logger.info(f"已清理群组 {group_id} 的旧数据")
                        return True
                    return False
                except Exception as e:
                    self.logger.error(f"清理群组 {file_path.stem} 数据失败: {e}")
                    return False
            
            # 并发执行所有文件的检查和清理
            results = await asyncio.gather(*[check_and_clean(f) for f in group_files])
            cleaned_count = sum(results)
            
            self.logger.info(f"数据清理完成，共清理 {cleaned_count} 个群组")
            
        except (IOError, OSError) as e:
            self.logger.error(f"数据清理时文件操作失败: {e}")
        except (ValueError, TypeError) as e:
            self.logger.error(f"数据清理参数错误: {e}")
        except Exception as e:
            self.logger.error(f"数据清理时发生未知错误: {e}")
    
    async def backup_group_data(self, group_id: str) -> Optional[Path]:
        """备份群组数据
        
        为指定群组创建数据备份。
        
        Args:
            group_id (str): 群组ID
            
        Returns:
            Optional[Path]: 备份文件路径，失败时返回None
        """
        try:
            source_file = self.groups_dir / f"{group_id}.json"
            
            if not await asyncio.to_thread(source_file.exists):
                self.logger.warning(f"群组 {group_id} 数据文件不存在，无法备份")
                return None
            
            # 创建备份目录
            backup_dir = self.data_dir / "backups"
            await asyncio.to_thread(backup_dir.mkdir, exist_ok=True)
            
            # 生成备份文件名（包含时间戳）
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = backup_dir / f"{group_id}_{timestamp}.json"
            
            # 复制文件
            async with aiofiles.open(source_file, 'r', encoding='utf-8') as src:
                content = await src.read()
            
            async with aiofiles.open(backup_file, 'w', encoding='utf-8') as dst:
                await dst.write(content)
            
            self.logger.info(f"群组 {group_id} 数据已备份到: {backup_file}")
            return backup_file
            
        except (IOError, OSError) as e:
            self.logger.error(f"备份群组 {group_id} 数据时文件操作失败: {e}")
            return None
        except (ValueError, TypeError) as e:
            self.logger.error(f"备份参数错误: {e}")
            return None
        except Exception as e:
            self.logger.error(f"备份群组 {group_id} 数据时发生未知错误: {e}")
            return None
    
    # 移除重复的方法，统一使用cache_manager的方法