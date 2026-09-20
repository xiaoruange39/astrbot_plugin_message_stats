"""排行榜数据准备、输出调度、文字格式化和 T2I 能力。"""

import asyncio
import base64
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api.event import AstrMessageEvent

from .exception_handlers import ExceptionConfig, exception_handler
from .image_generator import ImageGenerationError
from .llm_analyzer import LLMAnalyzer
from .models import GroupInfo, PluginConfig, RankType, UserData
from .platform_helper import PlatformHelper
from .group_id_utils import get_fallback_group_name, is_placeholder_group_name


CUSTOM_RANK_DATE_TOKEN = r"\d{4}年\d{1,2}月\d{1,2}日"
CUSTOM_RANK_PERIOD_PATTERN = (
    rf"(?P<start>{CUSTOM_RANK_DATE_TOKEN})"
    rf"(?:\s*(?:-|—|－|~|～|至|到)\s*(?P<end>{CUSTOM_RANK_DATE_TOKEN}))?"
)
CUSTOM_DATE_RANK_MESSAGE_PATTERN = re.compile(
    rf"^(?:[#＃/／]\s*)?{CUSTOM_RANK_PERIOD_PATTERN}\s*发言榜$"
)

# PDF 文件名非法字符：Windows/通用文件系统保留字符 + 方括号装饰 + 控制字符
_PDF_FILENAME_INVALID_CHARS = re.compile(r'[\[\]<>:"/\\|?*\x00-\x1f]')


class RankingMixin:
    """排行榜数据准备、输出调度、文字格式化和 T2I 能力。"""

    async def _show_rank(
        self,
        event: AstrMessageEvent,
        rank_type: Optional[RankType] = None,
        custom_period: Optional[tuple[date, date]] = None,
    ):
        """显示排行榜 - 重构版本"""
        try:
            # 阻止指令执行后框架默认再调用一次 LLM 回复（避免无谓消耗 token）
            # 插件注册了全量消息监听器会使 is_wake=True，导致指令处理完仍触发默认 LLM
            event.should_call_llm(False)

            # 检查群聊是否在屏蔽列表中
            group_id = event.get_group_id()
            if group_id and self._is_blocked_group(str(group_id)):
                return

            # 准备数据
            rank_data = await self._prepare_rank_data(event, rank_type, custom_period)
            if rank_data is None:
                if custom_period:
                    period_label = self._format_custom_rank_period(*custom_period)
                    yield event.plain_result(f"{period_label}暂无发言记录")
                else:
                    yield event.plain_result("无法获取排行榜数据,请检查群组信息或稍后重试")
                return

            group_id, current_user_id, filtered_data, config, title, group_info = rank_data

            # 如果启用了手动LLM分析，调用LLM生成头衔
            token_usage_info = None
            titles_map = None
            need_llm = config.llm_enabled and config.llm_enable_on_manual
            if need_llm:
                try:
                    # 获取群组数据用于LLM分析
                    group_data = await self.data_manager.get_group_data(group_id)
                    if group_data:
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

                        grp_name = group_info.group_name if not is_placeholder_group_name(group_info.group_name, group_id) else get_fallback_group_name(group_id)

                        # 修复：llm_title 可能存为空字符串 ""（旧数据污染），统一视为无头衔处理
                        # 同时无头衔用户也需要满足 min_daily_messages 才触发 LLM，避免低发言用户频繁触发
                        ranked_users_for_llm = filtered_data[:config.rand]
                        users_need_llm = []
                        users_with_title = []
                        min_daily = getattr(config, 'llm_min_daily_messages', 0)
                        for u, period_count in ranked_users_for_llm:
                            # 判断是否有有效头衔（兼容空字符串脏数据）
                            has_title = bool(u.llm_title and u.llm_title.strip())
                            # 上次生成头衔以来的总发言增量（不是当前周期发言数）
                            increment = u.message_count - u.llm_title_message_count

                            if not has_title:
                                # 无头衔用户：总发言增量达到阈值才触发 LLM
                                if min_daily > 0 and increment < min_daily:
                                    users_with_title.append(u)
                                    continue
                                users_need_llm.append(u)
                            elif u.llm_title_message_count == 0:
                                # 旧数据没有记录生成时的发言数，按增量判断
                                if min_daily > 0 and increment >= min_daily:
                                    users_need_llm.append(u)
                                else:
                                    users_with_title.append(u)
                            elif min_daily > 0 and increment >= min_daily:
                                # 上次生成头衔以来发言增量达到阈值，重新生成
                                users_need_llm.append(u)
                            else:
                                users_with_title.append(u)

                        if users_with_title:
                            self.logger.info(f"跳过 {len(users_with_title)} 个增量不足的用户，保留现有头衔")

                        titles = None
                        token_usage = None
                        if users_need_llm:
                            self.logger.info(f"为 {len(users_need_llm)} 个无头衔用户调用LLM生成头衔")
                            titles, token_usage = await llm_analyzer.analyze_users(
                                users_need_llm, grp_name, min_daily_messages=min_daily
                            )

                        if token_usage and token_usage.get("total_tokens", 0) > 0:
                            token_usage_info = token_usage

                        # 构建完整的titles_map：已有头衔 + 新生成的头衔
                        titles_map = {}

                        # 1. 先加载已有持久化头衔
                        for user_data_item, _ in filtered_data:
                            if user_data_item.llm_title:
                                titles_map[user_data_item.user_id] = {
                                    "title": user_data_item.llm_title,
                                    "color": user_data_item.llm_title_color or "#7C3AED"
                                }

                        # 2. 再合并新生成的头衔（覆盖旧头衔，因为LLM可能对之前无头衔的用户生成了新头衔）
                        if titles:
                            self.logger.info(f"✅ LLM头衔生成成功: 为 {len(titles)} 个新用户生成了头衔")
                            for user_data_item, _ in filtered_data:
                                if user_data_item.user_id in titles:
                                    info = titles[user_data_item.user_id]
                                    if isinstance(info, dict):
                                        title_text = info.get("title")
                                        title_color = info.get("color")
                                        user_data_item.display_title = title_text
                                        user_data_item.display_title_color = title_color
                                    else:
                                        title_text = info
                                        user_data_item.display_title = title_text
                                        user_data_item.display_title_color = None
                                    # 写入持久化字段
                                    user_data_item.llm_title = title_text
                                    user_data_item.llm_title_color = title_color if isinstance(info, dict) else None
                                    user_data_item.llm_title_message_count = user_data_item.message_count
                                    titles_map[user_data_item.user_id] = {
                                        "title": user_data_item.llm_title,
                                        "color": user_data_item.llm_title_color or "#7C3AED"
                                    }
                            # 保存群组数据到文件，确保头衔持久化
                            group_data_for_save = await self.data_manager.get_group_data(group_id)
                            if group_data_for_save:
                                await self.data_manager.save_group_data(group_id, group_data_for_save)
                                self.logger.info("头衔数据已持久化保存到文件")
                        else:
                            self.logger.info(f"所有用户已有持久化头衔，无需LLM分析，使用已有头衔")


                except Exception as e:
                    self.logger.error(f"❌ 手动LLM头衔生成异常: {e}", exc_info=True)

            # 根据配置选择渲染模式
            render_mode = getattr(config, 'render_mode', 'playwright')
            if render_mode == 'text':
                async for result in self._render_rank_as_text(event, filtered_data, group_info, title, config):
                    yield result
            elif render_mode == 'pdf':
                # PDF 模式：复用图片版 HTML 渲染成 PDF 文件发送，失败自动降级图片/文字
                async for result in self._render_rank_as_pdf(event, filtered_data, group_info, title, current_user_id, config, token_usage_info, titles_map):
                    yield result
            else:
                # playwright 或 t2i 都走图片渲染（playwright 失败自动降级 t2i）
                async for result in self._render_rank_as_image(event, filtered_data, group_info, title, current_user_id, config, token_usage_info, titles_map):
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

    async def _prepare_rank_data(
        self,
        event: AstrMessageEvent,
        rank_type: Optional[RankType],
        custom_period: Optional[tuple[date, date]] = None,
    ):
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

        # 生成排行榜时缓存群组名称（供 Web 面板使用）
        await self._cache_group_name(event, group_id)

        # 获取群组数据
        group_data = await self.data_manager.get_group_data(group_id)

        if not group_data:
            return None

        # 加载持久化的头衔到运行时字段
        # 确保即使不触发LLM分析，排行榜也能显示已有的头衔
        for user in group_data:
            if user.llm_title:
                user.display_title = user.llm_title
                if user.llm_title_color:
                    user.display_title_color = user.llm_title_color

        # 显示排行榜前刷新当前群展示名，确保图片/榜单展示尽量使用最新群名片
        await self._refresh_display_names_for_ranking(event, group_id, group_data)


        # 根据类型筛选数据并获取排序值
        if custom_period:
            filtered_data_with_values = await self._calculate_period_rank_optimized(
                group_data,
                custom_period[0],
                custom_period[1],
            )
        elif rank_type is not None:
            filtered_data_with_values = await self._filter_data_by_rank_type(group_data, rank_type)
        else:
            return None

        if not filtered_data_with_values:
            return None

        # 对数据进行排序
        filtered_data = sorted(filtered_data_with_values, key=lambda x: x[1], reverse=True)
        self._apply_rank_trend_labels(group_data, filtered_data, rank_type, custom_period)

        # 获取配置
        config = self.plugin_config

        # 生成标题
        if custom_period:
            title = self._generate_custom_rank_title(*custom_period)
        else:
            title = self._generate_title(rank_type)

        # 创建群组信息
        unified_msg_origin = self.group_unified_msg_origins.get(group_id, "")
        group_info = GroupInfo(group_id=group_id, unified_msg_origin=unified_msg_origin)

        # 获取群名称
        group_name = await self._get_group_name(event, group_id)
        group_info.group_name = group_name

        return group_id, current_user_id, filtered_data, config, title, group_info

    def _apply_rank_trend_labels(
        self,
        group_data: List[UserData],
        filtered_data: List[tuple],
        rank_type: Optional[RankType],
        custom_period: Optional[tuple[date, date]] = None,
    ) -> None:
        """为排行榜用户填充跨周期趋势展示字段。"""
        for user in group_data:
            user.display_trend_label = None
            user.display_trend_type = None

        if not filtered_data or not getattr(self.plugin_config, "rank_trend_enabled", True):
            return

        if rank_type == RankType.TOTAL and custom_period is None:
            trend_days = getattr(self.plugin_config, "rank_trend_days", 7)
            try:
                trend_days = max(2, min(30, int(trend_days)))
            except (TypeError, ValueError):
                trend_days = 7
            self._apply_total_rank_trends(filtered_data, trend_days=trend_days)
            return

        if custom_period:
            start_date, end_date = custom_period
        elif rank_type is not None:
            start_date, end_date, _ = self._get_time_period_for_rank_type(rank_type)
        else:
            return

        if not start_date or not end_date:
            return

        comparison_label = self._get_trend_comparison_label(
            rank_type,
            custom_period,
        )
        previous_start, previous_end = self._get_previous_period_range(start_date, end_date)
        previous_rank = self._calculate_period_rank_sync(group_data, previous_start, previous_end)
        previous_rank = sorted(previous_rank, key=lambda x: x[1], reverse=True)
        previous_counts = {user.user_id: count for user, count in previous_rank}
        previous_ranks = {user.user_id: index + 1 for index, (user, _) in enumerate(previous_rank)}

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
    def _get_trend_comparison_label(
        rank_type: Optional[RankType],
        custom_period: Optional[tuple[date, date]],
    ) -> str:
        if custom_period:
            return "上一周期"

        return {
            RankType.DAILY: "昨天",
            RankType.YESTERDAY: "前天",
            RankType.WEEKLY: "上周",
            RankType.MONTHLY: "上月",
            RankType.YEARLY: "去年",
            RankType.LAST_YEAR: "前年",
        }.get(rank_type, "上一周期")

    @staticmethod
    def _get_previous_period_range(start_date: date, end_date: date) -> tuple[date, date]:
        period_days = (end_date - start_date).days + 1
        previous_end = start_date - timedelta(days=1)
        previous_start = previous_end - timedelta(days=period_days - 1)
        return previous_start, previous_end

    def _calculate_period_rank_sync(self, group_data: List[UserData], start_date: date, end_date: date) -> List[tuple]:
        ranked = []
        for user in group_data:
            if self._is_blocked_user(user.user_id):
                continue
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
    def _format_rank_delta_label(current_rank: int, previous_rank: Optional[int]) -> str:
        if not previous_rank:
            return ""

        delta = previous_rank - current_rank
        if delta > 0:
            return f"名次上升{delta}位"
        if delta < 0:
            return f"名次下降{abs(delta)}位"
        return "名次不变"

    def _apply_total_rank_trends(self, filtered_data: List[tuple], trend_days: int = 7) -> None:
        today = date.today()
        for user, _ in filtered_data:
            daily_counts = []
            for offset in range(trend_days - 1, -1, -1):
                day = today - timedelta(days=offset)
                daily_counts.append(user.get_message_count_in_period(day, day))

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

    def _parse_custom_rank_period(self, query: str) -> tuple[date, date]:
        """解析单日或日期区间排行榜查询。"""
        text = str(query or "").strip()
        text = re.sub(r"^(?:[#＃/／]\s*)", "", text)
        text = re.sub(r"\s*发言榜$", "", text).strip()
        match = re.fullmatch(CUSTOM_RANK_PERIOD_PATTERN, text)
        if not match:
            raise ValueError(
                "日期格式错误，请使用“2026年6月1日”或"
                "“2026年5月30日-2026年6月1日”"
            )

        try:
            start_date = self._parse_custom_rank_date(match.group("start"))
            end_date = (
                self._parse_custom_rank_date(match.group("end"))
                if match.group("end")
                else start_date
            )
        except ValueError as exc:
            raise ValueError("日期无效，请检查年月日") from exc

        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")
        if end_date > date.today():
            raise ValueError("不能查询未来日期的发言榜")
        return start_date, end_date

    @staticmethod
    def _parse_custom_rank_date(value: str) -> date:
        match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
        if not match:
            raise ValueError("invalid date format")
        return date(*(int(part) for part in match.groups()))

    @staticmethod
    def _format_custom_rank_date(value: date) -> str:
        return f"{value.year}年{value.month}月{value.day}日"

    def _format_custom_rank_period(self, start_date: date, end_date: date) -> str:
        start_text = self._format_custom_rank_date(start_date)
        if start_date == end_date:
            return start_text
        return f"{start_text}—{self._format_custom_rank_date(end_date)}"

    def _generate_custom_rank_title(self, start_date: date, end_date: date) -> str:
        return f"[{self._format_custom_rank_period(start_date, end_date)}]发言榜单"

    async def _refresh_display_names_for_ranking(self, event: AstrMessageEvent, group_id: str, group_data):
        """排行榜显示前刷新当前群展示名，确保显示最新昵称"""
        try:
            # 获取最新群成员信息
            members_info = await self._fetch_group_members_from_api(event, group_id)
            if not members_info:
                return

            # 重建群成员字典缓存（使用 PlatformHelper 跨平台通用方式获取用户ID）
            dict_cache_key = f"group_members_dict_{group_id}"
            members_dict = {}
            for m in members_info:
                uid = PlatformHelper.get_user_id_from_member(m)
                if uid:
                    members_dict[uid] = m
            self.member_cache.group_members_dict_cache[dict_cache_key] = members_dict

            # 更新用户数据中的昵称
            updated_count = 0
            for user in group_data:
                user_id = user.user_id
                if user_id in members_dict:
                    member = members_dict[user_id]
                    display_name = self.member_cache.get_display_name_from_member(member)
                    if display_name and user.nickname != display_name:
                        # 更新当前群持久化展示名
                        old_nickname = user.nickname
                        user.nickname = display_name
                        updated_count += 1

                        if self.plugin_config.detailed_logging_enabled:
                            self.logger.debug(f"排行榜刷新展示名: {old_nickname} → {display_name}")

            # 保存更新后的数据
            if updated_count > 0:
                await self.data_manager.save_group_data(group_id, group_data)
                if self.plugin_config.detailed_logging_enabled:
                    self.logger.info(f"排行榜显示前更新了 {updated_count} 个用户的展示名")

        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, IOError, OSError, ConnectionError, asyncio.TimeoutError) as e:
            self.logger.warning(f"排行榜前刷新展示名失败: {e}")

    async def _render_rank_as_image(self, event: AstrMessageEvent, filtered_data: List[tuple],
                                  group_info: GroupInfo, title: str, current_user_id: str, config: PluginConfig,
                                  llm_token_usage: Dict[str, int] = None,
                                  titles_map: Optional[Dict[str, str]] = None):
        """渲染排行榜为图片模式"""
        temp_path = None
        try:
            # 提取用户数据用于图片生成，并应用人数限制
            # 先限制数量，再提取用户数据
            limited_data = filtered_data[:config.rand]
            users_for_image = []

            # 为用户数据设置display_total属性，确保图片生成器使用正确的数据
            # 修复：直接命令版排行榜图片显示错误数据的问题
            for user_data, count in limited_data:
                # 设置display_total属性（时间段内的发言数）
                user_data.display_total = count
                users_for_image.append(user_data)

            if not self.image_generator:
                text_msg = self._generate_text_message(filtered_data, group_info, title, config)
                yield event.plain_result(text_msg)
                return

            # 图片渲染（playwright / t2i 双向试探）
            temp_path = None
            try:
                render_mode = getattr(config, 'render_mode', 'playwright')
                if render_mode == 't2i':
                    temp_path = await self._try_t2i_render(users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map)
                    if not temp_path:
                        yield event.plain_result("⚠️ t2i 渲染失败，正在尝试 Playwright...")
                        temp_path = await self._try_playwright_render(users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map)
                else:
                    temp_path = await self._try_playwright_render(users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map)
                    if not temp_path:
                        yield event.plain_result("⚠️ Playwright 渲染失败，正在尝试 t2i...")
                        temp_path = await self._try_t2i_render(users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map)

                if not temp_path:
                    raise ImageGenerationError("Playwright 和 t2i 均渲染失败")
            except ImageGenerationError:
                raise


            # 检查图片路径是否存在（t2i 返回 URL，playwright 返回本地路径）
            if temp_path and (str(temp_path).startswith('http') or os.path.exists(temp_path)):
                yield event.image_result(str(temp_path))
                if os.path.exists(temp_path):
                    self._schedule_file_cleanup(str(temp_path))
            else:
                # 回退到文字模式
                text_msg = self._generate_text_message(filtered_data, group_info, title, config)
                yield event.plain_result(text_msg)

        except ImageGenerationError as e:
            self.logger.error(f"图片渲染失败（Playwright/t2i 均不可用）: {e}")
            # 最终降级文字
            text_msg = self._generate_text_message(filtered_data, group_info, title, config)
            yield event.plain_result(f"⚠️ 图片渲染失败，已自动切换为文字模式\n\n{text_msg}")
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
        except AttributeError as e:
            self.logger.error(f"图片渲染失败(生成器未初始化): {e}")
            text_msg = self._generate_text_message(filtered_data, group_info, title, config)
            yield event.plain_result(text_msg)
        finally:
            pass

    async def _render_rank_as_pdf(self, event: AstrMessageEvent, filtered_data: List[tuple],
                                  group_info: GroupInfo, title: str, current_user_id: str, config: PluginConfig,
                                  llm_token_usage: Dict[str, int] = None,
                                  titles_map: Optional[Dict[str, str]] = None):
        """渲染排行榜为 PDF 文件模式

        复用图片版 HTML 生成矢量 PDF（外观与图片一致），通过 File 组件作为文件发送。
        生成或发送失败时都会自动降级到图片模式（其内部含 playwright→t2i→文字 的完整降级链）。

        跨容器部署注意：OneBot(aiocqhttp/napcat) 常与 AstrBot 分处不同容器，File 会被框架
        转成本地路径 / `file://` URI 交给 napcat，napcat 读不到 → 报 retcode 1200「路径不存在」。
        框架的标准解法是配置 `callback_api_base`（填 napcat 能访问到的地址），此时 File 会被
        自动注册成 http 链接下发。这里通过 `await event.send(...)` 主动发送并捕获平台异常，
        发送失败时自动改发图片（图片走 base64，天然跨容器），并提示用户配置 callback_api_base。
        """
        # 提取用户数据并应用人数限制（与图片模式一致）
        limited_data = filtered_data[:config.rand]
        users_for_pdf = []
        for user_data, count in limited_data:
            # 设置 display_total 属性（时间段内的发言数）
            user_data.display_total = count
            users_for_pdf.append(user_data)

        # 图片生成器不可用：直接降级文字
        if not self.image_generator:
            text_msg = self._generate_text_message(filtered_data, group_info, title, config)
            yield event.plain_result(text_msg)
            return

        pdf_path = None
        try:
            pdf_path = await self.image_generator.generate_rank_pdf(
                users_for_pdf, group_info, title, current_user_id, llm_token_usage, titles_map
            )
        except ImageGenerationError as e:
            self.logger.warning(f"PDF 渲染失败，降级为图片/文字: {e}")
        except Exception as e:
            self.logger.warning(f"PDF 渲染异常，降级为图片/文字: {type(e).__name__}: {e}")

        # 生成成功：用 File 组件发送 PDF 文件
        if pdf_path and os.path.exists(pdf_path):
            from astrbot.core.message.components import File
            filename = self._build_pdf_filename(title)
            try:
                # 主动 await event.send()（而非 yield），以便捕获平台发送异常并降级；
                # 分离部署未配置 callback_api_base 时，napcat 读不到本地路径会抛 ActionFailed。
                await event.send(event.chain_result([File(name=filename, file=str(pdf_path))]))
                self._schedule_file_cleanup(str(pdf_path))
                return
            except Exception as e:
                self._schedule_file_cleanup(str(pdf_path))
                self.logger.warning(
                    f"PDF 文件发送失败，降级为图片: {type(e).__name__}: {e}"
                )
                yield event.plain_result(
                    "⚠️ PDF 发送失败，已改用图片模式。\n"
                    "若 OneBot(napcat) 与 AstrBot 分离部署（不同容器/主机），"
                    "请在 AstrBot 配置项 callback_api_base 填写 napcat 可访问到的地址后重试。"
                )
                async for result in self._render_rank_as_image(
                    event, filtered_data, group_info, title, current_user_id, config, llm_token_usage, titles_map
                ):
                    yield result
                return

        # PDF 生成失败：降级到图片模式（内部再降级 t2i/文字）
        yield event.plain_result("⚠️ PDF 生成失败，已切换为图片/文字模式")
        async for result in self._render_rank_as_image(
            event, filtered_data, group_info, title, current_user_id, config, llm_token_usage, titles_map
        ):
            yield result

    def _build_pdf_filename(self, title: str) -> str:
        """把排行榜标题清洗成合法的 PDF 文件名，并追加日期后缀

        例：'总发言排行榜' → '总发言排行榜_20260822.pdf'
            '[2026年8月1日]发言榜单' → '2026年8月1日发言榜单_20260822.pdf'
        """
        safe_title = _PDF_FILENAME_INVALID_CHARS.sub("", str(title or "")).strip().strip("_")
        if not safe_title:
            safe_title = "发言排行榜"
        date_suffix = datetime.now().strftime("%Y%m%d")
        return f"{safe_title}_{date_suffix}.pdf"

    async def _try_playwright_render(self, users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map):
        """尝试 playwright 渲染，成功返回路径，失败返回 None"""
        try:
            return await self.image_generator.generate_rank_image(
                users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map
            )
        except ImageGenerationError as e:
            self.logger.warning(f"Playwright 渲染失败: {e}")
            return None

    async def _try_t2i_render(self, users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map):
        """尝试官方 t2i 渲染，按浏览器内容宽度返回本地图片路径。"""
        try:
            html_content = await self.image_generator._generate_html(
                users_for_image, group_info, title, current_user_id, llm_token_usage, titles_map
            )
            target_width = self._get_t2i_target_width(html_content)
            t2i_html = self._prepare_t2i_html(html_content, target_width)

            for options in self._t2i_render_options():
                try:
                    render_result = await self.html_render(
                        t2i_html,
                        {},
                        return_url=False,
                        options=options,
                    )
                    image_path = self._store_t2i_render_result(
                        render_result,
                        target_width,
                    )
                    if image_path:
                        return image_path
                    self.logger.warning(
                        f"t2i 返回了无效图片，尝试下一渲染策略: {options}"
                    )
                except Exception as e:
                    self.logger.warning(
                        f"t2i 渲染策略失败: {type(e).__name__}: {e}"
                    )
            return None
        except Exception as e:
            self.logger.warning(f"t2i 渲染失败: {e}")
            return None

    @staticmethod
    def _t2i_render_options() -> List[Dict[str, Any]]:
        """返回兼容 AstrBot 本地/远程服务的截图策略。"""
        return [
            {
                "full_page": True,
                "type": "png",
                "animations": "disabled",
                "scale": "css",
                "timeout": 50_000,
            },
            {
                "full_page": True,
                "type": "jpeg",
                "quality": 80,
                "animations": "disabled",
                "scale": "css",
                "timeout": 100_000,
            },
        ]

    @staticmethod
    def _get_t2i_target_width(html_content: str) -> int:
        """复用 Playwright 的 container 宽度加 100px 画布规则。"""
        match = re.search(
            r"\.container\s*\{[^{}]*?max-width\s*:\s*(\d+)px",
            html_content,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return 1200
        return max(320, min(1200, int(match.group(1)) + 100))

    @staticmethod
    def _prepare_t2i_html(html_content: str, target_width: int) -> str:
        """固定 CSS 排版画布，图片本身仍由内容自然决定高度。"""
        canvas_style = f"""
<style id="message-stats-t2i-canvas">
  html, body {{
    width: {target_width}px !important;
    min-width: {target_width}px !important;
    max-width: {target_width}px !important;
  }}
  body {{
    min-height: 0 !important;
    overflow-x: hidden !important;
  }}
</style>
"""
        if "</head>" in html_content:
            return html_content.replace("</head>", f"{canvas_style}</head>", 1)
        return f"{canvas_style}{html_content}"

    def _store_t2i_render_result(
        self,
        render_result: Any,
        target_width: int,
    ) -> Optional[str]:
        """保存并裁切 AstrBot T2I 返回的 bytes/base64/文件结果。"""
        if isinstance(render_result, (bytes, bytearray)):
            return self._store_t2i_image_bytes(
                bytes(render_result),
                target_width,
            )

        text = str(render_result or "").strip()
        if not text or text.startswith("<"):
            return None
        if text.startswith("base64://"):
            try:
                data = base64.b64decode(text.removeprefix("base64://"))
            except Exception as e:
                self.logger.warning(f"解析 t2i base64 图片失败: {e}")
                return None
            return self._store_t2i_image_bytes(data, target_width)
        if text.lower().startswith("data:image/"):
            try:
                _, payload = text.split(",", 1)
                data = base64.b64decode(payload)
            except Exception as e:
                self.logger.warning(f"解析 t2i data-uri 图片失败: {e}")
                return None
            return self._store_t2i_image_bytes(data, target_width)
        if text.startswith(("http://", "https://")):
            self.logger.warning(
                "t2i 在 return_url=False 时仍返回 URL，无法执行本地画布裁切"
            )
            return text

        image_path = Path(text)
        if not image_path.is_file():
            return None
        try:
            header = image_path.read_bytes()[:16]
        except OSError:
            return None
        if not self._t2i_image_suffix(header):
            return None
        if not self._trim_t2i_canvas(image_path, target_width):
            return None
        return str(image_path)

    def _store_t2i_image_bytes(
        self,
        data: bytes,
        target_width: int,
    ) -> Optional[str]:
        suffix = self._t2i_image_suffix(data)
        if not suffix:
            return None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="message_stats_t2i_",
                suffix=suffix,
                delete=False,
            ) as image_file:
                image_file.write(data)
                image_path = Path(image_file.name)
            if not self._trim_t2i_canvas(image_path, target_width):
                image_path.unlink(missing_ok=True)
                return None
            return str(image_path)
        except OSError as e:
            self.logger.warning(f"保存 t2i 图片失败: {e}")
            return None

    @staticmethod
    def _t2i_image_suffix(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8"):
            return ".jpg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        return ""

    def _trim_t2i_canvas(self, image_path: Path, target_width: int) -> bool:
        """仅移除默认 T2I 视口右侧区域，保留浏览器自然内容高度。"""
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                if image.width <= target_width or target_width <= 0:
                    image.load()
                    return True
                cropped = image.crop((0, 0, target_width, image.height))
                suffix = image_path.suffix.lower()
                if suffix in {".jpg", ".jpeg"}:
                    cropped.convert("RGB").save(
                        image_path,
                        format="JPEG",
                        quality=92,
                        optimize=True,
                    )
                elif suffix == ".webp":
                    cropped.save(
                        image_path,
                        format="WEBP",
                        quality=92,
                    )
                else:
                    cropped.save(image_path, format="PNG")
            return True
        except Exception as e:
            self.logger.debug(
                f"t2i 图片校验或画布裁切失败: {type(e).__name__}: {e}"
            )
            return False

    async def _render_rank_as_text(self, event: AstrMessageEvent, filtered_data: List[tuple],
                                 group_info: GroupInfo, title: str, config: PluginConfig):
        """渲染排行榜为文字模式"""
        text_msg = self._generate_text_message(filtered_data, group_info, title, config)
        yield event.plain_result(text_msg)

    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
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
        elif rank_type == RankType.YESTERDAY:
            yesterday = current_date - timedelta(days=1)
            return yesterday, yesterday, "yesterday"
        elif rank_type == RankType.WEEKLY:
            # 获取本周开始日期(周一)
            days_since_monday = current_date.weekday()
            week_start = current_date - timedelta(days=days_since_monday)
            return week_start, current_date, "weekly"
        elif rank_type == RankType.MONTHLY:
            # 获取本月开始日期
            month_start = current_date.replace(day=1)
            return month_start, current_date, "monthly"
        elif rank_type == RankType.YEARLY:
            # 获取本年开始日期
            year_start = current_date.replace(month=1, day=1)
            return year_start, current_date, "yearly"
        elif rank_type == RankType.LAST_YEAR:
            # 获取去年的时间范围（1月1日 - 12月31日）
            last_year = current_date.year - 1
            year_start = date(last_year, 1, 1)
            year_end = date(last_year, 12, 31)
            return year_start, year_end, "lastyear"
        else:
            return None, None, "unknown"

    async def _filter_data_by_rank_type(self, group_data: List[UserData], rank_type: RankType) -> List[tuple]:
        """根据排行榜类型筛选数据并计算时间段内的发言次数 - 性能优化版本"""
        start_date, end_date, period_name = self._get_time_period_for_rank_type(rank_type)

        if rank_type == RankType.TOTAL:
            # 总榜：返回每个用户及其总发言数的元组，但过滤掉从未发言的用户和屏蔽用户
            return [(user, user.message_count) for user in group_data
                   if user.message_count > 0 and not self._is_blocked_user(user.user_id)]

        # 时间段过滤：优化版本，使用预聚合策略减少双重循环
        # 策略：如果时间段较短（日榜），直接计算；如果时间段较长（周榜/月榜），使用缓存

        # 所有时间段类型统一走批量优化路径
        if rank_type in [RankType.DAILY, RankType.YESTERDAY, RankType.WEEKLY, RankType.MONTHLY, RankType.YEARLY, RankType.LAST_YEAR]:
            return await self._calculate_period_rank_optimized(group_data, start_date, end_date)

        return []

    async def _calculate_period_rank_optimized(self, group_data: List[UserData], start_date, end_date) -> List[tuple]:
        """计算周榜/月榜（优化策略）"""
        # 优化策略：先筛选出有发言记录的用户，然后批量计算
        # 兼容新旧两种存储格式（_message_dates 或 history）
        active_users = [user for user in group_data if user._message_dates or user.history]

        if not active_users:
            return []

        # 批量计算，减少函数调用开销
        filtered_users = []
        for user in active_users:
            # 过滤屏蔽用户
            if self._is_blocked_user(user.user_id):
                continue

            # 使用 UserData.get_message_count_in_period 方法计算
            # 该方法内部使用 _message_dates 字典 O(1) 查询，比遍历 history 列表快得多
            # 并且有 _ensure_message_dates 兜底保护，兼容旧数据格式
            period_count = user.get_message_count_in_period(start_date, end_date)
            if period_count > 0:
                filtered_users.append((user, period_count))

        return filtered_users

    async def _count_messages_in_period_fast(self, history: List, start_date, end_date) -> int:
        """快速计算指定时间段内的消息数量（优化版本）

        如果历史记录未排序，将自动排序后进行计算。
        对于已排序的记录，使用高效的早停算法。
        """
        # 如果历史记录为空，直接返回0
        if not history:
            return 0

        # 完整遍历检查列表是否真正有序，避免采样检查的误判问题
        is_sorted = True
        if len(history) > 1:
            try:
                # 完整遍历检查：确保列表真正有序（优化版本）
                for current_item, next_item in zip(history[:-1], history[1:]):
                    current_date = current_item.to_date() if hasattr(current_item, 'to_date') else current_item
                    next_date = next_item.to_date() if hasattr(next_item, 'to_date') else next_item
                    if current_date > next_date:
                        is_sorted = False
                        break

            except (AttributeError, TypeError):
                # 如果无法比较，假设未排序
                is_sorted = False

        # 如果检测到列表确实有序，使用早停算法
        if is_sorted:
            count = 0
            for hist_date in history:
                # 转换为日期对象
                hist_date_obj = hist_date.to_date() if hasattr(hist_date, 'to_date') else hist_date

                # 检查是否在指定时间段内
                if hist_date_obj < start_date:
                    continue
                if hist_date_obj > end_date:
                    # 已排序，可以提前跳出循环
                    break
                count += 1

            return count

        # 如果检测到列表无序，直接使用无序版本计算
        else:
            return self._count_messages_in_period_unordered(history, start_date, end_date)

    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _count_messages_in_period_unordered(self, history: List, start_date, end_date) -> int:
        """计算指定时间段内的消息数量（适用于未排序的历史记录）"""
        if not history:
            return 0

        count = 0
        for hist_date in history:
            hist_date_obj = hist_date.to_date() if hasattr(hist_date, 'to_date') else hist_date
            if start_date <= hist_date_obj <= end_date:
                count += 1

        return count

    @exception_handler(ExceptionConfig(log_exception=True, reraise=True))
    def _generate_title(self, rank_type: RankType) -> str:
        """生成标题"""
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
            trend_label = getattr(user, 'display_trend_label', None)
            trend_suffix = f" {trend_label}" if trend_label else ""
            msg.append(f"第{i + 1}名:{user.nickname}·{user_messages}次(占比{percentage:.2f}%){trend_suffix}\n")

        return ''.join(msg)
