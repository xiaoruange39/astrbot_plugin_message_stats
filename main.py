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
from quart import jsonify, request

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
from .utils.constants import (
    MAX_RANK_COUNT,
    USER_NICKNAME_CACHE_TTL,
    GROUP_MEMBERS_CACHE_TTL as CACHE_TTL_SECONDS
)

@register("astrbot_plugin_message_stats", "xiaoruange39", "群发言统计插件", "1.9.1")
class MessageStatsPlugin(Star):
    """群发言统计插件
    该插件用于统计群组成员的发言次数,并生成多种类型的排行榜.
    支持自动监听群消息、手动记录、总榜/日榜/周榜/月榜/年榜等功能.
    """
    
    def __init__(self, context: Context, config: 'AstrBotConfig' = None):
        super().__init__(context)
        
        # 注册 plugin pages API
        context.register_web_api(
            "/astrbot_plugin_message_stats/stats",
            self.page_stats, ["GET"], "发言统计面板数据",
        )
        context.register_web_api(
            "/astrbot_plugin_message_stats/delete",
            self.page_delete, ["GET"], "删除群组发言数据",
        )
        self.logger = astrbot_logger
        
        data_dir = StarTools.get_data_dir('message_stats')
        self.data_manager = DataManager(data_dir)
        self.config = config
        self.plugin_config = self._convert_to_plugin_config()
        self.image_generator = None
        
        # 群组unified_msg_origin映射表
        self.group_unified_msg_origins = {}
        self._umo_file = Path(data_dir) / "unified_msg_origins.json"
        self._load_unified_msg_origins()
        
        self._group_names_file = Path(data_dir) / "group_names.json"
        self._web_group_name_cache: Dict[str, str] = {}
        self._load_group_names()
        
        self.member_cache = MemberCacheManager(
            context, cache_ttl=CACHE_TTL_SECONDS, nickname_cache_ttl=USER_NICKNAME_CACHE_TTL
        )
        
        self.timer_manager = None
        self._jsonify = jsonify
    
    def _load_unified_msg_origins(self):
        try:
            if self._umo_file.exists():
                with open(str(self._umo_file), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.group_unified_msg_origins = data
        except Exception as e:
            self.logger.debug(f"加载 unified_msg_origin 文件失败: {e}")

    def _save_unified_msg_origins(self):
        try:
            self._umo_file.parent.mkdir(parents=True, exist_ok=True)
            with open(str(self._umo_file), 'w', encoding='utf-8') as f:
                json.dump(self.group_unified_msg_origins, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.debug(f"保存 unified_msg_origin 文件失败: {e}")

    def _load_group_names(self):
        try:
            if self._group_names_file.exists():
                with open(str(self._group_names_file), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._web_group_name_cache = data
        except Exception as e:
            self.logger.debug(f"加载群组名称文件失败: {e}")

    def _save_group_names(self):
        try:
            self._group_names_file.parent.mkdir(parents=True, exist_ok=True)
            with open(str(self._group_names_file), 'w', encoding='utf-8') as f:
                json.dump(self._web_group_name_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.debug(f"保存群组名称文件失败: {e}")
    
    async def page_stats(self):
        try:
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
                    tu.append({
                        "nickname": u.nickname, "message_count": u.message_count,
                        "title": u.display_title or "", "title_color": u.display_title_color or "",
                        "last_date": u.last_date or "", "percentage": round(pct, 1)
                    })
                fp2 = self.data_manager.groups_dir / f"{gid}.json"
                fs2 = ""
                if fp2.exists():
                    s2 = os.path.getsize(str(fp2))
                    fs2 = f"{s2/1024:.1f}KB" if s2 < 1024*1024 else f"{s2/1024/1024:.1f}MB"
                gn = self._web_group_name_cache.get(str(gid), f"群{gid}")
                return self._jsonify({
                    "status": "ok", "data": {
                        "group": {
                            "group_id": gid, "group_name": gn, "display_name": f"{gn} - {gid}",
                            "file_size": fs2, "total_messages": tm, "user_count": len(act), "top_users": tu
                        }
                    }
                })
            gd = []
            ag = await self.data_manager.get_all_groups()
            for g2 in ag[:50]:
                us = await self.data_manager.get_group_data(g2)
                if not us: continue
                ac = [u for u in us if u.message_count > 0]
                ac.sort(key=lambda x: x.message_count, reverse=True)
                fp = self.data_manager.groups_dir / f"{g2}.json"
                fs = ""
                if fp.exists():
                    s = os.path.getsize(str(fp))
                    fs = f"{s/1024:.1f}KB" if s < 1024*1024 else f"{s/1024/1024:.1f}MB"
                gn = self._web_group_name_cache.get(str(g2), f"群{g2}")
                gd.append({
                    "group_id": g2, "group_name": gn, "display_name": f"{gn} - {g2}",
                    "file_size": fs, "total_messages": sum(u.message_count for u in ac), "user_count": len(ac)
                })
            ts = None
            if self.timer_manager:
                s = await self.timer_manager.get_status()
                ts = {"running": s["status"] == "running", "next_push": str(s.get("next_push_time", "") or "")}
            c = self.plugin_config
            return self._jsonify({
                "status": "ok", "data": {
                    "groups": gd, "config": {"rand": c.rand, "if_send_pic": c.if_send_pic}, "timer": ts
                }
            })
        except Exception as e:
            return self._jsonify({"status": "error", "message": str(e)})
    
    async def page_delete(self):
        try:
            gid = request.args.get('group_id') if request else None
            if not gid:
                return self._jsonify({"status": "error", "message": "缺少group_id参数"})
            ok = await self.data_manager.clear_group_data(gid)
            if ok:
                return self._jsonify({"status": "ok", "message": "已删除"})
            return self._jsonify({"status": "error", "message": "删除失败"})
        except Exception as e:
            return self._jsonify({"status": "error", "message": str(e)})

    def _convert_to_plugin_config(self) -> PluginConfig:
        try:
            if not self.config:
                return PluginConfig()
            config_dict = dict(self.config) if hasattr(self.config, 'items') else {}
            if 'theme_switch_light_time' in config_dict or 'theme_switch_dark_time' in config_dict:
                theme_times = config_dict.get('theme_switch_times', {})
                if isinstance(theme_times, dict):
                    if 'theme_switch_light_time' in config_dict:
                        theme_times['light'] = config_dict.pop('theme_switch_light_time')
                    if 'theme_switch_dark_time' in config_dict:
                        theme_times['dark'] = config_dict.pop('theme_switch_dark_time')
                    config_dict['theme_switch_times'] = theme_times
            return PluginConfig.from_dict(config_dict)
        except Exception as e:
            self.logger.error(f"配置转换失败: {e}")
            return PluginConfig()
    
    async def _collect_group_unified_msg_origin(self, event: AstrMessageEvent):
        try:
            group_id = event.get_group_id()
            unified_msg_origin = event.unified_msg_origin
            if group_id and unified_msg_origin:
                group_id_str = str(group_id)
                old_origin = self.group_unified_msg_origins.get(group_id_str)
                self.group_unified_msg_origins[group_id_str] = unified_msg_origin
                try:
                    extracted_id = unified_msg_origin.rsplit(':', 1)[-1]
                    if extracted_id and extracted_id != group_id_str:
                        self.group_unified_msg_origins[extracted_id] = unified_msg_origin
                except (AttributeError, IndexError, ValueError):
                    pass
                self.group_unified_msg_origins[unified_msg_origin] = unified_msg_origin
                self._save_unified_msg_origins()
                if old_origin != unified_msg_origin:
                    self.logger.info(f"已收集群组 {group_id} 的 unified_msg_origin")
        except (AttributeError, KeyError, TypeError) as e:
            self.logger.error(f"收集群组unified_msg_origin失败: {e}")
        except (RuntimeError, OSError, IOError, ImportError, ValueError) as e:
            self.logger.error(f"收集群组unified_msg_origin失败(系统错误): {e}")
    
    async def _cache_group_name(self, event: Optional[AstrMessageEvent], group_id: str):
        try:
            group_id_str = str(group_id)
            helper = PlatformHelper(event, self.context)
            group_name = await helper.get_group_name(group_id)
            if group_name:
                group_name = str(group_name).strip()
                old_name = self._web_group_name_cache.get(group_id_str)
                if old_name != group_name:
                    self._web_group_name_cache[group_id_str] = group_name
                    self._save_group_names()
        except (AttributeError, KeyError, TypeError, RuntimeError) as e:
            self.logger.debug(f"缓存群组名称失败: {e}")
    
    async def _collect_group_unified_msg_origins(self):
        return self.group_unified_msg_origins.copy()
    
    RANK_COUNT_MIN = 1
    IMAGE_MODE_ENABLE_ALIASES = {'1', 'true', '开', 'on', 'yes'}
    IMAGE_MODE_DISABLE_ALIASES = {'0', 'false', '关', 'off', 'no'}
    
    async def initialize(self):
        try:
            self.logger.info("群发言统计插件初始化中...")
            await self._initialize_data_manager()
            await self._load_plugin_config()
            self.data_manager.set_plugin_config(self.plugin_config)
            await self._initialize_timer_manager()
            await self._setup_caches()
            self.logger.info("群发言统计插件初始化完成")
        except (OSError, IOError) as e:
            self.logger.error(f"插件初始化失败: {e}")
            raise
    
    async def _initialize_data_manager(self):
        await self.data_manager.initialize()
    
    async def _load_plugin_config(self):
        self.plugin_config = self._convert_to_plugin_config()
        self.image_generator = ImageGenerator(self.plugin_config, context=self.context)
        try:
            await self.image_generator.initialize()
        except ImageGenerationError as e:
            self.logger.warning(f"图片生成器初始化失败: {e}")
    
    async def _initialize_timer_manager(self):
        try:
            from .utils.timer_manager import TimerManager
            self.timer_manager = TimerManager(
                self.data_manager, self.image_generator, self.context, self.group_unified_msg_origins
            )
        except (ImportError, OSError, IOError) as e:
            self.logger.warning(f"定时任务管理器初始化失败: {e}")
            self.timer_manager = None
        except (RuntimeError, AttributeError, ValueError, TypeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.warning(f"定时任务管理器初始化失败: {e}")
            self.timer_manager = None
    
    async def _setup_caches(self):
        self.initialized = True
        if self.timer_manager and self.plugin_config.timer_enabled:
            try:
                await self.timer_manager.update_config(self.plugin_config, self.group_unified_msg_origins)
            except Exception as e:
                self.logger.warning(f"定时任务启动失败: {e}")
    
    async def terminate(self):
        try:
            await self.data_manager.flush_all()
            if self.image_generator:
                await self.image_generator.cleanup()
            await self.data_manager.clear_cache()
            self.member_cache.clear_all()
            self.initialized = False
        except (OSError, IOError) as e:
            self.logger.error(f"插件卸载失败: {e}")
    
    @filter.event_message_type(EventMessageType.ALL)
    async def auto_message_listener(self, event: AstrMessageEvent):
        """自动消息监听器 - 监听所有消息并记录群成员发言统计"""
        message_str = getattr(event, 'message_str', '')
        if not message_str or message_str.startswith(('%', '/')):
            return
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        if not group_id or not user_id:
            return
        group_id, user_id = str(group_id), str(user_id)
        if self._is_bot_message(event, user_id):
            return
        if self._is_blocked_group(group_id):
            return
        await self._collect_group_unified_msg_origin(event)
        nickname = await self._get_user_display_name(event, group_id, user_id)
        await self._record_message_stats(group_id, user_id, nickname)
    
    def _is_bot_message(self, event: AstrMessageEvent, user_id: str) -> bool:
        try:
            self_id = event.get_self_id()
            return self_id and user_id == str(self_id)
        except (AttributeError, KeyError, TypeError):
            return False
    
    async def _record_message_stats(self, group_id: str, user_id: str, nickname: str):
        try:
            if self._is_blocked_user(user_id):
                return
            if not nickname or not nickname.strip():
                nickname = f"用户{user_id}"
            validated_data = await self._validate_message_data(group_id, user_id, nickname)
            group_id, user_id, nickname = validated_data
            await self._process_message_stats(group_id, user_id, nickname)
        except (ValueError, TypeError, KeyError) as e:
            self.logger.error(f"记录消息统计失败: {e}", exc_info=True)
        except (IOError, OSError, RuntimeError, AttributeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"记录消息统计失败: {e}", exc_info=True)
    
    @data_operation_handler('validate', '消息数据参数')
    async def _validate_message_data(self, group_id: str, user_id: str, nickname: str) -> tuple:
        group_id = Validators.validate_group_id(group_id)
        user_id = Validators.validate_user_id(user_id)
        nickname = Validators.validate_nickname(nickname)
        return group_id, user_id, nickname
    
    async def _process_message_stats(self, group_id: str, user_id: str, nickname: str):
        success, message_count = await self.data_manager.update_user_message(group_id, user_id, nickname)
        if success:
            cached_nickname = self.member_cache.get_nickname_from_cache(user_id)
            if cached_nickname != nickname:
                self.member_cache.update_nickname_cache(user_id, nickname)
            await self._check_milestone(group_id, user_id, nickname, message_count)
        else:
            self.logger.error(f"记录消息统计失败: {nickname}")
    
    async def _check_milestone(self, group_id: str, user_id: str, nickname: str, current_count: int):
        if not self.plugin_config.milestone_enabled or not self.plugin_config.milestone_targets:
            return
        milestone_set = set(self.plugin_config.milestone_targets)
        if current_count not in milestone_set:
            return
        if self.member_cache.is_milestone_cached(group_id, user_id, current_count):
            return
        self.member_cache.mark_milestone_cached(group_id, user_id, current_count)
        try:
            unified_msg_origin = self.group_unified_msg_origins.get(str(group_id))
            if not unified_msg_origin:
                self.logger.warning(f"群组 {group_id} 缺少 unified_msg_origin")
                return
            group_data = await self.data_manager.get_group_data(group_id)
            if not group_data:
                return
            rank, group_total_messages, target_user_data = 1, 0, None
            for item in group_data:
                if not isinstance(item, UserData): continue
                group_total_messages += item.message_count
                if item.message_count > current_count: rank += 1
                if item.user_id == user_id: target_user_data = item
            percentage = (current_count / group_total_messages * 100) if group_total_messages > 0 else 0
            daily_count = target_user_data.get_message_count_in_period(date.today(), date.today()) if target_user_data else 0
            active_days, last_date = 0, ""
            if target_user_data:
                target_user_data._ensure_message_dates()
                active_days = len(target_user_data._message_dates)
                last_date = target_user_data.last_date or ""
            group_info = GroupInfo(group_id=str(group_id))
            group_info.group_name = await self._get_group_name(None, group_id)
            image_path = await self.image_generator.generate_milestone_image(
                user_id=user_id, nickname=nickname, milestone_count=current_count,
                rank=rank, daily_count=daily_count, active_days=active_days,
                last_date=last_date, group_total_messages=group_total_messages,
                percentage=percentage, group_info=group_info
            )
            if not image_path: return
            from astrbot.api.event import MessageChain
            await self.context.send_message(unified_msg_origin, MessageChain().file_image(image_path))
            if await aiofiles.os.path.exists(image_path):
                await aiofiles.os.unlink(image_path)
        except Exception as e:
            self.logger.error(f"里程碑推送失败: {e}", exc_info=True)
    
    @filter.command("发言榜里程碑", alias={'发言里程碑', 'milestone', 'достижение'})
    async def show_my_milestone(self, event: AstrMessageEvent):
        """显示个人里程碑成就卡片，别名：发言里程碑"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        if not group_id or not user_id:
            yield event.plain_result("无法获取群组或用户信息,请在群聊中使用此命令！")
            return
        group_id, user_id = str(group_id), str(user_id)
        nickname = await self._get_user_display_name(event, group_id, user_id)
        try:
            group_data = await self.data_manager.get_group_data(group_id)
            if not group_data:
                yield event.plain_result("该群暂无发言数据！")
                return
            rank, group_total_messages, target_user_data = 1, 0, None
            for item in group_data:
                if not isinstance(item, UserData): continue
                group_total_messages += item.message_count
                if item.user_id == user_id: target_user_data = item
            if not target_user_data:
                for item in group_data:
                    if isinstance(item, UserData) and item.message_count > 0: rank += 1
                current_count = 0
            else:
                current_count = target_user_data.message_count
                rank = 1
                for item in group_data:
                    if isinstance(item, UserData) and item.message_count > current_count: rank += 1
            percentage = (current_count / group_total_messages * 100) if group_total_messages > 0 else 0
            daily_count = target_user_data.get_message_count_in_period(date.today(), date.today()) if target_user_data else 0
            active_days, last_date = 0, ""
            if target_user_data:
                target_user_data._ensure_message_dates()
                active_days = len(target_user_data._message_dates)
                last_date = target_user_data.last_date or ""
            group_info = GroupInfo(group_id=str(group_id))
            group_info.group_name = await self._get_group_name(event, group_id)
            image_path = await self.image_generator.generate_milestone_image(
                user_id=user_id, nickname=nickname, milestone_count=current_count,
                rank=rank, daily_count=daily_count, active_days=active_days,
                last_date=last_date, group_total_messages=group_total_messages,
                percentage=percentage, group_info=group_info
            )
            if not image_path:
                yield event.plain_result("个人里程碑卡片生成失败！")
                return
            yield event.image_result(image_path)
            if await aiofiles.os.path.exists(image_path):
                await aiofiles.os.unlink(image_path)
        except Exception as e:
            self.logger.error(f"里程碑获取失败: {e}", exc_info=True)
            yield event.plain_result(f"获取里程碑失败: {str(e)}")

    @filter.command("发言榜", alias={'水群榜', 'B话榜', '发言排行', '发言统计', 'leaderboard', 'рейтинг'})
    async def show_full_rank(self, event: AstrMessageEvent):
        """显示总排行榜，别名：水群榜/B话榜/发言排行/发言统计"""
        async for result in self._show_rank(event, RankType.TOTAL):
            yield result
    
    @filter.command("今日发言榜", alias={'今日水群榜', '今日发言排行', '今日B话榜', 'today', 'сегодня'})
    async def show_daily_rank(self, event: AstrMessageEvent):
        """显示今日排行榜，别名：今日水群榜/今日发言排行/今日B话榜"""
        async for result in self._show_rank(event, RankType.DAILY):
            yield result
    
    @filter.command("本周发言榜", alias={'本周水群榜', '本周发言排行', '本周B话榜', 'week', 'неделя'})
    async def show_weekly_rank(self, event: AstrMessageEvent):
        """显示本周排行榜，别名：本周水群榜/本周发言排行/本周B话榜"""
        async for result in self._show_rank(event, RankType.WEEKLY):
            yield result
    
    @filter.command("本月发言榜", alias={'本月水群榜', '本月发言排行', '本月B话榜', 'month', 'месяц'})
    async def show_monthly_rank(self, event: AstrMessageEvent):
        """显示本月排行榜，别名：本月水群榜/本月发言排行/本月B话榜"""
        async for result in self._show_rank(event, RankType.MONTHLY):
            yield result
    
    @filter.command("本年发言榜", alias={'本年水群榜', '本年发言排行', '本年B话榜', '年榜', 'year', 'год'})
    async def show_yearly_rank(self, event: AstrMessageEvent):
        """显示本年排行榜，别名：本年水群榜/本年发言排行/本年B话榜/年榜"""
        async for result in self._show_rank(event, RankType.YEARLY):
            yield result
    
    @filter.command("去年发言榜", alias={'去年水群榜', '去年发言排行', '去年B话榜', 'lastyear', 'прошлый'})
    async def show_last_year_rank(self, event: AstrMessageEvent):
        """显示去年排行榜，别名：去年水群榜/去年发言排行/去年B话榜"""
        async for result in self._show_rank(event, RankType.LAST_YEAR):
            yield result
    
    @filter.command("设置发言榜数量")
    async def set_rank_count(self, event: AstrMessageEvent):
        """设置排行榜显示人数"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            if not args:
                yield event.plain_result("请指定数量！用法:#设置发言榜数量 10")
                return
            try:
                count = int(args[0])
                if count < self.RANK_COUNT_MIN or count > MAX_RANK_COUNT:
                    yield event.plain_result(f"数量必须在{self.RANK_COUNT_MIN}-{MAX_RANK_COUNT}之间！")
                    return
            except ValueError:
                yield event.plain_result("数量必须是数字！")
                return
            config = await self.data_manager.get_config()
            config.rand = count
            await self.data_manager.save_config(config)
            yield event.plain_result(f"排行榜显示人数已设置为 {count} 人！")
        except (ValueError, TypeError, KeyError, IOError, OSError, FileNotFoundError, RuntimeError, AttributeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"设置排行榜数量失败: {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")

    @filter.command("设置发言榜图片")
    async def set_image_mode(self, event: AstrMessageEvent):
        """设置排行榜的显示模式（图片或文字）"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            if not args:
                yield event.plain_result("请指定模式！用法:#设置发言榜图片 1")
                return
            mode = args[0].lower()
            if mode in self.IMAGE_MODE_ENABLE_ALIASES:
                send_pic, mode_text = 1, "图片模式"
            elif mode in self.IMAGE_MODE_DISABLE_ALIASES:
                send_pic, mode_text = 0, "文字模式"
            else:
                yield event.plain_result("模式参数错误！可用:1/true/开 或 0/false/关")
                return
            config = await self.data_manager.get_config()
            config.if_send_pic = send_pic
            await self.data_manager.save_config(config)
            yield event.plain_result(f"排行榜显示模式已设置为 {mode_text}！")
        except (ValueError, TypeError, KeyError, IOError, OSError, FileNotFoundError, RuntimeError, AttributeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"设置图片模式失败: {e}", exc_info=True)
            yield event.plain_result("设置失败,请稍后重试")
    
    @filter.command("清除发言榜单")
    async def clear_message_ranking(self, event: AstrMessageEvent):
        """清除发言榜单"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("无法获取群组信息,请在群聊中使用此命令！")
                return
            success = await self.data_manager.clear_group_data(str(group_id))
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
            success = await self.member_cache.refresh_group_cache(event, str(group_id))
            if success:
                yield event.plain_result("群成员缓存、字典缓存和昵称缓存已全部刷新！")
            else:
                yield event.plain_result("刷新缓存失败,请稍后重试！")
        except (AttributeError, KeyError, TypeError, IOError, OSError, RuntimeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"刷新群成员缓存失败: {e}", exc_info=True)
            yield event.plain_result("刷新缓存失败,请稍后重试！")
    
    @filter.command("发言榜缓存状态")
    async def show_cache_status(self, event: AstrMessageEvent):
        """显示缓存状态"""
        try:
            cache_stats = await self.data_manager.get_cache_stats()
            member_cache_stats = self.member_cache.get_cache_stats()
            msg = [
                "📊 缓存状态报告", "━━━━━━━━━━━━━━",
                f"💾 数据缓存: {cache_stats['data_cache_size']}/{cache_stats['data_cache_maxsize']}",
                f"⚙️ 配置缓存: {cache_stats['config_cache_size']}/{cache_stats['config_cache_maxsize']}",
                f"👥 群成员缓存: {member_cache_stats['members_cache_size']}/{member_cache_stats['members_cache_maxsize']}",
                f"📖 字典缓存: {member_cache_stats['dict_cache_size']}",
                f"🏷️ 昵称缓存: {member_cache_stats['nickname_cache_size']}/{member_cache_stats['nickname_cache_maxsize']}",
                "━━━━━━━━━━━━━━",
                "🕐 数据缓存TTL: 5分钟", "🕐 配置缓存TTL: 1分钟",
                "🕐 群成员缓存TTL: 5分钟", "🕐 昵称缓存TTL: 10分钟"
            ]
            yield event.plain_result('\n'.join(msg))
        except (ValueError, TypeError, KeyError, IOError, OSError, RuntimeError, AttributeError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"显示缓存状态失败: {e}", exc_info=True)
            yield event.plain_result("获取缓存状态失败,请稍后重试！")
    
    async def _get_user_display_name(self, event: AstrMessageEvent, group_id: str, user_id: str) -> str:
        nickname = await self.member_cache.get_user_display_name(event, group_id, user_id)
        if nickname == f"用户{user_id}":
            return await self.member_cache.get_fallback_nickname(event, user_id)
        return nickname
    
    def _is_blocked_user(self, user_id: str) -> bool:
        if not hasattr(self, 'plugin_config') or not self.plugin_config:
            return False
        blocked_users = getattr(self.plugin_config, 'blocked_users', [])
        if not blocked_users: return False
        return str(user_id) in [str(uid) for uid in blocked_users]
    
    def _is_blocked_group(self, group_id: str) -> bool:
        if not hasattr(self, 'plugin_config') or not self.plugin_config:
            return False
        blocked_groups = getattr(self.plugin_config, 'blocked_groups', [])
        if not blocked_groups: return False
        return str(group_id) in [str(gid) for gid in blocked_groups]
    
    async def _get_group_name(self, event: Optional[AstrMessageEvent], group_id: str) -> str:
        try:
            if event is not None:
                group_data = await event.get_group(group_id)
                if group_data:
                    return (getattr(group_data, 'group_name', None) or getattr(group_data, 'name', None) or f"群{group_id}")
            helper = PlatformHelper(event, self.context)
            group_name = await helper.get_group_name(group_id)
            if group_name: return str(group_name).strip()
            return f"群{group_id}"
        except (AttributeError, KeyError, TypeError, OSError):
            return f"群{group_id}"
    
    async def _fetch_group_members_from_api(self, event: AstrMessageEvent, group_id: str):
        return await self.member_cache._fetch_group_members_from_api(event, group_id)
    
    async def _show_rank(self, event: AstrMessageEvent, rank_type: RankType):
        try:
            group_id = event.get_group_id()
            if group_id and self._is_blocked_group(str(group_id)):
                return
            rank_data = await self._prepare_rank_data(event, rank_type)
            if rank_data is None:
                yield event.plain_result("无法获取排行榜数据,请检查群组信息或稍后重试")
                return
            _, current_user_id, filtered_data, config, title, group_info = rank_data
            token_usage_info, titles_map = None, None
            if config.llm_enabled and config.llm_enable_on_manual:
                try:
                    group_data = await self.data_manager.get_group_data(str(group_id))
                    if group_data:
                        llm_analyzer = LLMAnalyzer(
                            context=self.context, provider_id=getattr(config, 'llm_provider_id', ''),
                            system_prompt=getattr(config, 'llm_system_prompt', ''),
                            max_retries=getattr(config, 'llm_max_retries', 2)
                        )
                        ranked_users = [user for user, _ in filtered_data[:config.rand]]
                        need_llm = [u for u in ranked_users if not u.display_title]
                        if not need_llm:
                            self.logger.info("所有用户已有头衔，跳过LLM分析")
                        else:
                            if len(need_llm) < len(ranked_users):
                                self.logger.info(f"已有 {len(ranked_users) - len(need_llm)} 个用户有头衔，跳过")
                            titles, token_usage = await llm_analyzer.analyze_users(
                                need_llm, group_info.group_name or f"群{group_id}",
                                min_daily_messages=getattr(config, 'llm_min_daily_messages', 0)
                            )
                        if token_usage and token_usage.get("total_tokens", 0) > 0:
                            token_usage_info = token_usage
                        if titles:
                            titles_map = titles
                            for u, _ in filtered_data:
                                if u.user_id in titles:
                                    info = titles[u.user_id]
                                    if isinstance(info, dict):
                                        u.display_title = info.get("title")
                                        u.display_title_color = info.get("color")
                                    else:
                                        u.display_title = info
                except Exception as e:
                    self.logger.error(f"LLM头衔生成异常: {e}", exc_info=True)
            if config.if_send_pic:
                async for r in self._render_rank_as_image(event, filtered_data, group_info, title, current_user_id, config, token_usage_info, titles_map):
                    yield r
            else:
                async for r in self._render_rank_as_text(event, filtered_data, group_info, title, config):
                    yield r
        except (IOError, OSError) as e:
            self.logger.error(f"文件操作失败: {e}")
            yield event.plain_result("文件操作失败,请检查权限")
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            self.logger.error(f"数据格式错误: {e}")
            yield event.plain_result("数据格式错误,请联系管理员")
        except (ConnectionError, TimeoutError, RuntimeError, ImportError) as e:
            self.logger.error(f"系统错误: {e}")
            yield event.plain_result("系统错误,请稍后重试")
    
    async def _prepare_rank_data(self, event: AstrMessageEvent, rank_type: RankType):
        group_id = event.get_group_id()
        current_user_id = event.get_sender_id()
        if not group_id or not current_user_id:
            return None
        group_id, current_user_id = str(group_id), str(current_user_id)
        await self._cache_group_name(event, group_id)
        group_data = await self.data_manager.get_group_data(group_id)
        if not group_data: return None
        await self._refresh_nickname_cache_for_ranking(event, group_id, group_data)
        filtered = await self._filter_data_by_rank_type(group_data, rank_type)
        if not filtered: return None
        filtered_data = sorted(filtered, key=lambda x: x[1], reverse=True)
        config = self.plugin_config
        title = self._generate_title(rank_type)
        group_info = GroupInfo(group_id=group_id)
        group_info.group_name = await self._get_group_name(event, group_id)
        return group_id, current_user_id, filtered_data, config, title, group_info
    
    async def _refresh_nickname_cache_for_ranking(self, event, group_id, group_data):
        try:
            members = await self._fetch_group_members_from_api(event, group_id)
            if not members: return
            dict_key = f"group_members_dict_{group_id}"
            members_dict = {}
            for m in members:
                uid = PlatformHelper.get_user_id_from_member(m)
                if uid: members_dict[uid] = m
            self.member_cache.group_members_dict_cache[dict_key] = members_dict
            updated = 0
            for user in group_data:
                if user.user_id in members_dict:
                    name = self.member_cache.get_display_name_from_member(members_dict[user.user_id])
                    if name and user.nickname != name:
                        user.nickname = name
                        self.member_cache.update_nickname_cache(user.user_id, name)
                        updated += 1
            if updated > 0:
                await self.data_manager.save_group_data(group_id, group_data)
        except Exception as e:
            self.logger.warning(f"排行榜前刷新昵称缓存失败: {e}")

    async def _render_rank_as_image(self, event, filtered_data, group_info, title, current_user_id, config, token_usage, titles_map):
        temp_path = None
        try:
            limited = filtered_data[:config.rand]
            users = []
            for u, c in limited:
                u.display_total = c
                users.append(u)
            temp_path = await self.image_generator.generate_rank_image(users, group_info, title, current_user_id, token_usage, titles_map)
            if await aiofiles.os.path.exists(temp_path):
                yield event.image_result(str(temp_path))
            else:
                yield event.plain_result(self._generate_text_message(filtered_data, group_info, title, config))
        except Exception as e:
            self.logger.error(f"生成图片失败: {e}")
            yield event.plain_result(self._generate_text_message(filtered_data, group_info, title, config))
        finally:
            if temp_path and await aiofiles.os.path.exists(temp_path):
                try:
                    await aiofiles.os.unlink(temp_path)
                except OSError as e:
                    self.logger.warning(f"清理临时图片失败: {e}")
    
    async def _render_rank_as_text(self, event, filtered_data, group_info, title, config):
        yield event.plain_result(self._generate_text_message(filtered_data, group_info, title, config))
    
    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _get_time_period_for_rank_type(self, rank_type: RankType) -> tuple:
        current_date = datetime.now().date()
        if rank_type == RankType.TOTAL:
            return None, None, "total"
        elif rank_type == RankType.DAILY:
            return current_date, current_date, "daily"
        elif rank_type == RankType.WEEKLY:
            week_start = current_date - timedelta(days=current_date.weekday())
            return week_start, current_date, "weekly"
        elif rank_type == RankType.MONTHLY:
            return current_date.replace(day=1), current_date, "monthly"
        elif rank_type == RankType.YEARLY:
            return current_date.replace(month=1, day=1), current_date, "yearly"
        elif rank_type == RankType.LAST_YEAR:
            last = current_date.year - 1
            return date(last, 1, 1), date(last, 12, 31), "lastyear"
        return None, None, "unknown"
    
    async def _filter_data_by_rank_type(self, group_data, rank_type):
        start, end, _ = self._get_time_period_for_rank_type(rank_type)
        if rank_type == RankType.TOTAL:
            return [(u, u.message_count) for u in group_data if u.message_count > 0 and not self._is_blocked_user(u.user_id)]
        elif rank_type == RankType.DAILY:
            result = []
            for u in group_data:
                if self._is_blocked_user(u.user_id): continue
                if not u._message_dates and not u.history: continue
                c = u.get_message_count_in_period(start, end)
                if c > 0: result.append((u, c))
            return result
        else:
            active = [u for u in group_data if u._message_dates or u.history]
            result = []
            for u in active:
                if self._is_blocked_user(u.user_id): continue
                c = u.get_message_count_in_period(start, end)
                if c > 0: result.append((u, c))
            return result
    
    def _generate_title(self, rank_type: RankType) -> str:
        now = datetime.now()
        lang = getattr(self.plugin_config, 'image_language', 'zh-CN')
        if lang == 'en-US':
            titles = {
                RankType.TOTAL: "All-Time Leaderboard",
                RankType.DAILY: f"Today [{now.year}-{now.month:02d}-{now.day:02d}]",
                RankType.WEEKLY: f"This Week [{now.year} W{now.isocalendar().week}]",
                RankType.MONTHLY: f"This Month [{now.year}-{now.month:02d}]",
                RankType.YEARLY: f"This Year [{now.year}]",
                RankType.LAST_YEAR: f"Last Year [{now.year-1}]",
            }
        elif lang == 'ru-RU':
            titles = {
                RankType.TOTAL: "Общий рейтинг",
                RankType.DAILY: f"Сегодня [{now.year}-{now.month:02d}-{now.day:02d}]",
                RankType.WEEKLY: f"Эта неделя [{now.year} W{now.isocalendar().week}]",
                RankType.MONTHLY: f"Этот месяц [{now.year}-{now.month:02d}]",
                RankType.YEARLY: f"Этот год [{now.year}]",
                RankType.LAST_YEAR: f"Прошлый год [{now.year-1}]",
            }
        else:
            titles = {
                RankType.TOTAL: "总发言排行榜",
                RankType.DAILY: f"今日[{now.year}年{now.month}月{now.day}日]发言榜单",
                RankType.WEEKLY: f"本周[{now.year}年{now.month}月第{now.isocalendar().week}周]发言榜单",
                RankType.MONTHLY: f"本月[{now.year}年{now.month}月]发言榜单",
                RankType.YEARLY: f"本年[{now.year}年]发言榜单",
                RankType.LAST_YEAR: f"去年[{now.year-1}年]发言榜单",
            }
        return titles.get(rank_type, "发言榜单")
    
    def _generate_text_message(self, users_with_values, group_info, title, config):
        total = sum(v for _, v in users_with_values)
        top = users_with_values[:config.rand]
        msg = [f"{title}\n发言总数: {total}\n━━━━━━━━━━━━━━\n"]
        for i, (user, cnt) in enumerate(top):
            pct = (cnt / total * 100) if total > 0 else 0
            msg.append(f"第{i+1}名:{user.nickname}·{cnt}次({pct:.2f}%)\n")
        return ''.join(msg)
    
    @filter.command("发言榜定时状态")
    async def timer_status(self, event: AstrMessageEvent):
        """查看定时任务状态"""
        try:
            config = self.plugin_config
            lines = [
                "📊 定时任务状态", "━━━━━━━━━━━━━━━━━━━━",
                f"定时功能: {'✅ 已启用' if config.timer_enabled else '❌ 已禁用'}",
                f"推送时间: {config.timer_push_time}",
                f"排行榜类型: {self._get_rank_type_text(config.timer_rank_type)}",
                f"推送模式: {'图片' if config.if_send_pic else '文字'}",
                f"显示人数: {config.rand} 人",
            ]
            if config.timer_target_groups:
                lines.append("目标群组:")
                for gid in config.timer_target_groups:
                    status = "✅" if str(gid) in self.group_unified_msg_origins else "❌"
                    lines.append(f"  {status} {gid}")
            if self.timer_manager:
                ts = await self.timer_manager.get_status()
                lines.extend([f"运行状态: {self._get_status_text(ts['status'])}", f"下次推送: {ts['next_push_time'] or '未设置'}"])
            yield event.plain_result('\n'.join(lines))
        except Exception as e:
            self.logger.error(f"获取定时状态失败: {e}")
            yield event.plain_result("获取定时状态失败，请稍后重试！")
    
    @filter.command("手动推送发言榜")
    async def manual_push(self, event: AstrMessageEvent):
        """手动推送排行榜"""
        try:
            if not self.timer_manager:
                yield event.plain_result("定时管理器未初始化，无法执行手动推送！")
                return
            config = self.plugin_config
            if not config.timer_target_groups:
                yield event.plain_result("未设置目标群组，请先使用 #设置定时群组 设置目标群组！")
                return
            yield event.plain_result("正在执行手动推送，请稍候...")
            if await self.timer_manager.manual_push(config):
                yield event.plain_result("✅ 手动推送执行成功！")
            else:
                yield event.plain_result("❌ 手动推送执行失败！\n\n请检查 unified_msg_origin 是否已收集")
        except (AttributeError, TypeError) as e:
            self.logger.error(f"处理手动推送请求失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
        except (RuntimeError, ValueError, KeyError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.error(f"处理手动推送请求失败(运行时错误): {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("设置发言榜定时时间")
    async def set_timer_time(self, event: AstrMessageEvent):
        """设置定时推送时间，自动设置当前群组为定时群组并启用定时功能"""
        try:
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            if not args:
                yield event.plain_result("请指定时间！用法:#设置定时时间 16:12")
                return
            time_str = args[0]
            if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
                yield event.plain_result("时间格式错误！请使用 HH:MM 格式")
                return
            group_id = event.get_group_id()
            config = self.plugin_config
            config.timer_push_time = time_str
            if group_id and str(group_id) not in config.timer_target_groups:
                config.timer_target_groups.append(str(group_id))
            config.timer_enabled = True
            if self.timer_manager:
                success = await self.timer_manager.update_config(config, self.group_unified_msg_origins)
                if success:
                    yield event.plain_result(f"✅ 定时推送设置完成！推送时间：{time_str}")
                else:
                    yield event.plain_result(f"⚠️ 配置已保存，但定时任务启动失败")
            else:
                yield event.plain_result(f"✅ 配置已保存")
        except Exception as e:
            self.logger.error(f"设置定时时间失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("设置发言榜定时群组")
    async def set_timer_groups(self, event: AstrMessageEvent):
        """设置定时推送目标群组"""
        try:
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            if not args:
                yield event.plain_result("请指定群组ID！用法:#设置发言榜定时群组 123456789 987654321")
                return
            valid = [g for g in args if g.isdigit() and len(g) >= 5]
            config = self.plugin_config
            config.timer_target_groups = valid
            if self.timer_manager and config.timer_enabled:
                await self.timer_manager.update_config(config, self.group_unified_msg_origins)
            yield event.plain_result(f"✅ 定时推送目标群组已设置：\n" + "\n".join(f"   • {g}" for g in valid))
        except Exception as e:
            self.logger.error(f"设置定时群组失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("删除发言榜定时群组")
    async def remove_timer_groups(self, event: AstrMessageEvent):
        """删除定时推送目标群组"""
        try:
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            config = self.plugin_config
            if not args:
                config.timer_target_groups = []
                yield event.plain_result("✅ 已清空所有定时推送目标群组")
            else:
                config.timer_target_groups = [g for g in config.timer_target_groups if g not in args]
                yield event.plain_result("✅ 已删除指定群组")
            if self.timer_manager and config.timer_enabled:
                await self.timer_manager.update_config(config, self.group_unified_msg_origins)
        except Exception as e:
            self.logger.error(f"删除定时群组失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("启用发言榜定时")
    async def enable_timer(self, event: AstrMessageEvent):
        """启用定时推送功能"""
        try:
            config = self.plugin_config
            if not config.timer_target_groups:
                yield event.plain_result("请先设置目标群组！用法:#设置定时群组 群组ID")
                return
            config.timer_enabled = True
            if self.timer_manager:
                if await self.timer_manager.update_config(config, self.group_unified_msg_origins):
                    yield event.plain_result("✅ 定时推送功能已启用！")
                else:
                    yield event.plain_result("⚠️ 定时推送功能启用失败，请检查配置！")
            else:
                yield event.plain_result("⚠️ 定时管理器未初始化！")
        except Exception as e:
            self.logger.error(f"启用定时失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("禁用发言榜定时")
    async def disable_timer(self, event: AstrMessageEvent):
        """禁用定时推送功能"""
        try:
            config = self.plugin_config
            config.timer_enabled = False
            if self.timer_manager:
                await self.timer_manager.stop_timer()
            yield event.plain_result("✅ 定时推送功能已禁用！")
        except Exception as e:
            self.logger.error(f"禁用定时失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    @filter.command("设置发言榜定时类型")
    async def set_timer_type(self, event: AstrMessageEvent):
        """设置定时推送的排行榜类型"""
        try:
            args = event.message_str.split()[1:] if hasattr(event, 'message_str') else []
            if not args:
                yield event.plain_result("请指定排行榜类型！用法:#设置定时类型 total/daily/week/month")
                return
            config = self.plugin_config
            config.timer_rank_type = args[0].lower()
            if self.timer_manager and config.timer_enabled:
                await self.timer_manager.update_config(config, self.group_unified_msg_origins)
            yield event.plain_result(f"✅ 定时推送排行榜类型已设置为 {self._get_rank_type_text(config.timer_rank_type)}！")
        except Exception as e:
            self.logger.error(f"设置定时类型失败: {e}")
            yield event.plain_result("处理请求失败，请稍后重试！")
    
    def _get_status_text(self, status: str) -> str:
        return {'stopped': '已停止', 'running': '运行中', 'error': '错误', 'paused': '已暂停'}.get(status, status)
    
    def _get_rank_type_text(self, rank_type: str) -> str:
        return {
            'total': '总排行榜', 'daily': '今日排行榜', 'week': '本周排行榜',
            'weekly': '本周排行榜', 'month': '本月排行榜', 'monthly': '本月排行榜'
        }.get(rank_type, rank_type)
