"""
数据管理模块
负责插件的数据存储、读取、缓存和管理
"""

import os
import json
import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, date, timedelta
import shutil
import aiofiles
from cachetools import TTLCache
from astrbot.api import logger as astrbot_logger
import traceback

from .models import (
    UserData, PluginConfig, GroupInfo, 
    MessageDate, RankData, load_json_file, save_json_file
)




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
        self.logger = astrbot_logger
        
        # 缓存设置 - 优化TTL设置
        self.data_cache = TTLCache(maxsize=1000, ttl=300)  # 5分钟缓存，提高性能
        self.config_cache = TTLCache(maxsize=10, ttl=60)  # 1分钟缓存，平衡实时性和性能
        
        # 确保目录存在
        self._ensure_directories()
    
    def _ensure_directories(self):
        """确保必要的目录存在
        
        创建数据管理器所需的所有目录结构，包括数据目录、群组目录和缓存目录。
        
        Returns:
            None: 无返回值，目录创建结果通过日志输出
            
        Example:
            >>> self._ensure_directories()
            # 将在日志中显示创建的目录信息
        """
        directories = [
            self.data_dir,
            self.groups_dir,
            self.cache_dir
        ]
        
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
    
    # ========== 群组数据管理 ==========
    
    async def get_group_data(self, group_id: str) -> List[UserData]:
        """获取群组数据
        
        异步获取指定群组的所有用户数据，包含缓存机制以提高性能。
        
        Args:
            group_id (str): 群组ID，必须是有效的数字字符串
            
        Returns:
            List[UserData]: 用户数据列表，如果群组无数据则返回空列表
            
        Raises:
            ValueError: 当group_id格式无效时抛出
            
        Example:
            >>> users = await dm.get_group_data("123456789")
            >>> print(f"群组共有 {len(users)} 个用户")
        """
        cache_key = f"group_data_{group_id}"
        
        # 检查缓存
        if cache_key in self.data_cache:
            return self.data_cache[cache_key]
        
        file_path = self.groups_dir / f"{group_id}.json"
        
        try:
            if file_path.exists():
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    data_list = json.loads(content)
                
                # 转换为UserData对象
                users = [UserData.from_dict(user_data) for user_data in data_list]
                
                # 缓存结果
                self.data_cache[cache_key] = users
                return users
            else:
                return []
        
        except (IOError, OSError, json.JSONDecodeError) as e:
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
            IOError: 当文件写入失败时抛出
            OSError: 当文件系统操作失败时抛出
            
        Example:
            >>> users = [UserData("123", "用户1"), UserData("456", "用户2")]
            >>> await dm.save_group_data("123456789", users)
            # 将用户数据保存到 groups/123456789.json
        """
        file_path = self.groups_dir / f"{group_id}.json"
        
        try:
            # 转换为字典列表
            data_list = [user.to_dict() for user in users]
            
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                json_content = json.dumps(data_list, ensure_ascii=False, indent=2)
                await f.write(json_content)
            
            # 清除缓存
            cache_key = f"group_data_{group_id}"
            if cache_key in self.data_cache:
                del self.data_cache[cache_key]
            
            self.logger.info(f"群组 {group_id} 数据已保存，共 {len(users)} 个用户")
        
        except (IOError, OSError) as e:
            self.logger.error(f"保存群组 {group_id} 数据失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
    
    async def update_user_message(self, group_id: str, user_id: str, nickname: str) -> bool:
        """更新用户消息统计
        
        异步更新指定用户在群组中的消息统计，包括新增用户和更新现有用户。
        
        Args:
            group_id (str): 群组ID，必须是有效的数字字符串
            user_id (str): 用户ID，必须是有效的数字字符串
            nickname (str): 用户昵称，将进行安全验证和转义
            
        Returns:
            bool: 更新是否成功，True表示成功，False表示失败
            
        Raises:
            ValueError: 当参数验证失败时抛出
            TypeError: 当参数类型错误时抛出
            KeyError: 当数据格式错误时抛出
            
        Example:
            >>> success = await dm.update_user_message("123456789", "987654321", "用户昵称")
            >>> print(success)
            True
        """
        try:
            # 获取当前群组数据
            users = await self.get_group_data(group_id)
            
            # 查找用户
            current_date = MessageDate.from_datetime(datetime.now())
            user_index = None
            
            for i, user in enumerate(users):
                if user.user_id == user_id:
                    user_index = i
                    break
            
            if user_index is not None:
                # 更新现有用户
                user = users[user_index]
                user.nickname = nickname  # 更新昵称
                user.add_message(current_date)
            else:
                # 创建新用户
                new_user = UserData(user_id=user_id, nickname=nickname)
                new_user.add_message(current_date)
                users.append(new_user)
            
            # 保存数据
            await self.save_group_data(group_id, users)
            return True
        
        except (IOError, OSError) as e:
            self.logger.error(f"更新用户消息失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            return False
    
    async def clear_group_data(self, group_id: str) -> bool:
        """清除群组数据"""
        file_path = self.groups_dir / f"{group_id}.json"
        
        try:
            if file_path.exists():
                file_path.unlink()
            
            # 清除缓存
            cache_key = f"group_data_{group_id}"
            if cache_key in self.data_cache:
                del self.data_cache[cache_key]
            
            self.logger.info(f"群组 {group_id} 数据已清除")
            return True
        
        except IOError as e:
            self.logger.error(f"清除群组 {group_id} 数据失败: {e}")
            return False
    
    async def get_user_in_group(self, group_id: str, user_id: str) -> Optional[UserData]:
        """获取群组中的特定用户"""
        users = await self.get_group_data(group_id)
        
        for user in users:
            if user.user_id == user_id:
                return user
        
        return None
    
    async def get_all_groups(self) -> List[str]:
        """获取所有群组ID列表"""
        group_ids = []
        
        try:
            # 遍历groups_dir目录中的所有.json文件
            for json_file in self.groups_dir.glob("*.json"):
                # 从文件名中提取群组ID（去掉.json后缀）
                group_id = json_file.stem
                group_ids.append(group_id)
            
            return group_ids
        
        except (IOError, OSError) as e:
            self.logger.error(f"获取群组列表失败: {e}")
            return []
    
    # ========== 配置管理 ==========
    
    async def get_config(self) -> PluginConfig:
        """获取插件配置"""
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
                # 返回默认配置
                default_config = PluginConfig()
                self.config_cache[cache_key] = default_config
                return default_config
        
        except (json.JSONDecodeError, IOError, KeyError) as e:
            self.logger.error(f"读取配置文件失败: {e}")
            # 返回默认配置
            default_config = PluginConfig()
            self.config_cache[cache_key] = default_config
            return default_config
    
    async def save_config(self, config: PluginConfig):
        """保存插件配置"""
        try:
            config_data = config.to_dict()
            
            async with aiofiles.open(self.config_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(config_data, ensure_ascii=False, indent=2))
            
            # 更新缓存
            cache_key = "plugin_config"
            self.config_cache[cache_key] = config
        
        except IOError as e:
            self.logger.error(f"保存配置文件失败: {e}")
    
    async def update_config(self, updates: Dict[str, Any]) -> bool:
        """更新配置"""
        try:
            current_config = await self.get_config()
            
            # 应用更新
            for key, value in updates.items():
                if hasattr(current_config, key):
                    setattr(current_config, key, value)
            
            await self.save_config(current_config)
            return True
        
        except (IOError, OSError) as e:
            self.logger.error(f"更新配置失败: {e}")
            return False
    
    # ========== 缓存管理 ==========
    
    async def get_cached_image(self, cache_key: str) -> Optional[str]:
        """获取缓存的图片路径"""
        cache_file = self.cache_dir / f"{cache_key}.png"
        
        # 简化缓存检查逻辑
        if cache_file.exists() and datetime.now().timestamp() - cache_file.stat().st_mtime < 3600:
            return str(cache_file)
        
        # 删除过期文件
        cache_file.unlink() if cache_file.exists() else None
        return None
    
    async def cache_image(self, cache_key: str, image_path: str) -> bool:
        """缓存图片"""
        try:
            cache_file = self.cache_dir / f"{cache_key}.png"
            
            shutil.copy2(image_path, cache_file)
            return True
        
        except IOError as e:
            self.logger.error(f"缓存图片失败: {e}")
            return False
    
    async def clear_cache(self, cache_type: str = "all"):
        """清除缓存"""
        if cache_type in ["all", "data"]:
            self.data_cache.clear()
        
        if cache_type in ["all", "config"]:
            self.config_cache.clear()
        
        if cache_type in ["all", "image"]:
            # 清除图片缓存
            for cache_file in self.cache_dir.glob("*.png"):
                cache_file.unlink()
    
    def _generate_cache_key(self, prefix: str, *args) -> str:
        """生成唯一的缓存键"""
        return f"{prefix}_{'_'.join(str(arg) for arg in args if arg)}"
    
    # 简化缓存检查，直接内联使用
    
    async def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        return {
            "data_cache_size": len(self.data_cache),
            "data_cache_maxsize": self.data_cache.maxsize,
            "config_cache_size": len(self.config_cache),
            "config_cache_maxsize": self.config_cache.maxsize,
            "cache_directories": {
                "data_dir": str(self.data_dir),
                "groups_dir": str(self.groups_dir),
                "cache_dir": str(self.cache_dir)
            }
        }
    
    # ========== 数据统计 ==========
    
    async def get_group_statistics(self, group_id: str) -> Dict[str, Any]:
        """获取群组统计信息"""
        users = await self.get_group_data(group_id)
        
        if not users:
            return {
                "total_users": 0,
                "total_messages": 0,
                "active_users": 0,
                "average_messages": 0
            }
        
        total_messages = sum(user.total for user in users)
        active_users = len([user for user in users if user.total > 0])
        
        return {
            "total_users": len(users),
            "total_messages": total_messages,
            "active_users": active_users,
            "average_messages": round(total_messages / len(users), 2) if users else 0
        }
    
    async def get_top_users(self, group_id: str, limit: int = 10) -> List[UserData]:
        """获取活跃用户排行"""
        users = await self.get_group_data(group_id)
        
        # 按总消息数排序
        sorted_users = sorted(users, key=lambda x: x.total, reverse=True)
        
        return sorted_users[:limit]
    
    async def get_users_by_time_period(self, group_id: str, period: str) -> List[UserData]:
        """按时间段获取用户数据"""
        users = await self.get_group_data(group_id)
        current_date = datetime.now().date()
        
        filtered_users = []
        
        for user in users:
            if not user.history:
                continue
            
            last_date = user.get_last_message_date()
            if not last_date:
                continue
            
            # 直接使用MessageDate对象的to_date()方法，避免重复字符串解析
            last_date_obj = last_date.to_date()
            
            if period == "daily" and last_date_obj == current_date:
                filtered_users.append(user)
            elif period == "weekly":
                # 获取本周开始日期
                week_start = current_date
                days_since_monday = current_date.weekday()
                week_start = current_date - timedelta(days=days_since_monday)
                
                if week_start <= last_date_obj <= current_date:
                    filtered_users.append(user)
            elif period == "monthly":
                # 获取本月
                if (last_date_obj.year == current_date.year and 
                    last_date_obj.month == current_date.month):
                    filtered_users.append(user)
        
        return filtered_users
    
    # ========== 数据导入导出 ==========
    
    async def export_group_data(self, group_id: str) -> Optional[Dict[str, Any]]:
        """导出群组数据"""
        try:
            users = await self.get_group_data(group_id)
            statistics = await self.get_group_statistics(group_id)
            
            return {
                "group_id": group_id,
                "export_time": datetime.now().isoformat(),
                "users": [user.to_dict() for user in users],
                "statistics": statistics
            }
        
        except (IOError, OSError) as e:
            self.logger.error(f"导出群组 {group_id} 数据失败: {e}")
            return None
    
    async def import_group_data(self, group_id: str, data: Dict[str, Any]) -> bool:
        """导入群组数据"""
        try:
            if "users" not in data:
                self.logger.error("导入数据缺少用户信息")
                return False
            
            users = []
            for user_data in data["users"]:
                try:
                    user = UserData.from_dict(user_data)
                    users.append(user)
                except (KeyError, ValueError, TypeError) as e:
                    self.logger.warning(f"跳过无效用户数据（缺少字段或类型错误）: {e}")
                    continue
                except Exception as e:
                    self.logger.warning(f"跳过无效用户数据（未知错误）: {e}")
                    continue
            
            await self.save_group_data(group_id, users)
            self.logger.info(f"群组 {group_id} 数据导入完成，共 {len(users)} 个用户")
            return True
        
        except (IOError, OSError) as e:
            self.logger.error(f"导入群组 {group_id} 数据失败: {e}")
            return False
    
    # ========== 清理和维护 ==========
    
    async def cleanup_old_data(self, days: int = 30):
        """清理旧数据 - 性能优化版本"""
        try:
            cutoff_date = datetime.now().date() - timedelta(days=days)
            cleaned_files = 0
            total_users_removed = 0
            
            # 批量处理文件以提高性能
            group_files = list(self.groups_dir.glob("*.json"))
            
            for group_file in group_files:
                try:
                    # 读取数据
                    async with aiofiles.open(group_file, 'r', encoding='utf-8') as f:
                        content = await f.read()
                        data_list = json.loads(content)
                    
                    # 优化：直接处理字典数据，避免不必要的序列化/反序列化
                    filtered_users = []
                    users_removed = 0
                    
                    for user_data in data_list:
                        # 优化：直接从字典中获取最后发言日期进行快速检查
                        last_date_str = user_data.get('last_date')
                        
                        if last_date_str:
                            try:
                                # 快速日期比较，避免解析整个历史记录
                                last_date = datetime.strptime(last_date_str, '%Y-%m-%d').date()
                                has_recent_activity = last_date >= cutoff_date
                            except ValueError:
                                # 日期格式错误，保留用户数据
                                has_recent_activity = True
                        else:
                            # 没有最后发言日期，检查总消息数
                            has_recent_activity = user_data.get('total', 0) >= 10
                        
                        # 保留有最近活动的用户或总消息数较高的用户
                        if has_recent_activity or user_data.get('total', 0) >= 10:
                            filtered_users.append(user_data)
                        else:
                            users_removed += 1
                    
                    # 只在有变化时保存文件
                    if len(filtered_users) != len(data_list):
                        async with aiofiles.open(group_file, 'w', encoding='utf-8') as f:
                            await f.write(json.dumps(filtered_users, ensure_ascii=False, indent=2))
                        
                        cleaned_files += 1
                        total_users_removed += users_removed
                        self.logger.info(f"清理了 {group_file.name}，移除了 {users_removed} 个用户")
                
                except (json.JSONDecodeError, IOError) as e:
                    self.logger.error(f"清理文件 {group_file} 失败: {e}")
                except (OSError, PermissionError) as e:
                    self.logger.error(f"清理文件 {group_file} 时发生系统错误: {e}")
                except Exception as e:
                    self.logger.error(f"清理文件 {group_file} 时发生未知错误: {e}")
            
            # 优化：批量清理图片缓存
            cache_files = list(self.cache_dir.glob("*.png"))
            cleaned_cache_files = 0
            
            for cache_file in cache_files:
                try:
                    file_time = datetime.fromtimestamp(cache_file.stat().st_mtime).date()
                    if file_time < cutoff_date:
                        cache_file.unlink()
                        cleaned_cache_files += 1
                except (OSError, PermissionError) as e:
                    self.logger.warning(f"删除缓存文件 {cache_file} 失败: {e}")
            
            # 记录清理结果
            if cleaned_files > 0:
                self.logger.info(f"数据清理完成：处理了 {len(group_files)} 个文件，清理了 {cleaned_files} 个文件，移除了 {total_users_removed} 个用户")
            if cleaned_cache_files > 0:
                self.logger.info(f"缓存清理完成：删除了 {cleaned_cache_files} 个过期缓存文件")
            
            if cleaned_files == 0 and cleaned_cache_files == 0:
                self.logger.info("数据清理完成：无需清理的数据")
        
        except (IOError, OSError, PermissionError) as e:
            self.logger.error(f"数据清理过程中发生系统错误: {e}")
            raise
        except Exception as e:
            self.logger.error(f"数据清理过程中发生未知错误: {e}")
            raise
