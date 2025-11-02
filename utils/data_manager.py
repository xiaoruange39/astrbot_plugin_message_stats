"""
数据管理模块
负责插件的数据存储、读取、缓存和管理
"""

import os
import json
import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, date
import aiofiles
from cachetools import TTLCache
import logging

from .models import (
    UserData, PluginConfig, GroupInfo, 
    MessageDate, RankData, load_json_file, save_json_file
)

logger = logging.getLogger('message_stats_plugin')


class DataManager:
    """数据管理器"""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.groups_dir = self.data_dir / "groups"
        self.cache_dir = self.data_dir / "cache" / "rank_images"
        self.config_file = self.data_dir / "config.json"
        
        # 缓存设置 - 暂时禁用缓存进行测试
        self.data_cache = TTLCache(maxsize=1000, ttl=1)  # 1秒缓存，几乎相当于禁用
        self.config_cache = TTLCache(maxsize=10, ttl=1)  # 1秒缓存，几乎相当于禁用
        
        # 确保目录存在
        self._ensure_directories()
    
    def _ensure_directories(self):
        """确保必要的目录存在"""
        directories = [
            self.data_dir,
            self.groups_dir,
            self.cache_dir
        ]
        
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    
    async def initialize(self):
        """初始化数据管理器"""
        logger.info("数据管理器初始化中...")
        
        # 创建默认配置
        if not self.config_file.exists():
            await self._create_default_config()
        
        logger.info("数据管理器初始化完成")
    
    async def _create_default_config(self):
        """创建默认配置"""
        default_config = PluginConfig()
        await self.save_config(default_config)
        logger.info("已创建默认配置文件")
    
    # ========== 群组数据管理 ==========
    
    async def get_group_data(self, group_id: str) -> List[UserData]:
        """获取群组数据"""
        cache_key = f"group_data_{group_id}"
        
        # 检查缓存
        if cache_key in self.data_cache:
            logger.info(f"从缓存获取群组 {group_id} 数据: {len(self.data_cache[cache_key])} 个用户")
            return self.data_cache[cache_key]
        
        file_path = self.groups_dir / f"{group_id}.json"
        
        logger.info(f"尝试读取群组数据: 群组ID={group_id}, 文件路径={file_path}")
        logger.info(f"文件是否存在: {file_path.exists()}")
        logger.info(f"groups_dir: {self.groups_dir}")
        
        try:
            if file_path.exists():
                logger.info(f"文件存在，开始读取: {file_path}")
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    logger.info(f"文件内容长度: {len(content)} 字符")
                    data_list = json.loads(content)
                    logger.info(f"解析得到 {len(data_list)} 个用户数据")
                
                # 转换为UserData对象
                users = [UserData.from_dict(user_data) for user_data in data_list]
                logger.info(f"转换后得到 {len(users)} 个UserData对象")
                
                # 调试信息：显示每个用户的total值
                for i, user in enumerate(users):
                    logger.info(f"  用户 {i+1}: {user.nickname} -> total={user.total}")
                
                # 缓存结果
                self.data_cache[cache_key] = users
                return users
            else:
                logger.info(f"文件不存在，返回空列表: {file_path}")
                return []
        
        except Exception as e:
            logger.error(f"读取群组 {group_id} 数据失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return []
    
    async def save_group_data(self, group_id: str, users: List[UserData]):
        """保存群组数据"""
        file_path = self.groups_dir / f"{group_id}.json"
        
        logger.info(f"准备保存群组数据: 群组ID={group_id}, 用户数={len(users)}")
        logger.info(f"保存路径: {file_path}")
        logger.info(f"groups_dir: {self.groups_dir}")
        
        try:
            # 转换为字典列表
            data_list = [user.to_dict() for user in users]
            logger.info(f"转换为字典列表: {len(data_list)} 个用户")
            
            # 调试信息：显示要保存的用户数据
            for i, user in enumerate(users):
                logger.info(f"  保存用户 {i+1}: {user.nickname} -> total={user.total}")
            
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"确保目录存在: {file_path.parent}")
            
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                json_content = json.dumps(data_list, ensure_ascii=False, indent=2)
                await f.write(json_content)
                logger.info(f"写入文件成功，内容长度: {len(json_content)} 字符")
            
            # 清除缓存
            cache_key = f"group_data_{group_id}"
            if cache_key in self.data_cache:
                del self.data_cache[cache_key]
                logger.info(f"清除缓存: {cache_key}")
            
            # 验证文件是否真的被创建
            if file_path.exists():
                file_size = file_path.stat().st_size
                logger.info(f"文件保存成功，文件大小: {file_size} 字节")
            else:
                logger.error(f"文件保存后检查：文件不存在！")
            
            logger.info(f"群组 {group_id} 数据已保存，共 {len(users)} 个用户")
        
        except Exception as e:
            logger.error(f"保存群组 {group_id} 数据失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
    
    async def update_user_message(self, group_id: str, user_id: str, nickname: str) -> bool:
        """更新用户消息统计"""
        logger.info(f"开始更新用户消息统计: 群组={group_id}, 用户ID={user_id}, 昵称={nickname}")
        
        try:
            # 获取当前群组数据
            users = await self.get_group_data(group_id)
            logger.info(f"获取到当前群组数据: {len(users)} 个用户")
            
            # 查找用户
            current_date = MessageDate.from_datetime(datetime.now())
            logger.info(f"当前日期: {current_date}")
            user_index = None
            
            for i, user in enumerate(users):
                logger.debug(f"检查用户 {i}: {user.nickname} (ID: {user.user_id})")
                if user.user_id == user_id:
                    user_index = i
                    logger.info(f"找到现有用户: {user.nickname}，索引: {i}")
                    break
            
            if user_index is not None:
                # 更新现有用户
                user = users[user_index]
                user.nickname = nickname  # 更新昵称
                old_total = user.total
                user.add_message(current_date)
                logger.info(f"更新现有用户: {user.nickname}, 发言数: {old_total} -> {user.total}")
            else:
                # 创建新用户
                new_user = UserData(user_id=user_id, nickname=nickname)
                new_user.add_message(current_date)
                users.append(new_user)
                logger.info(f"创建新用户: {new_user.nickname}, 发言数: {new_user.total}")
            
            logger.info(f"准备保存数据: {len(users)} 个用户")
            # 保存数据
            await self.save_group_data(group_id, users)
            logger.info("数据保存完成")
            return True
        
        except Exception as e:
            logger.error(f"更新用户消息失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
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
            
            logger.info(f"群组 {group_id} 数据已清除")
            return True
        
        except IOError as e:
            logger.error(f"清除群组 {group_id} 数据失败: {e}")
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
            
            logger.info(f"找到 {len(group_ids)} 个群组: {group_ids}")
            return group_ids
        
        except Exception as e:
            logger.error(f"获取群组列表失败: {e}")
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
            logger.error(f"读取配置文件失败: {e}")
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
            
            logger.debug("配置已保存")
        
        except IOError as e:
            logger.error(f"保存配置文件失败: {e}")
    
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
        
        except Exception as e:
            logger.error(f"更新配置失败: {e}")
            return False
    
    # ========== 缓存管理 ==========
    
    async def get_cached_image(self, cache_key: str) -> Optional[str]:
        """获取缓存的图片路径"""
        cache_file = self.cache_dir / f"{cache_key}.png"
        
        if cache_file.exists():
            # 检查文件是否过期（1小时）
            file_time = cache_file.stat().st_mtime
            if datetime.now().timestamp() - file_time < 3600:
                return str(cache_file)
            else:
                # 删除过期文件
                cache_file.unlink()
        
        return None
    
    async def cache_image(self, cache_key: str, image_path: str) -> bool:
        """缓存图片"""
        try:
            import shutil
            cache_file = self.cache_dir / f"{cache_key}.png"
            
            shutil.copy2(image_path, cache_file)
            logger.debug(f"图片已缓存: {cache_key}")
            return True
        
        except IOError as e:
            logger.error(f"缓存图片失败: {e}")
            return False
    
    async def clear_cache(self, cache_type: str = "all"):
        """清除缓存"""
        if cache_type in ["all", "data"]:
            self.data_cache.clear()
            logger.info("数据缓存已清除")
        
        if cache_type in ["all", "config"]:
            self.config_cache.clear()
            logger.info("配置缓存已清除")
        
        if cache_type in ["all", "image"]:
            # 清除图片缓存
            for cache_file in self.cache_dir.glob("*.png"):
                cache_file.unlink()
            logger.info("图片缓存已清除")
    
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
            
            try:
                last_date_obj = datetime.strptime(last_date, "%Y-%m-%d").date()
                
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
            
            except ValueError:
                # 忽略无效日期格式
                continue
        
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
        
        except Exception as e:
            logger.error(f"导出群组 {group_id} 数据失败: {e}")
            return None
    
    async def import_group_data(self, group_id: str, data: Dict[str, Any]) -> bool:
        """导入群组数据"""
        try:
            if "users" not in data:
                logger.error("导入数据缺少用户信息")
                return False
            
            users = []
            for user_data in data["users"]:
                try:
                    user = UserData.from_dict(user_data)
                    users.append(user)
                except Exception as e:
                    logger.warning(f"跳过无效用户数据: {e}")
                    continue
            
            await self.save_group_data(group_id, users)
            logger.info(f"群组 {group_id} 数据导入完成，共 {len(users)} 个用户")
            return True
        
        except Exception as e:
            logger.error(f"导入群组 {group_id} 数据失败: {e}")
            return False
    
    # ========== 清理和维护 ==========
    
    async def cleanup_old_data(self, days: int = 30):
        """清理旧数据"""
        try:
            cutoff_date = datetime.now().date() - timedelta(days=days)
            
            for group_file in self.groups_dir.glob("*.json"):
                try:
                    # 读取数据
                    async with aiofiles.open(group_file, 'r', encoding='utf-8') as f:
                        content = await f.read()
                        data_list = json.loads(content)
                    
                    # 过滤旧数据
                    filtered_users = []
                    for user_data in data_list:
                        user = UserData.from_dict(user_data)
                        
                        # 检查用户是否在截止日期后有活动
                        has_recent_activity = False
                        for hist_date in user.history:
                            if hist_date.to_date() >= cutoff_date:
                                has_recent_activity = True
                                break
                        
                        # 保留有最近活动的用户或总消息数较高的用户
                        if has_recent_activity or user.total >= 10:
                            filtered_users.append(user_data)
                    
                    # 保存过滤后的数据
                    if filtered_users != data_list:
                        async with aiofiles.open(group_file, 'w', encoding='utf-8') as f:
                            await f.write(json.dumps(filtered_users, ensure_ascii=False, indent=2))
                        
                        logger.info(f"清理了 {group_file.name} 的旧数据")
                
                except Exception as e:
                    logger.error(f"清理文件 {group_file} 失败: {e}")
            
            # 清理图片缓存
            for cache_file in self.cache_dir.glob("*.png"):
                file_time = datetime.fromtimestamp(cache_file.stat().st_mtime).date()
                if file_time < cutoff_date:
                    cache_file.unlink()
            
            logger.info("旧数据清理完成")
        
        except Exception as e:
            logger.error(f"清理旧数据失败: {e}")


from datetime import timedelta
