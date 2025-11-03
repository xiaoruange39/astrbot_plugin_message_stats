"""
数据存储模块
将DataManager拆分为更小的、职责单一的类
"""

import json
import asyncio
import aiofiles
import aiofiles.os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from astrbot.api import logger as astrbot_logger
from cachetools import TTLCache

from .models import UserData, PluginConfig, MessageDate


class GroupDataStore:
    """群组数据存储管理器
    
    专门负责群组数据（JSON文件）的增删改查和修复。
    """
    
    def __init__(self, groups_dir: Path, logger=None):
        self.groups_dir = groups_dir
        self.logger = logger or astrbot_logger
        # 目录创建延迟到首次使用时异步执行
    
    async def _ensure_groups_directory(self):
        """确保群组数据目录存在"""
        await asyncio.to_thread(self.groups_dir.mkdir, parents=True, exist_ok=True)
    
    def _get_group_file_path(self, group_id: str) -> Path:
        """获取群组数据文件路径"""
        return self.groups_dir / f"{group_id}.json"
    
    async def load_group_data(self, group_id: str) -> List[UserData]:
        """加载群组数据"""
        # 确保目录存在
        await self._ensure_groups_directory()
        file_path = self._get_group_file_path(group_id)
        
        if not await aiofiles.os.path.exists(file_path):
            return []
        
        try:
            async with aiofiles.open(str(file_path), 'r', encoding='utf-8') as f:
                content = await f.read()
                data = await asyncio.to_thread(json.loads, content)
            
            # 转换为UserData对象列表
            users = []
            
            # 处理不同的数据格式
            if isinstance(data, list):
                # 如果数据是列表格式，直接使用
                user_data_list = data
            elif isinstance(data, dict):
                # 如果数据是字典格式，获取users字段
                user_data_list = data.get('users', [])
            else:
                # 如果数据格式不正确，返回空列表
                self.logger.warning(f"群组 {group_id} 数据格式不正确")
                return []
            
            for user_data in user_data_list:
                try:
                    # 使用UserData.from_dict方法来消除逻辑重复
                    user = UserData.from_dict(user_data)
                    users.append(user)
                except (ValueError, TypeError) as e:
                    self.logger.warning(f"跳过无效的用户数据: {e}")
                    continue
            
            return users
            
        except (IOError, json.JSONDecodeError) as e:
            self.logger.error(f"读取群组数据失败 {group_id}: {e}")
            return []
    
    async def save_group_data(self, group_id: str, users: List[UserData]) -> bool:
        """保存群组数据"""
        file_path = self._get_group_file_path(group_id)
        
        try:
            # 准备数据
            data = {
                'group_id': group_id,
                'last_updated': datetime.now().isoformat(),
                'users': [user.to_dict() for user in users]
            }
            
            json_content = await asyncio.to_thread(json.dumps, data, ensure_ascii=False, indent=2)
            async with aiofiles.open(str(file_path), 'w', encoding='utf-8') as f:
                await f.write(json_content)
            
            return True
            
        except (IOError, OSError) as e:
            self.logger.error(f"保存群组数据失败 {group_id}: {e}")
            return False
    
    async def delete_group_data(self, group_id: str) -> bool:
        """删除群组数据"""
        file_path = self._get_group_file_path(group_id)
        
        try:
            if await aiofiles.os.path.exists(file_path):
                await aiofiles.os.remove(file_path)
                return True
            return False
        except OSError as e:
            self.logger.error(f"删除群组数据失败 {group_id}: {e}")
            return False
    
    async def repair_corrupted_json(self, group_id: str) -> bool:
        """修复损坏的JSON文件"""
        file_path = self._get_group_file_path(group_id)
        
        if not await aiofiles.os.path.exists(file_path):
            return False
        
        try:
            # 读取文件内容
            async with aiofiles.open(str(file_path), 'r', encoding='utf-8') as f:
                content = await f.read()
            
            # 尝试解析JSON
            try:
                await asyncio.to_thread(json.loads, content)
                return True  # 文件正常
            except json.JSONDecodeError:
                # 文件损坏，创建备份
                backup_path = file_path.with_suffix('.json.backup')
                async with aiofiles.open(str(backup_path), 'w', encoding='utf-8') as f:
                    await f.write(content)
                
                # 创建新的空数据文件
                await self.save_group_data(group_id, [])
                self.logger.warning(f"已修复损坏的群组数据文件 {group_id}，备份保存至 {backup_path}")
                return True
                
        except (IOError, OSError) as e:
            self.logger.error(f"修复群组数据失败 {group_id}: {e}")
            return False


class ConfigManager:
    """配置管理器
    
    专门负责 config.json 的读写。
    """
    
    def __init__(self, config_file: Path, logger=None):
        self.config_file = config_file
        self.logger = logger or astrbot_logger
        # 目录创建延迟到首次使用时异步执行
    
    async def _ensure_config_directory(self):
        """确保配置目录存在"""
        await asyncio.to_thread(self.config_file.parent.mkdir, parents=True, exist_ok=True)
    
    async def load_config(self) -> PluginConfig:
        """加载配置"""
        # 确保配置目录存在
        await self._ensure_config_directory()
        if not await aiofiles.os.path.exists(self.config_file):
            # 创建默认配置
            default_config = PluginConfig()
            await self.save_config(default_config)
            return default_config
        
        try:
            async with aiofiles.open(str(self.config_file), 'r', encoding='utf-8') as f:
                content = await f.read()
                data = await asyncio.to_thread(json.loads, content)
            
            # 转换为PluginConfig对象
            return PluginConfig.from_dict(data)
            
        except (IOError, json.JSONDecodeError) as e:
            self.logger.error(f"读取配置文件失败: {e}")
            # 返回默认配置
            return PluginConfig()
    
    async def save_config(self, config: PluginConfig) -> bool:
        """保存配置"""
        try:
            data = config.to_dict()
            
            json_content = await asyncio.to_thread(json.dumps, data, ensure_ascii=False, indent=2)
            async with aiofiles.open(str(self.config_file), 'w', encoding='utf-8') as f:
                await f.write(json_content)
            
            return True
            
        except (IOError, OSError) as e:
            self.logger.error(f"保存配置文件失败: {e}")
            return False


class PluginCache:
    """插件缓存管理器
    
    统一管理所有 TTLCache 实例（数据、配置、图片等）。
    """
    
    def __init__(self, logger=None):
        self.logger = logger or astrbot_logger
        
        # 缓存设置
        self.data_cache_maxsize = 1000
        self.data_cache_ttl = 300  # 5分钟
        self.config_cache_maxsize = 10
        self.config_cache_ttl = 60  # 1分钟
        
        # 创建缓存实例
        self.data_cache = TTLCache(maxsize=self.data_cache_maxsize, ttl=self.data_cache_ttl)
        self.config_cache = TTLCache(maxsize=self.config_cache_maxsize, ttl=self.config_cache_ttl)
    
    def get_data_cache(self):
        """获取数据缓存"""
        return self.data_cache
    
    def get_config_cache(self):
        """获取配置缓存"""
        return self.config_cache
    
    def clear_all_caches(self):
        """清理所有缓存"""
        self.data_cache.clear()
        self.config_cache.clear()
        self.logger.info("所有缓存已清理")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息
        
        注意：TTLCache 不支持 hits/misses 统计，只返回基本统计信息。
        """
        return {
            'data_cache': {
                'size': len(self.data_cache),
                'maxsize': self.data_cache.maxsize,
                'ttl': self.data_cache.ttl
            },
            'config_cache': {
                'size': len(self.config_cache),
                'maxsize': self.config_cache.maxsize,
                'ttl': self.config_cache.ttl
            }
        }