"""
数据管理模块

提供群组数据的存储、读取、更新和管理功能。
支持异步操作和缓存机制。
"""

import json
import traceback
from pathlib import Path
from typing import List, Optional, Dict, Any
import aiofiles
import asyncio
from datetime import datetime
from cachetools import TTLCache

from .models import UserData, PluginConfig


class DataManager:
    """数据管理器
    
    负责插件的数据存储、读取、缓存和管理。支持群组数据管理、配置管理、缓存管理等功能。
    
    主要功能:
        - 群组数据的增删改查
        - 插件配置的读取和保存
        - 多层缓存机制（数据缓存、配置缓存、图片缓存）
        - 数据导入导出功能
        - 旧数据清理和维护
        - 异步文件操作
        
    Attributes:
        data_dir (Path): 数据根目录路径
        groups_dir (Path): 群组数据目录路径
        cache_dir (Path): 缓存目录路径
        config_file (Path): 配置文件路径
        logger: 日志记录器
        data_cache (TTLCache): 数据缓存，5分钟TTL
        config_cache (TTLCache): 配置缓存，1分钟TTL
        
    Example:
        >>> dm = DataManager("/path/to/data")
        >>> await dm.initialize()
        >>> users = await dm.get_group_data("123456789")
    """
    
    def __init__(self, data_dir: str = "data"):
        """初始化数据管理器

        Args:
            data_dir (str): 数据目录路径，默认为"data"
        """
        self.data_dir = Path(data_dir)
        self.groups_dir = self.data_dir / "groups"
        self.cache_dir = self.data_dir / "cache" / "rank_images"
        self.config_file = self.data_dir / "config.json"
        self.logger = self._get_logger()
        
        # 缓存设置 - 优化TTL设置
        self.data_cache = TTLCache(maxsize=1000, ttl=300)  # 5分钟缓存，提高性能
        self.config_cache = TTLCache(maxsize=10, ttl=60)  # 1分钟缓存，平衡实时性和性能
        
        # 确保目录存在
        self._ensure_directories()
    
    def _get_logger(self):
        """获取日志记录器"""
        import logging
        return logging.getLogger(__name__)
    
    def _ensure_directories(self):
        """确保所有必要的目录存在"""
        directories = [self.data_dir, self.groups_dir, self.cache_dir]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    
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
        if not self.config_file.exists():
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
    
    def _repair_corrupted_json(self, content: str) -> Optional[List[Dict]]:
        """尝试修复损坏的JSON数据
        
        Args:
            content (str): 损坏的JSON内容
            
        Returns:
            Optional[List[Dict]]: 修复后的数据，如果无法修复则返回None
        """
        try:
            # 尝试找到最后一个完整的JSON对象
            bracket_count = 0
            last_valid_pos = 0
            
            for i, char in enumerate(content):
                if char == '[':
                    bracket_count += 1
                elif char == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        last_valid_pos = i + 1
            
            if last_valid_pos > 0:
                # 提取有效的JSON部分
                valid_content = content[:last_valid_pos]
                return json.loads(valid_content)
            
        except (json.JSONDecodeError, IndexError):
            pass
        
        # 如果无法修复，返回空列表
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
                json_content = json.dumps(data, ensure_ascii=False, indent=2)
                await f.write(json_content)
            
            # 原子性移动到目标文件
            temp_file.replace(file_path)
            return True
            
        except Exception as e:
            self.logger.error(f"安全保存文件失败: {e}")
            # 清理临时文件
            temp_file = file_path.with_suffix('.tmp')
            if temp_file.exists():
                temp_file.unlink()
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
        
        file_path = self.groups_dir / f"{group_id}.json"
        
        try:
            if file_path.exists():
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                
                # 验证JSON格式
                if not self._validate_json_content(content):
                    self.logger.warning(f"群组 {group_id} 数据文件格式异常，尝试修复")
                    
                    # 尝试修复损坏的数据
                    repaired_data = self._repair_corrupted_json(content)
                    if repaired_data is not None:
                        # 修复成功，重新保存数据
                        self.logger.info(f"群组 {group_id} 数据修复成功")
                        await self._save_json_safely(file_path, repaired_data)
                        data_list = repaired_data
                    else:
                        # 修复失败，使用空数据
                        self.logger.error(f"群组 {group_id} 数据无法修复，使用空数据")
                        data_list = []
                else:
                    data_list = json.loads(content)
                
                # 转换为UserData对象
                users = []
                for user_data in data_list:
                    try:
                        user = UserData.from_dict(user_data)
                        users.append(user)
                    except (KeyError, TypeError, ValueError) as e:
                        self.logger.warning(f"跳过无效的用户数据: {e}")
                        continue
                
                # 缓存结果
                self.data_cache[cache_key] = users
                return users
            else:
                return []
        
        except (IOError, OSError) as e:
            self.logger.error(f"读取群组 {group_id} 文件失败: {e}")
            return []
        except json.JSONDecodeError as e:
            self.logger.error(f"解析群组 {group_id} JSON数据失败: {e}")
            return []
        except Exception as e:
            self.logger.error(f"读取群组 {group_id} 数据失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            return []
    
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
            IOError: 当文件写入失败时抛出
            OSError: 当文件系统操作失败时抛出
        """
        if not group_id.isdigit():
            raise ValueError(f"群组ID必须是数字字符串，当前值: {group_id}")
        
        file_path = self.groups_dir / f"{group_id}.json"
        
        try:
            # 转换为字典列表
            data_list = [user.to_dict() for user in users]
            
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 安全保存数据
            success = await self._save_json_safely(file_path, data_list)
            
            if success:
                # 清除缓存
                cache_key = f"group_data_{group_id}"
                if cache_key in self.data_cache:
                    del self.data_cache[cache_key]
                
                self.logger.info(f"群组 {group_id} 数据已安全保存，共 {len(users)} 个用户")
            else:
                self.logger.error(f"群组 {group_id} 数据保存失败")
        
        except Exception as e:
            self.logger.error(f"保存群组 {group_id} 数据失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
    
    async def update_user_message(self, group_id: str, user_id: str, nickname: str) -> bool:
        """更新用户消息统计
        
        异步更新指定用户在群组中的消息统计，包括新增用户和更新现有用户。
        
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
        
        try:
            # 获取现有数据
            users = await self.get_group_data(group_id)
            current_timestamp = int(datetime.now().timestamp())
            
            # 查找用户
            user_found = False
            for user in users:
                if user.user_id == user_id:
                    # 更新现有用户 - 使用add_message方法正确记录历史
                    from .models import MessageDate
                    today = datetime.now().date()
                    message_date = MessageDate.from_date(today)
                    user.add_message(message_date)
                    user.last_message_time = current_timestamp
                    if user.first_message_time is None:
                        user.first_message_time = current_timestamp
                    user_found = True
                    break
            
            # 如果用户不存在，创建新用户
            if not user_found:
                new_user = UserData(
                    user_id=user_id,
                    nickname=nickname,
                    message_count=1,
                    first_message_time=current_timestamp,
                    last_message_time=current_timestamp
                )
                # 为新用户添加第一条消息记录
                from .models import MessageDate
                today = datetime.now().date()
                message_date = MessageDate.from_date(today)
                new_user.add_message(message_date)
                users.append(new_user)
            
            # 保存更新后的数据
            await self.save_group_data(group_id, users)
            return True
            
        except Exception as e:
            self.logger.error(f"更新用户 {user_id} 消息统计失败: {e}")
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
            
            if file_path.exists():
                file_path.unlink()
            
            # 清除缓存
            cache_key = f"group_data_{group_id}"
            if cache_key in self.data_cache:
                del self.data_cache[cache_key]
            
            self.logger.info(f"群组 {group_id} 数据已清空")
            return True
            
        except Exception as e:
            self.logger.error(f"清空群组 {group_id} 数据失败: {e}")
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
        except Exception as e:
            self.logger.error(f"获取用户 {user_id} 在群组 {group_id} 中的信息失败: {e}")
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
        except Exception as e:
            self.logger.error(f"获取群组列表失败: {e}")
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
            if self.config_file.exists():
                async with aiofiles.open(self.config_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    config_data = json.loads(content)
                
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
        try:
            config_data = config.to_dict()
            
            async with aiofiles.open(self.config_file, 'w', encoding='utf-8') as f:
                json_content = json.dumps(config_data, ensure_ascii=False, indent=2)
                await f.write(json_content)
            
            # 清除配置缓存
            cache_key = "plugin_config"
            if cache_key in self.config_cache:
                del self.config_cache[cache_key]
            
            self.logger.info("插件配置已保存")
            
        except Exception as e:
            self.logger.error(f"保存配置文件失败: {e}")
            raise
    
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
            
        except Exception as e:
            self.logger.error(f"更新配置失败: {e}")
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
        except Exception as e:
            self.logger.error(f"获取缓存图片失败: {e}")
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
        except Exception as e:
            self.logger.error(f"缓存图片失败: {e}")
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
                
        except Exception as e:
            self.logger.error(f"清空缓存失败: {e}")
    
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
        except Exception as e:
            self.logger.error(f"获取缓存统计失败: {e}")
            return {}
    
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
        except Exception as e:
            self.logger.error(f"获取群组 {group_id} 统计信息失败: {e}")
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
            # 按消息数降序排序
            sorted_users = sorted(users, key=lambda x: x.message_count, reverse=True)
            return sorted_users[:limit]
        except Exception as e:
            self.logger.error(f"获取群组 {group_id} 排行榜失败: {e}")
            return []
    
    async def get_users_by_time_period(self, group_id: str, period: str) -> List[UserData]:
        """按时间段获取用户
        
        根据时间段获取活跃用户列表。
        
        Args:
            group_id (str): 群组ID
            period (str): 时间段，'day', 'week', 'month'
            
        Returns:
            List[UserData]: 符合条件的用户列表
        """
        try:
            users = await self.get_group_data(group_id)
            
            # 这里可以根据需要实现时间段过滤逻辑
            # 目前返回所有用户
            return users
        except Exception as e:
            self.logger.error(f"获取群组 {group_id} 时间段用户失败: {e}")
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
                "export_time": asyncio.get_event_loop().time(),
                "users": [user.to_dict() for user in users],
                "statistics": await self.get_group_statistics(group_id)
            }
        except Exception as e:
            self.logger.error(f"导出群组 {group_id} 数据失败: {e}")
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
            users_data = data.get("users", [])
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
            
        except Exception as e:
            self.logger.error(f"导入群组 {group_id} 数据失败: {e}")
            return False
    
    async def cleanup_old_data(self, days: int = 30):
        """清理旧数据
        
        清理指定天数之前的群组数据。
        
        Args:
            days (int): 保留天数，默认为30天
        """
        try:
            import time
            current_time = time.time()
            cutoff_time = current_time - (days * 24 * 60 * 60)
            
            cleaned_count = 0
            
            for group_file in self.groups_dir.glob("*.json"):
                try:
                    # 检查文件最后修改时间
                    if group_file.stat().st_mtime < cutoff_time:
                        group_id = group_file.stem
                        await self.clear_group_data(group_id)
                        cleaned_count += 1
                        self.logger.info(f"已清理群组 {group_id} 的旧数据")
                except Exception as e:
                    self.logger.error(f"清理群组 {group_file.stem} 数据失败: {e}")
            
            self.logger.info(f"数据清理完成，共清理 {cleaned_count} 个群组")
            
        except Exception as e:
            self.logger.error(f"数据清理失败: {e}")
    
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
            
            if not source_file.exists():
                self.logger.warning(f"群组 {group_id} 数据文件不存在，无法备份")
                return None
            
            # 创建备份目录
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            
            # 生成备份文件名（包含时间戳）
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = backup_dir / f"{group_id}_{timestamp}.json"
            
            # 复制文件
            async with aiofiles.open(source_file, 'r', encoding='utf-8') as src:
                content = await src.read()
            
            async with aiofiles.open(backup_file, 'w', encoding='utf-8') as dst:
                await dst.write(content)
            
            self.logger.info(f"群组 {group_id} 数据已备份到: {backup_file}")
            return backup_file
            
        except Exception as e:
            self.logger.error(f"备份群组 {group_id} 数据失败: {e}")
            return None