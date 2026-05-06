"""
图片生成模块
负责将HTML模板转换为排行榜图片
"""

import asyncio
import aiofiles
from pathlib import Path
from typing import List, Optional, Dict, Any, Union
from datetime import datetime
import tempfile
import os
import traceback
import hashlib
import json
import uuid

from astrbot.api import logger as astrbot_logger

# 从集中管理的常量模块导入图片生成配置
from .constants import (
    IMAGE_WIDTH,
    VIEWPORT_HEIGHT,
    BROWSER_TIMEOUT,
    DEFAULT_FONT_SIZE,
    ROW_HEIGHT
)

# Jinja2模板引擎
try:
    from jinja2 import Template, Environment, select_autoescape, FileSystemLoader
    import html  # 用于HTML转义安全防护
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False
    astrbot_logger.warning("Jinja2未安装，将使用不安全的字符串拼接方式")

try:
    from playwright.async_api import async_playwright, Browser, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    astrbot_logger.warning("Playwright未安装，图片生成功能将不可用")

from .models import UserData, GroupInfo, PluginConfig
from .exception_handlers import safe_generation, safe_file_operation




class ImageGenerationError(Exception):
    """图片生成异常
    
    当图片生成过程中发生错误时抛出的自定义异常。
    
    Attributes:
        message (str): 异常消息，描述具体的错误原因
        
    Example:
        >>> raise ImageGenerationError("Playwright未安装，无法生成图片")
    """
    pass


class ImageGenerator:
    """图片生成器
    
    负责将HTML模板转换为排行榜图片。支持Playwright浏览器自动化和Jinja2模板渲染。
    
    主要功能:
        - 使用Playwright浏览器生成高质量排行榜图片
        - 支持Jinja2模板引擎进行安全的HTML渲染
        - 自动调整页面高度和截图尺寸
        - 包含多层回退机制，确保在各种环境下都能正常工作
        - 支持当前用户高亮显示
        - 提供默认模板作为备用方案
        - 模板缓存机制，提高重复渲染效率
        
    Attributes:
        config (PluginConfig): 插件配置对象，包含生成参数
        browser (Optional[Browser]): Playwright浏览器实例
        page (Optional[Page]): Playwright页面实例
        playwright: Playwright实例
        logger: 日志记录器
        width (int): 图片宽度，默认1200像素
        timeout (int): 页面加载超时时间，默认10秒
        viewport_height (int): 视口高度，默认1像素
        template_path (Path): HTML模板文件路径
        jinja_env (Optional[Environment]): Jinja2环境对象
        _template_cache (Dict): 模板缓存字典
        _cache_lock (Lock): 缓存锁，确保线程安全
        
    Example:
        >>> generator = ImageGenerator(config)
        >>> await generator.initialize()
        >>> image_path = await generator.generate_rank_image(users, group_info, "排行榜")
    """
    
    def __init__(self, config: PluginConfig, context=None):
        """初始化图片生成器
        
        Args:
            config (PluginConfig): 插件配置对象，包含生成参数和设置
            context: AstrBot上下文对象，用于API调用获取头像
        """
        self.config = config
        self.context = context
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.logger = astrbot_logger
        
        # 图片生成配置
        self.width = IMAGE_WIDTH
        self.timeout = BROWSER_TIMEOUT
        self.viewport_height = VIEWPORT_HEIGHT
        
        # 模板路径 - 根据主题选择
        self._templates_dir = Path(__file__).parent.parent / "templates"
        self._update_template_path()
        
        # 模板缓存机制
        self._template_cache: Dict[str, Any] = {}
        self._cache_lock = asyncio.Lock()
        self._cache_hits = 0
        self._cache_misses = 0
        
        # 并发控制：浏览器的生命周期管理
        self._browser_lock = asyncio.Lock()
        self._active_tasks = 0
        
        # Jinja2环境将在initialize方法中初始化
        self.jinja_env = None
    
    def _update_template_path(self):
        """根据主题配置更新模板路径（支持自动根据时间切换主题）"""
        theme = getattr(self.config, 'theme', 'default')
        auto_switch = getattr(self.config, 'auto_theme_switch', False)
        
        if auto_switch:
            # 自动根据时间切换主题
            theme = self._get_auto_theme(theme)
            self.logger.info(f"自动主题切换已启用，当前时间匹配主题: {theme}")
        
        template_map = {
            'default': 'rank_template.html',
            'liquid_glass': 'rank_template_liquid_glass.html',
            'liquid_glass_dark': 'rank_template_liquid_glass_dark.html',
        }
        template_file = template_map.get(theme, 'rank_template.html')
        self.template_path = self._templates_dir / template_file
        self.logger.info(f"使用排行榜主题: {theme}, 模板: {template_file}")
    
    def _get_auto_theme(self, base_theme: str) -> str:
        """根据当前时间自动选择合适的主题
        
        根据 auto_theme_switch 配置中的 light/dark 切换时间，
        判断当前应该使用浅色主题还是深色主题。
        浅色时段使用用户配置的主题（如 liquid_glass），
        深色时段固定使用 liquid_glass_dark。
        
        Args:
            base_theme: 用户配置的浅色主题名称
            
        Returns:
            str: 主题名称，浅色时段返回 base_theme，深色时段返回 'liquid_glass_dark'
        """
        try:
            switch_times = getattr(self.config, 'theme_switch_times', {"light": "06:00", "dark": "18:00"})
            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute
            
            # 解析浅色主题开始时间
            light_time_str = switch_times.get("light", "06:00")
            light_h, light_m = map(int, light_time_str.split(':'))
            light_minutes = light_h * 60 + light_m
            
            # 解析深色主题开始时间
            dark_time_str = switch_times.get("dark", "18:00")
            dark_h, dark_m = map(int, dark_time_str.split(':'))
            dark_minutes = dark_h * 60 + dark_m
            
            # 判断当前时间段
            if light_minutes <= current_minutes < dark_minutes:
                # 浅色时间段：使用用户配置的浅色主题
                return base_theme
            else:
                # 深色时间段：使用液态玻璃深色主题
                return 'liquid_glass_dark'
        except (ValueError, AttributeError, KeyError, TypeError) as e:
            self.logger.warning(f"自动主题切换时间解析失败，使用默认主题: {e}")
            return base_theme
    

    
    async def _init_jinja2_env(self):
        """初始化Jinja2环境
        
        创建Jinja2模板环境，启用自动转义以防止XSS攻击。
        如果Jinja2不可用，将使用不安全的字符串拼接方式作为备用。
        添加模板缓存机制以提高性能。
        
        Returns:
            None: 无返回值，初始化结果通过日志输出
            
        Example:
            >>> await self._init_jinja2_env()
            # 将初始化Jinja2环境或记录警告信息
        """
        if JINJA2_AVAILABLE:
            try:
                # 创建Jinja2环境，启用自动转义和缓存，但不启用异步
                self.jinja_env = Environment(
                    autoescape=select_autoescape(['html', 'xml']),
                    trim_blocks=True,
                    lstrip_blocks=True,
                    cache_size=400  # 启用模板缓存，但不启用异步
                )
                
                # 预加载模板文件
                await self._preload_templates()
                
                self.logger.info("Jinja2环境初始化成功，模板缓存已启用")
            except Exception as e:
                self.logger.error(f"Jinja2环境初始化失败: {e}")
                self.jinja_env = None
        else:
            self.jinja_env = None
            self.logger.warning("Jinja2不可用，将使用不安全的字符串拼接")
    
    async def _preload_templates(self):
        """预加载模板文件到缓存"""
        try:
            if await aiofiles.os.path.exists(self.template_path):
                # 使用异步文件读取优化
                async with aiofiles.open(self.template_path, 'r', encoding='utf-8') as f:
                    template_content = await f.read()
                
                # 缓存模板内容
                template_hash = self._get_template_hash(template_content)
                async with self._cache_lock:
                    self._template_cache['main_template'] = {
                        'content': template_content,
                        'hash': template_hash,
                        'template': self.jinja_env.from_string(template_content) if self.jinja_env else None
                    }
                
                self.logger.info(f"模板预加载完成，缓存键: main_template")
            else:
                self.logger.warning(f"模板文件不存在: {self.template_path}")
        except Exception as e:
            self.logger.error(f"模板预加载失败: {e}")
    
    def _get_template_hash(self, content: str) -> str:
        """获取模板内容的哈希值，用于缓存验证"""
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    async def _get_cached_template(self) -> Optional[Union[str, Template]]:
        """获取缓存的模板"""
        async with self._cache_lock:
            cached = self._template_cache.get('main_template')
            if cached:
                self._cache_hits += 1
                return cached.get('template') if self.jinja_env else cached.get('content')
            else:
                self._cache_misses += 1
                return None
    
    async def _update_template_cache(self, content: str):
        """更新模板缓存"""
        try:
            template_hash = self._get_template_hash(content)
            async with self._cache_lock:
                self._template_cache['main_template'] = {
                    'content': content,
                    'hash': template_hash,
                    'template': self.jinja_env.from_string(content) if self.jinja_env else None
                }

        except Exception as e:
            self.logger.error(f"更新模板缓存失败: {e}")
    
    async def get_cache_stats(self) -> Dict[str, int]:
        """获取缓存统计信息"""
        async with self._cache_lock:
            return {
                'hits': self._cache_hits,
                'misses': self._cache_misses,
                'total_requests': self._cache_hits + self._cache_misses,
                'hit_rate': self._cache_hits / max(1, self._cache_hits + self._cache_misses)
            }
    
    @safe_generation(default_return=None)
    async def initialize(self):
        """初始化图片生成器（轻量初始化）
        
        只初始化Jinja2模板环境，不启动浏览器。
        浏览器将在首次生成图片时按需启动（懒加载）。
        
        Raises:
            ImageGenerationError: 当Playwright未安装时抛出
            
        Returns:
            None: 无返回值
        """
        if not PLAYWRIGHT_AVAILABLE:
            self.logger.error("Playwright未安装，图片生成功能将不可用")
            raise ImageGenerationError("Playwright未安装，无法生成图片")
        
        try:
            self.logger.info("开始初始化图片生成器（轻量模式）...")
            
            # 只初始化Jinja2环境，浏览器按需启动
            await self._init_jinja2_env()
            
            self.logger.info("图片生成器初始化完成（浏览器未启动，将在首次生成图片时按需启动）")
        except FileNotFoundError as e:
            self.logger.error(f"模板文件未找到: {e}")
            raise ImageGenerationError(f"模板文件未找到: {e}")
        except PermissionError as e:
            self.logger.error(f"权限错误: {e}")
            raise ImageGenerationError(f"权限不足: {e}")
        except OSError as e:
            self.logger.error(f"初始化图片生成器失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"初始化失败: {e}")
    
    async def _ensure_browser(self):
        """确保浏览器已启动（懒加载）并增加任务计数
        
        使用异步锁防止并发启动。如果是第一个任务则启动浏览器，
        然后增加活跃任务计数器。
        """
        async with self._browser_lock:
            self._active_tasks += 1
            if self.browser:
                return
            
            self.logger.info("按需启动浏览器...")
            try:
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-extensions"
                    ]
                )
                self.logger.info("Chromium浏览器启动成功")
            except Exception as e:
                self._active_tasks -= 1
                self.logger.error(f"启动浏览器失败: {e}")
                raise ImageGenerationError(f"启动浏览器失败: {e}")
    
    async def _close_browser(self):
        """任务完成，减少任务计数，如果计数为0则关闭浏览器释放内存"""
        async with self._browser_lock:
            self._active_tasks = max(0, self._active_tasks - 1)
            
            if self._active_tasks > 0:
                # 还有其他任务在使用浏览器，不关闭
                return
                
            try:
                # 不再在此处关闭self.page，因为页面已变为局部变量，由各自的任务自行关闭
                if self.browser:
                    await self.browser.close()
                    self.browser = None
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
                self.logger.info("所有渲染任务完成，浏览器已关闭并释放内存")
            except Exception as e:
                self.logger.warning(f"关闭浏览器时发生错误: {e}")
            finally:
                self.browser = None
                self.playwright = None
    
    async def cleanup(self):
        """清理资源
        
        异步清理图片生成器的所有资源，包括浏览器实例、页面和Playwright对象。
        确保资源正确释放，避免内存泄漏。
        
        Raises:
            Exception: 当清理过程中发生错误时抛出
            
        Returns:
            None: 无返回值，清理完成后所有资源将被释放
            
        Example:
            >>> await generator.cleanup()
            >>> print(generator.browser is None)
            True
        """
        try:
            if self.page:
                await self.page.close()
                self.page = None
            
            if self.browser:
                await self.browser.close()
                self.browser = None
            
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            
            self.logger.info("图片生成器资源已清理")
        
        except ConnectionError as e:
            self.logger.error(f"浏览器连接错误: {e}")
        except Exception as e:
            self.logger.error(f"清理图片生成器资源失败: {e}")
    
    @safe_generation(default_return=None)
    async def generate_rank_image(self, 
                                 users: List[UserData], 
                                 group_info: GroupInfo, 
                                 title: str,
                                 current_user_id: Optional[str] = None,
                                 llm_token_usage: Dict[str, int] = None,
                                 titles_map: Optional[Dict[str, str]] = None) -> str:
        """生成排行榜图片（懒加载浏览器，用完即关）
        
        Args:
            users: 用户数据列表（已按发言数降序排列）
            group_info: 群组信息
            title: 排行榜标题
            current_user_id: 当前用户ID，用于高亮显示
            llm_token_usage: LLM token使用统计
            titles_map: 用户ID到头衔的映射字典 {user_id: title}
            
        Returns:
            str: 生成的临时图片路径
            
        Raises:
            ImageGenerationError: 图片生成失败时抛出
        """
        # 每次生成图片时重新检查主题（支持自动主题切换实时生效）
        self._update_template_path()
        
        # 按需启动浏览器
        await self._ensure_browser()
        
        temp_path = None
        page = None
        success = False
        
        try:
            # 创建局部页面变量，防止并发时互相覆盖（开启两倍高清渲染）
            page = await self.browser.new_page(device_scale_factor=2)
            
            # 设置视口
            await page.set_viewport_size({"width": self.width, "height": self.viewport_height})
            
            # 生成HTML内容（显式传入头衔映射）
            html_content = await self._generate_html(users, group_info, title, current_user_id, llm_token_usage, titles_map)
            
            # 设置页面内容（使用 load 而非 networkidle，避免外部资源加载超时）
            await page.set_content(html_content, wait_until="load")
            
            # 等待页面加载完成
            await page.wait_for_timeout(2000)
            
            # 动态调整页面高度
            body_height = await page.evaluate("document.body.scrollHeight")
            await page.set_viewport_size({"width": self.width, "height": body_height})
            
            # 生成临时文件路径（异步方式）
            temp_filename = f"rank_image_{uuid.uuid4().hex}.png"
            temp_path = Path(tempfile.gettempdir()) / temp_filename
            
            # 截图
            await page.screenshot(path=temp_path, full_page=True)
            
            success = True
            return str(temp_path)

        
        except FileNotFoundError as e:
            self.logger.error(f"临时文件或资源未找到: {e}")
            raise ImageGenerationError(f"文件资源未找到: {e}")
        except PermissionError as e:
            self.logger.error(f"权限错误: {e}")
            raise ImageGenerationError(f"权限不足: {e}")
        except TimeoutError as e:
            self.logger.error(f"浏览器操作超时: {e}")
            raise ImageGenerationError(f"操作超时: {e}")
        except RuntimeError as e:
            # 捕获浏览器运行时错误，如页面渲染失败、JavaScript执行错误等
            self.logger.error(f"生成排行榜图片失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"生成图片失败: {e}")
        
        finally:
            # 清理资源
            if page:
                try:
                    await page.close()
                except Exception as e:
                    self.logger.warning(f"关闭页面时发生错误: {e}")
            
            # 生成完毕后关闭浏览器释放内存
            await self._close_browser()
            
            # 清理临时文件：如果生成失败，删除已创建的临时文件避免积累
            if not success and temp_path and temp_path.exists():
                try:
                    await aiofiles.os.unlink(str(temp_path))
                    self.logger.debug(f"已清理失败的临时文件: {temp_path}")
                except Exception as e:
                    self.logger.warning(f"清理临时文件失败: {e}")
    
    @safe_generation(default_return=None)
    async def generate_milestone_image(self,
                                       user_id: str,
                                       nickname: str,
                                       milestone_count: int,
                                       rank: int,
                                       daily_count: int,
                                       active_days: int,
                                       last_date: str,
                                       group_total_messages: int,
                                       percentage: float,
                                       group_info: GroupInfo) -> str:
        """生成里程碑个人成就卡片图片
        
        生成一张精美的个人成就卡片，替代里程碑触发时发送整个排行榜。
        
        Args:
            user_id: 用户ID
            nickname: 用户昵称
            milestone_count: 里程碑发言次数
            rank: 群内排名
            daily_count: 今日发言数
            active_days: 活跃天数
            last_date: 最后发言日期
            group_total_messages: 群总发言数
            percentage: 发言占比
            group_info: 群组信息
            
        Returns:
            str: 生成的图片路径，失败时返回None
        """
        # 按需启动浏览器
        await self._ensure_browser()
        
        page = None
        temp_path = None
        success = False
        try:
            # 创建局部页面变量（里程碑卡片使用较窄的视口，开启两倍高清渲染）
            page = await self.browser.new_page(device_scale_factor=2)
            milestone_width = 600
            await page.set_viewport_size({"width": milestone_width, "height": self.viewport_height})
            
            # 加载里程碑模板
            milestone_template_path = self._templates_dir / "milestone_template.html"
            template_content = ""
            if await aiofiles.os.path.exists(milestone_template_path):
                async with aiofiles.open(milestone_template_path, 'r', encoding='utf-8') as f:
                    template_content = await f.read()
            else:
                self.logger.warning(f"里程碑模板文件不存在: {milestone_template_path}")
                return None
            
            # 获取语言设置
            lang = getattr(self.config, 'image_language', 'zh-CN')
            ms_texts = {
                'zh-CN': {'title': '发言里程碑', 'brand': '群聊成就', 'hint1': 'Total number', 'hint2': 'of messages', 'milestone': '总发言破', 'rank': '群内排名', 'defeated': '击败了群内', 'defeated2': '的活跃群友'},
                'en-US': {'title': 'Message Milestone', 'brand': 'Group Achievement', 'hint1': 'Total number', 'hint2': 'of messages', 'milestone': 'Messages:', 'rank': 'Group rank', 'defeated': 'Beat', 'defeated2': 'of active members'},
                'ru-RU': {'title': 'Веха сообщений', 'brand': 'Достижение', 'hint1': 'Всего', 'hint2': 'сообщений', 'milestone': 'Сообщений', 'rank': 'Ранг', 'defeated': 'Опередил', 'defeated2': 'активных участников'}
            }
            ms = ms_texts.get(lang, ms_texts['zh-CN'])
            
            avatar_url = self._get_avatar_url(user_id)
            group_name = group_info.group_name or f"群{group_info.group_id}"
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            template_data = {
                'avatar_url': avatar_url,
                'nickname': self._escape_html_safe(nickname),
                'user_id': self._escape_html_safe(str(user_id)),
                'group_name': self._escape_html_safe(f"{group_name}[{group_info.group_id}]"),
                'milestone_count': milestone_count,
                'rank': rank,
                'daily_count': daily_count,
                'active_days': active_days,
                'last_date': self._escape_html_safe(last_date or "未知"),
                'group_total_messages': group_total_messages,
                'percentage': f"{percentage:.2f}",
                'current_time': current_time,
                'title_text': self._escape_html_safe(ms['title']),
                'milestone_brand': self._escape_html_safe(ms['brand']),
                'ms_hint_line1': self._escape_html_safe(ms['hint1']),
                'ms_hint_line2': self._escape_html_safe(ms['hint2']),
                'ms_milestone_label': self._escape_html_safe(ms['milestone']),
                'ms_rank_prefix': self._escape_html_safe(ms['rank']),
                'ms_defeated_prefix': self._escape_html_safe(ms['defeated']),
                'ms_defeated_suffix': self._escape_html_safe(ms['defeated2'])
            }
            
            # 渲染模板
            if JINJA2_AVAILABLE and self.jinja_env:
                template = self.jinja_env.from_string(template_content)
                html_content = template.render(**template_data)
            else:
                # 回退：简单占位符替换
                html_content = template_content
                for key, value in template_data.items():
                    html_content = html_content.replace('{{ ' + key + ' }}', str(value))
                    html_content = html_content.replace('{{' + key + '}}', str(value))
            
            # 设置页面内容
            await page.set_content(html_content, wait_until="load")
            await page.wait_for_timeout(2000)
            
            # 动态调整页面高度
            body_height = await page.evaluate("document.body.scrollHeight")
            await page.set_viewport_size({"width": milestone_width, "height": body_height})
            
            # 生成临时文件
            temp_filename = f"milestone_{uuid.uuid4().hex}.png"
            temp_path = Path(tempfile.gettempdir()) / temp_filename
            
            # 截图
            await page.screenshot(path=temp_path, full_page=True)
            
            success = True
            return str(temp_path)
        
        except FileNotFoundError as e:
            self.logger.error(f"里程碑模板文件未找到: {e}")
            raise ImageGenerationError(f"文件资源未找到: {e}")
        except PermissionError as e:
            self.logger.error(f"权限错误: {e}")
            raise ImageGenerationError(f"权限不足: {e}")
        except TimeoutError as e:
            self.logger.error(f"浏览器操作超时: {e}")
            raise ImageGenerationError(f"操作超时: {e}")
        except RuntimeError as e:
            self.logger.error(f"生成里程碑卡片失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"生成图片失败: {e}")
        
        finally:
            if page:
                try:
                    await page.close()
                except Exception as e:
                    self.logger.warning(f"关闭页面时发生错误: {e}")
            
            # 生成完毕后关闭浏览器释放内存
            await self._close_browser()
            
            # 清理临时文件：如果生成失败，删除已创建的临时文件避免积累
            if not success and temp_path and temp_path.exists():
                try:
                    await aiofiles.os.unlink(str(temp_path))
                    self.logger.debug(f"已清理失败的里程碑临时文件: {temp_path}")
                except Exception as e:
                    self.logger.warning(f"清理里程碑临时文件失败: {e}")
    
    @safe_generation(default_return="")
    async def _generate_html(self, 
                      users: List[UserData], 
                      group_info: GroupInfo, 
                      title: str,
                      current_user_id: Optional[str] = None,
                      llm_token_usage: Dict[str, int] = None,
                      titles_map: Optional[Dict[str, str]] = None) -> str:
        """生成HTML内容
        
        Args:
            users: 用户数据列表
            group_info: 群组信息
            title: 排行榜标题
            current_user_id: 当前用户ID，用于高亮显示
            llm_token_usage: LLM token使用统计
            titles_map: 用户ID到头衔的映射字典 {user_id: title}
        """
        if not users:
            return await self._generate_empty_html(group_info, title)
        
        # 使用批量处理优化性能（显式传入头衔映射）
        processed_data = self._process_user_data_batch(users, current_user_id, titles_map)
        
        # 计算统计数据
        total_messages = processed_data['total_messages']
        
        # 生成完整HTML
        html_template = await self._load_html_template()
        
        # 获取当前时间
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 准备模板数据（使用字典构建优化）
        llm_token_text = ""
        if llm_token_usage and llm_token_usage.get("total_tokens", 0) > 0:
            lang_llm = {
                'zh-CN': f"LLM Token 消耗: {llm_token_usage.get('total_tokens', 0)} (输入{llm_token_usage.get('prompt_tokens', 0)}+输出{llm_token_usage.get('completion_tokens', 0)})",
                'en-US': f"LLM Token usage: {llm_token_usage.get('total_tokens', 0)} (in:{llm_token_usage.get('prompt_tokens', 0)}+out:{llm_token_usage.get('completion_tokens', 0)})",
                'ru-RU': f"LLM токенов: {llm_token_usage.get('total_tokens', 0)} (вх:{llm_token_usage.get('prompt_tokens', 0)}+вых:{llm_token_usage.get('completion_tokens', 0)})"
            }
            llm_token_text = lang_llm.get(lang, lang_llm['zh-CN'])
        
        # 获取图片语言设置
        lang = getattr(self.config, 'image_language', 'zh-CN')
        lang_texts = {
            'zh-CN': {'total': '总发言数', 'messages': '条', 'generated_by': '🤖 由 AstrBot 发言统计插件生成', 'generate_time': '生成时间', 'latest_msg': '最近发言', 'count_suffix': '次'},
            'en-US': {'total': 'Total', 'messages': 'msgs', 'generated_by': '🤖 Generated by AstrBot Message Stats', 'generate_time': 'Generated', 'latest_msg': 'Last seen', 'count_suffix': 'times'},
            'ru-RU': {'total': 'Всего', 'messages': 'сообщ.', 'generated_by': '🤖 Сгенерировано AstrBot', 'generate_time': 'Сгенерировано', 'latest_msg': 'Последнее', 'count_suffix': 'раз'}
        }
        texts = lang_texts.get(lang, lang_texts['zh-CN'])
        langs_sep = {
            'zh-CN': '▼ 其他用户 ▼',
            'en-US': '▼ Other Users ▼',
            'ru-RU': '▼ Другие ▼'
        }
        
        template_data = {
            'group_name': self._escape_html_safe(group_info.group_name or f"群{group_info.group_id}"),
            'group_id': self._escape_html_safe(str(group_info.group_id)),
            'title': self._escape_html_safe(title),
            'total_messages': self._escape_html_safe(str(total_messages)),
            'user_count': self._escape_html_safe(str(len(users))),
            'current_time': self._escape_html_safe(current_time),
            'llm_token_info': self._escape_html_safe(llm_token_text) if llm_token_text else "",
            'lang': lang,
            'text_total': self._escape_html_safe(texts['total']),
            'text_messages': self._escape_html_safe(texts['messages']),
            'text_generated_by': self._escape_html_safe(texts['generated_by']),
            'text_generate_time': self._escape_html_safe(texts['generate_time']),
            'text_latest_msg': self._escape_html_safe(texts['latest_msg']),
            'text_count_suffix': self._escape_html_safe(texts['count_suffix']),
            'text_separator': self._escape_html_safe(langs_sep.get(lang, langs_sep['zh-CN']))
        }
        
        # 生成HTML内容（优化渲染逻辑）
        return await self._render_html_template(html_template, template_data, processed_data['user_items'])
    
    def _process_user_data_batch(self, users: List[UserData], current_user_id: Optional[str], 
                                  titles_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """批量处理用户数据，优化性能
        
        Args:
            users: 用户数据列表
            current_user_id: 当前用户ID，用于高亮显示
            titles_map: 用户ID到头衔的映射字典 {user_id: title}
                      优先使用此参数中的头衔，若无则回退到 user.display_title
        """
        if not users:
            return {'total_messages': 0, 'user_items': []}
        
        # 预计算统计数据 - 使用时间段内的发言数
        total_messages = sum(user.display_total if user.display_total is not None else user.message_count for user in users)
        
        # 批量生成用户项目
        user_items = []
        current_user_found = False
        
        for i, user in enumerate(users):
            is_current_user = current_user_id and user.user_id == current_user_id
            if is_current_user:
                current_user_found = True
            
            # 使用时间段内的发言数
            user_messages = user.display_total if user.display_total is not None else user.message_count
            
            # 获取 LLM 头衔：优先使用 titles_map，其次 user.display_title
            user_title = None
            user_title_color = None
            if titles_map and user.user_id in titles_map:
                raw = titles_map[user.user_id]
                if isinstance(raw, dict):
                    user_title = raw.get("title")
                    user_title_color = raw.get("color")
                else:
                    user_title = raw
            elif user.display_title:
                user_title = user.display_title
                user_title_color = user.display_title_color

            
            if user_title:
                self.logger.info(f"头衔数据: {user.nickname} -> 「{user_title}」")
            
            avatar_url = self._get_avatar_url(user.user_id)
            user_items.append({
                'rank': i + 1,
                'nickname': user.nickname,
                'title': user_title,
                'title_color': user_title_color,
                'avatar_url': avatar_url,
                'avatar_color': self._get_avatar_color(user.nickname),
                'avatar_char': self._get_avatar_char(user.nickname),
                'total': user_messages,
                'percentage': (user_messages / total_messages * 100) if total_messages > 0 else 0,
                'last_date': user.last_date or "未知",
                'is_current_user': is_current_user,
                'is_separator': False
            })
        
        # 如果当前用户不在排行榜中，添加到末尾
        if current_user_id and not current_user_found:
            current_user_data = next((user for user in users if user.user_id == current_user_id), None)
            if current_user_data:
                current_user_messages = current_user_data.display_total if current_user_data.display_total is not None else current_user_data.message_count
                current_rank = sum(1 for user in users if (user.display_total if user.display_total is not None else user.message_count) > current_user_messages) + 1
                user_items.append({
                    'rank': current_rank,
                    'nickname': current_user_data.nickname,
                    'avatar_url': self._get_avatar_url(current_user_data.user_id),
                    'total': current_user_messages,
                    'percentage': (current_user_messages / total_messages * 100) if total_messages > 0 else 0,
                    'last_date': current_user_data.last_date or "未知",
                    'is_current_user': True,
                    'is_separator': True
                })
        
        return {
            'total_messages': total_messages,
            'user_items': user_items
        }

    
    async def _render_html_template(self, template_content: str, template_data: Dict[str, Any], user_items: List[Dict[str, Any]]) -> str:
        """优化的HTML模板渲染方法"""
        try:
            if JINJA2_AVAILABLE and self.jinja_env:
                # 使用缓存的模板
                cached_template = await self._get_cached_template()
                if cached_template and isinstance(cached_template, Template):
                    template_data['user_items'] = user_items
                    return cached_template.render(**template_data)
                else:
                    # 动态创建模板
                    template = self.jinja_env.from_string(template_content)
                    template_data['user_items'] = user_items
                    return template.render(**template_data)
            else:
                # Jinja2不可用时，使用纯占位符回退模板
                return await self._render_fallback(template_data, user_items)
        except (ValueError, TypeError, KeyError, PermissionError, UnicodeDecodeError) as e:
            self.logger.error(f"HTML模板渲染失败({type(e).__name__}): {e}")
            return await self._render_fallback(template_data, user_items)
    
    async def _render_fallback(self, template_data: Dict[str, Any], user_items: List[Dict[str, Any]]) -> str:
        """统一的回退渲染方法"""
        fallback_template = await self._get_fallback_template()
        return self._render_fallback_template(fallback_template, template_data, user_items)
    
    def _render_fallback_template(self, template_content: str, template_data: Dict[str, Any], user_items: List[Dict[str, Any]]) -> str:
        """回退模板渲染方法（安全版本）
        
        当Jinja2不可用时的安全回退方案。
        使用简单的字符串替换而不是format()，避免Jinja2语法冲突。
        """
        # 使用生成器表达式优化内存使用
        user_items_html = ''.join(self._generate_user_item_html_safe(item) for item in user_items)
        
        # 安全替换：避免Jinja2语法冲突
        safe_content = template_content
        for key, value in template_data.items():
            if isinstance(value, str):
                # 对字符串值进行HTML转义
                safe_value = self._escape_html_safe(value)
                safe_content = safe_content.replace('{{' + key + '}}', safe_value)
            else:
                # 对于非字符串值，直接替换
                safe_content = safe_content.replace('{{' + key + '}}', str(value))
        
        # 替换user_items
        safe_content = safe_content.replace('{{user_items}}', user_items_html)
        
        return safe_content
    
    # 最简单的空数据回退HTML常量
    def _get_fallback_lang_texts(self):
        """获取回退文案（用于_fallback等没有lang上下文的地方）"""
        lang = getattr(self.config, 'image_language', 'zh-CN')
        texts = {
            'zh-CN': {'rank_title': '发言排行榜', 'no_data': '暂无发言数据', 'waiting': '期待大家的活跃发言！', 'empty_data': '暂无数据', 'latest_msg': '最近发言', 'count_unit': '次', 'unknown_user': '未知用户', 'unknown': '未知'},
            'en-US': {'rank_title': 'Message Leaderboard', 'no_data': 'No message data yet', 'waiting': 'Looking forward to active chatting!', 'empty_data': 'No data', 'latest_msg': 'Last seen', 'count_unit': 'times', 'unknown_user': 'Unknown User', 'unknown': 'Unknown'},
            'ru-RU': {'rank_title': 'Таблица лидеров', 'no_data': 'Нет данных о сообщениях', 'waiting': 'Ждём активных сообщений!', 'empty_data': 'Нет данных', 'latest_msg': 'Последнее', 'count_unit': 'раз', 'unknown_user': 'Неизвестный', 'unknown': 'Неизвестно'}
        }
        return texts.get(lang, texts['zh-CN'])

    @property
    def _fallback_texts(self):
        return self._get_fallback_lang_texts()

    _EMPTY_FALLBACK_HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
</head>
<body>
    <h1>{title}</h1>
    <p>{no_data}</p>
</body>
</html>"""

    async def _generate_empty_html(self, group_info: GroupInfo, title: str) -> str:
        """生成空数据HTML（优化版本）"""
        # 尝试从缓存获取空数据模板
        empty_template_cache_key = 'empty_template'
        async with self._cache_lock:
            cached_empty = self._template_cache.get(empty_template_cache_key)
        
        if cached_empty:
            template_content = cached_empty['content']
            template_obj = cached_empty.get('template')
        else:
            # 创建空数据模板
            template_content = await self._get_empty_template()
            async with self._cache_lock:
                self._template_cache[empty_template_cache_key] = {
                    'content': template_content,
                    'template': self.jinja_env.from_string(template_content) if self.jinja_env else None
                }
            template_obj = self._template_cache[empty_template_cache_key].get('template')
        
        # 准备模板数据
        template_data = {
            'group_name': self._escape_html_safe(group_info.group_name or f"群{group_info.group_id}"),
            'group_id': self._escape_html_safe(str(group_info.group_id)),
            'title': self._escape_html_safe(title)
        }
        
        try:
            if JINJA2_AVAILABLE and self.jinja_env and template_obj:
                return template_obj.render(**template_data)
            else:
                # 使用安全的字符串替换而不是format()
                safe_content = template_content
                for key, value in template_data.items():
                    if isinstance(value, str):
                        safe_value = self._escape_html_safe(value)
                        safe_content = safe_content.replace('{{' + key + '}}', safe_value)
                    else:
                        safe_content = safe_content.replace('{{' + key + '}}', str(value))
                return safe_content
        except (ValueError, TypeError, KeyError, PermissionError, UnicodeDecodeError) as e:
            self.logger.error(f"空数据HTML模板渲染失败({type(e).__name__}): {e}")
            return self._EMPTY_FALLBACK_HTML
    
    async def _get_empty_template(self) -> str:
        """获取空数据模板（简化版本）"""
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{{ title }}</title>
    <style>
        body {
            font-family: 'Microsoft YaHei', sans-serif;
            background: linear-gradient(135deg, #E9EFF6 0%, #D6E4F0 100%);
            margin: 0;
            padding: 40px;
            text-align: center;
        }
        .container {
            background: rgba(255,255,255,0.95);
            border-radius: 20px;
            padding: 60px;
            max-width: 600px;
            margin: 0 auto;
        }
        .title {
            font-size: 32px;
            color: #1F2937;
            margin-bottom: 20px;
        }
        .subtitle {
            font-size: 24px;
            color: #6B7280;
            margin-bottom: 40px;
        }
        .empty-text {
            font-size: 18px;
            color: #9CA3AF;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="title">{{ group_name }}[{{ group_id }}]</div>
        <div class="subtitle">{{ title }}</div>
        <div class="empty-text">
            暂无发言数据
            <br>
            期待大家的活跃发言！
        </div>
    </div>
</body>
</html>"""
    
    def _generate_user_item_html_safe(self, item_data: Dict[str, Any]) -> str:
        """生成安全的用户条目HTML（使用Jinja2模板）"""
        # 使用元组和字典预构建减少字符串操作
        css_classes = self._get_css_classes(item_data)
        styles = self._get_item_styles(item_data)
        safe_content = self._get_safe_content(item_data)
        
        # 准备模板数据
        template_data = {
            'rank': item_data['rank'],
            'total': item_data['total'],
            'percentage': item_data['percentage'],
            'css_classes': css_classes,
            'styles': styles,
            'safe_content': safe_content
        }
        
        # 使用Jinja2模板渲染，确保所有动态内容都经过转义
        if JINJA2_AVAILABLE:
            try:
                if not hasattr(self, '_user_item_macro_template'):
                    self._user_item_macro_template = self._load_user_item_macro_template()
                
                if self._user_item_macro_template:
                    return self._user_item_macro_template.render(item_data=template_data)
            except Exception as e:
                self.logger.warning(f"Jinja2模板渲染失败，使用备用方案: {e}")
        
        # 备用方案：使用更安全的字符串拼接方式
        # 对所有动态内容进行HTML转义
        safe_nickname = html.escape(safe_content['nickname'])
        safe_avatar_url = html.escape(safe_content['avatar_url'])
        safe_last_date = html.escape(safe_content['last_date'])
        safe_separator_style = html.escape(styles['separator'])
        safe_rank_color = html.escape(styles['rank_color'])
        safe_avatar_border = html.escape(styles['avatar_border'])
        
        # 使用字符串拼接而不是f-string，提高安全性
        # 根据当前用户状态选择合适的排名样式类
        rank_class = "rank-current" if item_data['is_current_user'] else "rank"
        
        # 获取头衔和颜色
        user_title_raw = item_data.get('title', None) or item_data.get('safe_content', {}).get('title', None)
        user_title_color_raw = item_data.get('title_color', None) or item_data.get('safe_content', {}).get('title_color', None)
        user_title_html = ""
        if user_title_raw:
            safe_title = html.escape(str(user_title_raw))
            safe_title_color = html.escape(str(user_title_color_raw)) if user_title_color_raw else '#7C3AED'
            user_title_html = f'<div class="user-title" style="color:{safe_title_color};background:{safe_title_color}22;font-size:13px;font-weight:700;padding:0px 8px;border-radius:10px;display:inline-block;margin-left:8px;vertical-align:middle;line-height:24px;">「{safe_title}」</div>'
        
        html_parts = [
            f'<div class="{css_classes["item"]}" style="{safe_separator_style}">',
            f'    <div class="{rank_class}">#{item_data["rank"]}</div>',
            f'    <img class="avatar" src="{safe_avatar_url}" style="border-color: {safe_avatar_border};" />',
            '    <div class="info">',
            '        <div class="name-date">',
            f'            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;"><span class="nickname" style="font-size:24px;font-weight:600;color:#1F2937;line-height:1.3;">{safe_nickname}</span>{user_title_html}</div>',
            f'            <div class="date" style="color:#6B7280;font-size:15px;">最近发言: {safe_last_date}</div>',
            '        </div>',
            '        <div class="stats">',
            f'            <div class="count">{item_data["total"]} 次</div>',
            f'            <div class="percentage">({item_data["percentage"]:.2f}%)</div>',
            '        </div>',
            '    </div>',
            '</div>'
        ]
        return '\n'.join(html_parts)
    
    def _get_css_classes(self, item_data: Dict[str, Any]) -> Dict[str, str]:
        """获取CSS类名（优化版本）"""
        return {
            'item': "user-item-current" if item_data['is_current_user'] else "user-item"
        }
    
    def _get_item_styles(self, item_data: Dict[str, Any]) -> Dict[str, str]:
        """获取样式信息（优化版本）"""
        return {
            'separator': "margin-top: 20px; border-top: 2px dashed #bdc3c7;" if item_data['is_separator'] else "margin-top: 10px;",
            'rank_color': "#EF4444" if item_data['is_current_user'] else "#3B82F6",
            'avatar_border': "#ffffff"
        }
    
    def _get_safe_content(self, item_data: Dict[str, Any]) -> Dict[str, str]:
        """获取安全的内容（优化版本）"""
        # 批量转义提高性能
        safe_nickname = self._escape_html_safe(str(item_data.get('nickname', '未知用户')))
        safe_last_date = self._escape_html_safe(str(item_data.get('last_date', '未知')))
        safe_avatar_url = self._validate_url_safe(str(item_data.get('avatar_url', '')))
        
        # 处理头衔转义
        title = item_data.get('title', None)
        safe_title = self._escape_html_safe(str(title)) if title else None
        
        # 如果头像URL无效，使用默认头像
        if not safe_avatar_url:
            safe_avatar_url = self._get_avatar_url(str(item_data.get('user_id', '0')))
        
        content = {
            'nickname': safe_nickname,
            'last_date': safe_last_date,
            'avatar_url': safe_avatar_url
        }
        
        if safe_title:
            content['title'] = safe_title
            
        return content

    def _escape_html_safe(self, text: str) -> str:
        """安全的HTML转义"""
        if not isinstance(text, str):
            text = str(text)
        return html.escape(text, quote=True)
    
    def _validate_url_safe(self, url: str) -> str:
        """验证并清理URL"""
        if not isinstance(url, str):
            url = str(url)
        
        # 基本URL验证
        if not url or not url.startswith(('http://', 'https://')):
            return ""
        
        # 移除潜在的恶意字符
        url = url.replace('<', '').replace('>', '').replace('"', '').replace("'", '')
        return url

    _AVATAR_COLORS = ['#F59E0B','#3B82F6','#8B5CF6','#EC4899','#10B981','#EF4444','#14B8A6','#F97316','#6366F1','#84CC16','#06B6D4','#D946EF','#0EA5E9','#EAB308','#A855F7']

    def _get_avatar_color(self, nickname: str) -> str:
        h = 0
        for c in str(nickname):
            h = (h * 31 + ord(c)) & 0xFFFFFFFF
        return self._AVATAR_COLORS[h % len(self._AVATAR_COLORS)]

    def _get_avatar_char(self, nickname: str) -> str:
        n = str(nickname).strip()
        return n[0] if n else '?'

    async def _get_telegram_avatar_from_api(self, user_id: str) -> str:
        """通过Telegram Bot API获取用户真实头像URL"""
        try:
            if not self.context:
                return ""
            bot = await self.context.get_bot()
            if not bot or not hasattr(bot, 'api'):
                return ""
            # getUserProfilePhotos 需要正数ID
            tg_id = abs(int(user_id))
            photos = await bot.api.call_action("getUserProfilePhotos", user_id=tg_id, limit=1)
            if photos and photos.get("total_count", 0) > 0:
                photo = photos["photos"][0][-1]  # 取最大尺寸
                file_info = await bot.api.call_action("getFile", file_id=photo["file_id"])
                if file_info and "file_path" in file_info:
                    token = getattr(bot, 'token', '')
                    if token:
                        return f"https://api.telegram.org/file/bot{token}/{file_info['file_path']}"
        except Exception as e:
            self.logger.warning(f"获取Telegram头像失败: {e}")
        return ""

    def _get_avatar_url(self, user_id: str, platform: str = "") -> str:
        """获取用户头像URL
        根据用户ID自动判断平台，QQ获取真实头像，Telegram通过API获取。
        
        Args:
            user_id (str): 用户ID
            platform (str): 平台类型（自动检测，保留参数）
            
        Returns:
            str: 头像URL
        """
        # 支持多种平台的头像服务
        avatar_services = {
            "qq": "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
            "telegram": "",  # Telegram通过API获取
            "discord": "https://cdn.discordapp.com/embed/avatars/{avatar_id}.png",
            "default": ""
        }
        
        # 根据用户ID自动判断平台
        try:
            user_id_str = str(user_id).strip()
            if not user_id_str.lstrip('-').isdigit():
                platform = "default"
            else:
                uid_num = int(user_id_str)
                if uid_num < 0:
                    platform = "telegram"
                elif uid_num > 10000000000000000:
                    platform = "discord"
                else:
                    platform = "qq"
                    # QQ号有效性：5-12位数字才是有效QQ
                    if len(user_id_str) < 5 or len(user_id_str) > 12:
                        return ""
        except (ValueError, TypeError):
            platform = "default"
        
        # Telegram通过API获取，同步返回空让模板回退到彩色字母
        if platform == "telegram":
            return ""
        
        service_url = avatar_services.get(platform, avatar_services["default"])
        if not service_url:
            return ""
        
        # 安全计算 avatar_id（Discord用）
        try:
            uid_int = abs(int(user_id))
            avatar_id = uid_int % 5
        except (ValueError, TypeError):
            avatar_id = 0
        
        return service_url.format(user_id=user_id, avatar_id=avatar_id)
    
    @safe_file_operation(default_return="")
    async def _load_html_template(self) -> str:
        """加载HTML模板（简化缓存逻辑）"""
        try:
            # 尝试从缓存获取
            cached_template = await self._get_cached_template()
            if cached_template:
                if isinstance(cached_template, str):
                    return cached_template
                elif hasattr(cached_template, 'source'):
                    # Jinja2模板对象，返回源代码
                    return cached_template.source
                else:
                    return str(cached_template)
            
            # 缓存未命中，从文件加载
            if await aiofiles.os.path.exists(self.template_path):
                async with aiofiles.open(self.template_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                
                # 更新缓存
                await self._update_template_cache(content)
                return content
            else:
                self.logger.warning(f"模板文件不存在: {self.template_path}")
                # 使用默认模板
                default_template = await self._get_default_template()
                await self._update_template_cache(default_template)
                return default_template
        except FileNotFoundError as e:
            self.logger.warning(f"模板文件未找到: {e}")
            default_template = await self._get_default_template()
            await self._update_template_cache(default_template)
            return default_template
        except PermissionError as e:
            self.logger.error(f"模板文件权限错误: {e}")
            default_template = await self._get_default_template()
            await self._update_template_cache(default_template)
            return default_template
        except UnicodeDecodeError as e:
            self.logger.error(f"模板文件编码错误: {e}")
            default_template = await self._get_default_template()
            await self._update_template_cache(default_template)
            return default_template
    
    async def _get_fallback_template(self) -> str:
        """获取纯占位符回退模板（不含Jinja2语法）
        
        当Jinja2不可用时使用的安全模板，只使用简单的{{ key }}占位符，
        不包含任何Jinja2特有的语法（如循环、过滤器等）。
        """
        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #E9EFF6 0%, #D6E4F0 100%);
            padding: 30px;
            min-height: 100vh;
        }
        .title {
            text-align: center;
            font-size: 28px;
            color: #1F2937;
            margin-bottom: 25px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
        }
        .user-list {
            max-width: 800px;
            margin: 0 auto;
            background: rgba(255,255,255,0.9);
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            padding: 20px;
        }
        .user-item {
            display: flex;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid #E5E7EB;
            transition: transform 0.2s ease;
            border-radius: 8px;
            margin-bottom: 8px;
        }
        .user-item:hover {
            transform: translateX(10px);
            background-color: rgba(59, 130, 246, 0.05);
        }
        .user-item-current {
            display: flex;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid #E5E7EB;
            transition: transform 0.2s ease;
            background: linear-gradient(135deg, #F3E8FF 0%, #EDE9FE 100%);
            border-radius: 12px;
            margin-bottom: 8px;
            box-shadow: 0 2px 4px rgba(139, 92, 246, 0.1);
        }
        .user-item-current:hover {
            transform: translateX(10px);
            box-shadow: 0 4px 8px rgba(139, 92, 246, 0.2);
        }
        .rank {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, #3B82F6 0%, #1D4ED8 100%);
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            font-weight: bold;
            margin-right: 20px;
            box-shadow: 0 2px 4px rgba(59, 130, 246, 0.3);
            transition: transform 0.2s ease;
        }
        .rank:hover {
            transform: scale(1.1);
        }
        .rank-current {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, #8B5CF6 0%, #7C3AED 100%);
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            font-weight: bold;
            margin-right: 20px;
            box-shadow: 0 2px 4px rgba(139, 92, 246, 0.3);
            transition: transform 0.2s ease;
        }
        .rank-current:hover {
            transform: scale(1.1);
        }
        .avatar {
            width: 60px;
            height: 60px;
            border-radius: 50%;
            margin-right: 20px;
            border: 3px solid #ffffff;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            transition: transform 0.2s ease;
        }
        .avatar:hover {
            transform: scale(1.05);
        }
        .info {
            flex: 1;
        }
        .name-date {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 8px;
        }
        .nickname {
            font-size: 18px;
            font-weight: bold;
            color: #1F2937;
        }
        .nickname-with-title {
            max-width: 150px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .user-title {
            font-size: 12px;
            color: #7C3AED;
            font-weight: 700;
            background: #EDE9FE;
            padding: 2px 6px;
            border-radius: 10px;
            white-space: nowrap;
            flex-shrink: 0;
            margin-left: 6px;
        }
        .date {
            font-size: 14px;
            color: #6B7280;
        }
        .stats {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .count {
            font-size: 16px;
            font-weight: bold;
            color: #3B82F6;
        }
        .percentage {
            font-size: 14px;
            color: #6B7280;
        }
        .footer {
            text-align: center;
            margin-top: 20px;
            color: #6B7280;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="title">{{ group_name }}[{{ group_id }}]</div>
    <div class="title">{{ title }}</div>
    <div class="user-list">
        {{ user_items }}
    </div>
    <div class="footer">
        <p>🤖 由 AstrBot 发言统计插件生成</p>
        <p>生成时间: {{ current_time }}</p>
        {{ llm_token_info }}
    </div>
</body>
</html>"""

    async def _get_default_template(self) -> str:
        """获取默认HTML模板（优化版本）"""
        # 尝试从缓存获取默认模板
        default_cache_key = 'default_template'
        async with self._cache_lock:
            cached_default = self._template_cache.get(default_cache_key)
        
        if cached_default:
            return cached_default['content']
        
        # 创建优化的默认模板（使用简单占位符）
        default_template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #E9EFF6 0%, #D6E4F0 100%);
            padding: 30px;
            min-height: 100vh;
        }
        .title {
            text-align: center;
            font-size: 28px;
            color: #1F2937;
            margin-bottom: 25px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
        }
        .user-list {
            max-width: 800px;
            margin: 0 auto;
            background: rgba(255,255,255,0.9);
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            padding: 20px;
        }
        .user-item {
            display: flex;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid #E5E7EB;
            transition: transform 0.2s ease;
            border-radius: 8px;
            margin-bottom: 8px;
        }
        .user-item:hover {
            transform: translateX(10px);
            background-color: rgba(59, 130, 246, 0.05);
        }
        .user-item-current {
            display: flex;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid #E5E7EB;
            transition: transform 0.2s ease;
            background: linear-gradient(135deg, #F3E8FF 0%, #EDE9FE 100%);
            border-radius: 12px;
            margin-bottom: 8px;
            box-shadow: 0 2px 4px rgba(139, 92, 246, 0.1);
        }
        .user-item-current:hover {
            transform: translateX(10px);
            box-shadow: 0 4px 8px rgba(139, 92, 246, 0.2);
        }
        .rank {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, #3B82F6 0%, #1D4ED8 100%);
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            font-weight: bold;
            margin-right: 20px;
            box-shadow: 0 2px 4px rgba(59, 130, 246, 0.3);
            transition: transform 0.2s ease;
        }
        .rank:hover {
            transform: scale(1.1);
        }
        .rank-current {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, #EF4444 0%, #DC2626 100%);
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            font-weight: bold;
            margin-right: 20px;
            box-shadow: 0 2px 4px rgba(239, 68, 68, 0.3);
            transition: transform 0.2s ease;
        }
        .rank-current:hover {
            transform: scale(1.1);
        }
        .avatar {
            width: 60px;
            height: 60px;
            border-radius: 50%;
            margin: 0 20px;
            border: 3px solid #3B82F6;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .info {
            flex: 1;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .name-date {
            display: flex;
            flex-direction: column;
        }
        .nickname {
            font-size: 20px;
            color: #1F2937;
            font-weight: 500;
            line-height: 1.2;
        }
        .date {
            color: #6B7280;
            font-size: 14px;
            margin-top: 4px;
        }
        .stats {
            text-align: right;
            font-size: 18px;
            min-width: 120px;
        }
        .count {
            color: #EF4444;
            font-weight: bold;
        }
        .percentage {
            color: #22C55E;
            font-size: 16px;
        }
    </style>
</head>
<body>
    <div class="title">{{ group_name }}[{{ group_id }}]</div>
    <div class="title">{{ title }}</div>
    <div class="user-list">
        {{ user_items }}
    </div>
</body>
</html>
"""
        
        # 缓存默认模板
        async with self._cache_lock:
            self._template_cache[default_cache_key] = {
                'content': default_template,
                'template': self.jinja_env.from_string(default_template) if self.jinja_env else None
            }
        
        return default_template
    
    async def test_browser_connection(self) -> bool:
        """测试浏览器连接（懒加载，用完即关）"""
        try:
            await self._ensure_browser()
            
            # 创建一个测试页面
            test_page = await self.browser.new_page()
            
            # 设置基本内容
            await test_page.set_content("<html><body><h1>Test</h1></body></html>")
            
            # 验证页面可以正常加载
            title = await test_page.title()
            
            await test_page.close()
            
            return title == "Test"
        
        except FileNotFoundError as e:
            self.logger.error(f"浏览器可执行文件未找到: {e}")
            return False
        except PermissionError as e:
            self.logger.error(f"测试浏览器连接权限不足: {e}")
            return False
        except ConnectionError as e:
            self.logger.error(f"浏览器连接失败: {e}")
            return False
        except RuntimeError as e:
            # 捕获浏览器运行时错误，如页面操作失败、JavaScript执行错误等
            self.logger.error(f"测试浏览器连接失败: {e}")
            return False
        finally:
            await self._close_browser()
    
    async def get_browser_info(self) -> Dict[str, Any]:
        """获取浏览器信息"""
        try:
            if not self.browser:
                return {"status": "not_initialized"}
            
            return {
                "status": "ready",
                "user_agent": await self.browser.user_agent(),
                "viewport": {"width": self.width, "height": self.viewport_height}
            }
        
        except FileNotFoundError as e:
            return {"status": "error", "error": f"浏览器文件未找到: {e}"}
        except PermissionError as e:
            return {"status": "error", "error": f"权限不足: {e}"}
        except ConnectionError as e:
            return {"status": "error", "error": f"连接失败: {e}"}
        except RuntimeError as e:
            # 捕获浏览器信息获取时的运行时错误，如页面操作失败、资源访问错误等
            return {"status": "error", "error": str(e)}
    
    async def clear_cache(self):
        """清理模板缓存"""
        async with self._cache_lock:
            self._template_cache.clear()
            self.logger.info("模板缓存已清理")
    
    async def get_performance_stats(self) -> Dict[str, Any]:
        """获取性能统计信息"""
        cache_stats = await self.get_cache_stats()
        
        return {
            'cache_stats': cache_stats,
            'cached_templates': list(self._template_cache.keys()),
            'jinja2_enabled': JINJA2_AVAILABLE and self.jinja_env is not None,
            'playwright_enabled': PLAYWRIGHT_AVAILABLE,
            'template_path': str(self.template_path),
            'template_exists': await aiofiles.os.path.exists(self.template_path) if self.template_path else False
        }
    
    async def optimize_for_batch_generation(self):
        """为批量生成优化配置"""
        # 预热缓存
        await self._preload_templates()
        
        # 启用更激进的缓存策略
        if self.jinja_env:
            # Jinja2环境已经配置了缓存
            self.logger.info("批量生成优化已启用")
    
    async def _load_user_item_macro_template(self):
        """加载用户条目宏模板（异步版本）"""
        try:
            macro_path = Path(__file__).parent.parent / "templates" / "user_item_macro.html"
            if await aiofiles.os.path.exists(macro_path):
                async with aiofiles.open(macro_path, 'r', encoding='utf-8') as f:
                    macro_content = await f.read()
                
                # 创建环境并加载宏模板
                env = Environment(
                    loader=FileSystemLoader(str(macro_path.parent)),
                    autoescape=select_autoescape(['html', 'xml'])
                )
                return env.from_string(macro_content)
        except Exception as e:
            self.logger.warning(f"加载用户条目宏模板失败: {e}")
        
        return None

