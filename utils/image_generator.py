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
import shutil
import uuid
import html
import base64
from urllib.parse import quote

from astrbot.api import logger as astrbot_logger

# 异步 HTTP 请求（用于获取 TG/Discord 头像）
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False

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
from .group_id_utils import get_fallback_group_name, is_official_qq_openid, is_placeholder_group_name




class ImageGenerationError(Exception):
    """图片生成异常
    
    当图片生成过程中发生错误时抛出的自定义异常。
    
    Attributes:
        message (str): 异常消息，描述具体的错误原因
        
    Example:
        >>> raise ImageGenerationError("Playwright未安装，无法生成图片")
    """
    pass


_AVATAR_COLORS = ['#F59E0B','#3B82F6','#8B5CF6','#EC4899','#10B981','#EF4444','#14B8A6','#F97316','#6366F1','#84CC16','#06B6D4','#D946EF','#0EA5E9','#EAB308','#A855F7']

def _gen_bubble_layout(users):
    import math, random; n = len(users)
    if n == 0: return [], [], [], 800, 800
    max_msg = max(u.display_total if u.display_total is not None else u.message_count for u in users)
    if max_msg == 0: max_msg = 1
    sqrt_max = max(1, math.sqrt(max_msg))
    sizes = [max(80, min(250, 80 + ((u.display_total if u.display_total is not None else u.message_count) / max_msg) ** 0.4 * 170)) for u in users]
    fonts = [max(14, min(28, 14 + (s-80)/170 * 14)) for s in sizes]
    placed, raw_pos = [], []
    def overlaps(x, y, r):
        for px, py, pr in placed:
            dx, dy = x - px, y - py
            if dx*dx + dy*dy < (r + pr + 8)**2: return True
        return False
    random.seed(42)
    jitter = lambda: (random.random() - 0.5) * 5.0
    for i in range(n):
        r = sizes[i]/2
        if i == 0: bx, by = 0, 0
        else:
            ta = math.atan2(-placed[0][1], -placed[0][0]) + i * 2.39996; found = False
            for st in range(1, 1000):
                sr = r + sizes[0]/2 + st * 4.0
                for ao in range(24):
                    a = ta + ao * math.pi / 12
                    tx, ty = math.cos(a) * sr + jitter(), math.sin(a) * sr + jitter()
                    if not overlaps(tx, ty, r): bx, by = tx, ty; found = True; break
                if found: break
            if not found: bx, by = 0, 0
        raw_pos.append((bx, by)); placed.append((bx, by, r))
    
    if n == 1:
        min_x = -sizes[0]/2
        max_x = sizes[0]/2
        min_y = -sizes[0]/2
        max_y = sizes[0]/2
    else:
        min_x = min(p[0]-sizes[i]/2 for i,p in enumerate(raw_pos))
        max_x = max(p[0]+sizes[i]/2 for i,p in enumerate(raw_pos))
        min_y = min(p[1]-sizes[i]/2 for i,p in enumerate(raw_pos))
        max_y = max(p[1]+sizes[i]/2 for i,p in enumerate(raw_pos))
        
    margin = 20
    cw = 800
    ch = 800
    sx = (cw - margin*2) / max(1, max_x - min_x)
    sy = (ch - margin*2) / max(1, max_y - min_y)
    scale = min(1.0, min(sx, sy))
    
    ox = (cw - (max_x - min_x) * scale) / 2 - min_x * scale + margin
    oy = (ch - (max_y - min_y) * scale) / 2 - min_y * scale + margin
    
    fs2, ff2, fp2 = [], [], []
    final_max_y = 0
    
    for i in range(n):
        fx = raw_pos[i][0] * scale + ox
        fy = raw_pos[i][1] * scale + oy
        fs = max(60, sizes[i] * scale); ff = max(10, fonts[i] * scale)
        fs2.append(int(fs)); ff2.append(int(ff)); fp2.append((int(fx - fs/2), int(fy - fs/2)))
        
        bottom = fy + fs/2
        if bottom > final_max_y: final_max_y = bottom

    area_h = 800
    if n > 0 and final_max_y + margin > 800:
        area_h = final_max_y + margin

    return fs2, ff2, fp2, 800, int(area_h)


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
    
    def __init__(self, config: PluginConfig):
        """初始化图片生成器
        
        Args:
            config (PluginConfig): 插件配置对象，包含生成参数和设置
        """
        self.config = config
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
        
        # 头像缓存字典 {user_id: avatar_url}，每次生成图片时预获取，用完即弃
        self._avatar_cache: Dict[str, str] = {}
        self._font_css_cache_key = None
        self._font_css_cache_value = ""

    def _update_template_path(self):
        """根据主题配置更新模板路径（支持自动根据时间切换主题）"""
        theme = getattr(self.config, 'theme', 'default')
        auto_switch = getattr(self.config, 'auto_theme_switch', False)
        
        if auto_switch:
            # 自动根据时间切换主题
            theme = self._get_auto_theme(theme)
            self.logger.info(f"自动主题切换已启用，当前时间匹配主题: {theme}")
        
        template_map = {
        'default': 'rank_template_cartoon_light.html',
        'cartoon_light': 'rank_template_cartoon_light.html',
        'cartoon_dark': 'rank_template_cartoon_dark.html',
        'liquid_glass': 'rank_template_liquid_glass.html',
        'liquid_glass_dark': 'rank_template_liquid_glass_dark.html',
        }
        template_file = template_map.get(theme, 'rank_template.html')
        self.template_path = self._templates_dir / template_file
        self.logger.info(f"使用排行榜主题: {theme}, 模板: {template_file}")
    
    def _get_auto_theme(self, base_theme: str) -> str:
        """根据当前时间自动选择合适的主题
        
        根据 auto_theme_switch 配置中的 light/dark 切换时间，
        判断当前应该使用浅色主题还是深色主题，并自动映射到对应的主题版本。
        
        Args:
            base_theme: 用户配置的基础主题名称
            
        Returns:
            str: 主题名称，根据当前时段自动映射到对应的浅色/深色版本
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
            
            # 深色→浅色 映射表（深色时段配置的主题在浅色时段自动映射）
            light_theme_map = {
                'cartoon_dark': 'cartoon_light',
                'liquid_glass_dark': 'liquid_glass',
            }
            # 浅色→深色 映射表（浅色时段配置的主题在深色时段自动映射）
            dark_theme_map = {
                'liquid_glass': 'liquid_glass_dark',
                'default': 'cartoon_dark',
                'cartoon_light': 'cartoon_dark',
            }
            
            # 判断当前时间段
            if light_minutes <= current_minutes < dark_minutes:
                # 浅色时间段：深色主题自动映射回浅色版本
                return light_theme_map.get(base_theme, base_theme)
            else:
                # 深色时间段：浅色主题自动映射回深色版本
                return dark_theme_map.get(base_theme, 'cartoon_dark')
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
        """预加载模板文件到缓存（使用模板路径区分的缓存键）"""
        try:
            if os.path.exists(self.template_path):
                # 使用异步文件读取优化
                async with aiofiles.open(self.template_path, 'r', encoding='utf-8') as f:
                    template_content = await f.read()
                
                # 缓存模板内容
                template_hash = self._get_template_hash(template_content)
                cache_key = self._get_template_cache_key()
                async with self._cache_lock:
                    self._template_cache[cache_key] = {
                        'content': template_content,
                        'hash': template_hash,
                        'template': self.jinja_env.from_string(template_content) if self.jinja_env else None
                    }
                
                self.logger.info(f"模板预加载完成，缓存键: {cache_key}")
            else:
                self.logger.warning(f"模板文件不存在: {self.template_path}")
        except Exception as e:
            self.logger.error(f"模板预加载失败: {e}")
    
    def _get_template_hash(self, content: str) -> str:
        """获取模板内容的哈希值，用于缓存验证"""
        return hashlib.md5(content.encode('utf-8')).hexdigest()

    def _get_template_cache_key(self) -> str:
        """获取基于当前模板路径的缓存键，确保不同主题模板各自独立缓存"""
        return f"main_template:{self.template_path.stem}"

    def _resolve_font_path(self) -> Optional[Path]:
        font_path = str(getattr(self.config, 'font_path', '') or '').strip()
        if not font_path:
            return None

        raw_path = Path(font_path).expanduser()
        candidates = []
        if raw_path.is_absolute():
            candidates.append(raw_path)
        else:
            plugin_dir = Path(__file__).parent.parent
            candidates.extend([
                plugin_dir / raw_path,
                plugin_dir / "fonts" / raw_path.name,
            ])
            font_base_dirs = getattr(self.config, 'font_base_dirs', []) or []
            for base_dir in font_base_dirs:
                base_path = Path(str(base_dir))
                candidates.extend([
                    base_path / raw_path,
                    base_path / raw_path.name,
                ])
            try:
                data_dir = Path(StarTools.get_data_dir('message_stats'))
                legacy_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_message_stats"))
                candidates.extend([
                    data_dir / raw_path,
                    data_dir / "resources" / "fonts" / raw_path.name,
                    legacy_data_dir / raw_path,
                    legacy_data_dir / "resources" / "fonts" / raw_path.name,
                ])
            except Exception:
                pass

        checked = []
        for candidate in candidates:
            checked.append(str(candidate))
            if candidate.exists() and candidate.is_file():
                resolved = candidate.resolve()
                self.logger.info(f"自定义字体文件解析成功: {resolved}")
                return resolved

        self.logger.warning(f"自定义字体文件不存在，使用主题默认字体: {font_path}; 已检查: {' | '.join(checked)}")
        return None

    def _get_font_format(self, font_path: Path) -> str:
        suffix = font_path.suffix.lower()
        if suffix in {'.otf'}:
            return 'opentype'
        if suffix in {'.woff'}:
            return 'woff'
        if suffix in {'.woff2'}:
            return 'woff2'
        return 'truetype'

    def _get_font_mime_type(self, font_path: Path) -> str:
        suffix = font_path.suffix.lower()
        if suffix in {'.otf'}:
            return 'font/otf'
        if suffix in {'.woff'}:
            return 'font/woff'
        if suffix in {'.woff2'}:
            return 'font/woff2'
        return 'font/ttf'

    def _get_custom_font_css(self) -> str:
        font_path = self._resolve_font_path()
        if not font_path:
            self._font_css_cache_key = None
            self._font_css_cache_value = ""
            return ""

        try:
            stat = font_path.stat()
            cache_key = (str(font_path), stat.st_mtime_ns, stat.st_size)
            if cache_key == self._font_css_cache_key:
                return self._font_css_cache_value

            font_data = base64.b64encode(font_path.read_bytes()).decode('ascii')
        except OSError as e:
            self._font_css_cache_key = None
            self._font_css_cache_value = ""
            self.logger.warning(f"读取自定义字体文件失败，使用主题默认字体: {e}")
            return ""

        font_format = self._get_font_format(font_path)
        mime_type = self._get_font_mime_type(font_path)
        css = (
            "@font-face { "
            "font-family: 'MessageStatsCustomFont'; "
            f"src: url(\"data:{mime_type};base64,{font_data}\") format('{font_format}'); "
            "font-weight: 100 900; font-style: normal; font-display: block; "
            "}\n"
            ":root { --message-stats-font-family: 'MessageStatsCustomFont', 'Microsoft YaHei', 'Segoe UI', sans-serif; }\n"
            "body, body * { font-family: var(--message-stats-font-family) !important; }"
        )
        self.logger.info(f"自定义字体文件已读取，准备注入CSS: {font_path}")
        self._font_css_cache_key = cache_key
        self._font_css_cache_value = css
        return css

    async def _apply_custom_font(self, page: Page):
        css = self._get_custom_font_css()
        if css:
            await page.add_style_tag(content=css)

    async def _wait_for_assets(self, page: Page):
        await page.evaluate("""
            async () => {
                if (document.fonts && document.fonts.ready) {
                    await document.fonts.ready;
                }
                const images = Array.from(document.querySelectorAll('img'));
                await Promise.all(images.map(img => {
                    if (img.complete) return Promise.resolve();
                    return new Promise(resolve => {
                        img.addEventListener('load', resolve, { once: true });
                        img.addEventListener('error', resolve, { once: true });
                    });
                }));
            }
        """)

    async def _get_cached_template(self) -> Optional[Union[str, Template]]:
        """获取缓存的模板（使用模板路径区分的缓存键，确保深浅色主题独立缓存）"""
        cache_key = self._get_template_cache_key()
        async with self._cache_lock:
            cached = self._template_cache.get(cache_key)
            if cached:
                self._cache_hits += 1
                return cached.get('template') if self.jinja_env else cached.get('content')
            else:
                self._cache_misses += 1
                return None
    
    async def _update_template_cache(self, content: str):
        """更新模板缓存（使用模板路径区分的缓存键）"""
        try:
            template_hash = self._get_template_hash(content)
            cache_key = self._get_template_cache_key()
            async with self._cache_lock:
                self._template_cache[cache_key] = {
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
            
            # 启动时清理上次异常退出残留的临时图片文件
            await self._cleanup_stale_temp_files()
            
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
    
    async def _cleanup_stale_temp_files(self):
        """清理上次异常退出残留的临时图片文件和 Playwright 临时数据目录
        
        当插件被 kill -9、OOM 杀死、断电等异常情况时，
        finally 块不会执行，tmp 文件和 Playwright profiles 会残留。
        每次初始化时扫描临时目录，清理残留文件。
        """
        try:
            import glob
            temp_dir = tempfile.gettempdir()
            patterns = ["rank_image_*.png", "milestone_*.png"]
            cleaned = 0
            for pattern in patterns:
                search_path = os.path.join(temp_dir, pattern)
                for file_path in glob.glob(search_path):
                    try:
                        os.unlink(file_path)
                        cleaned += 1
                    except OSError:
                        pass
            if cleaned > 0:
                self.logger.info(f"启动清理：已删除 {cleaned} 个上次残留的临时图片文件")
            
            # 清理 Playwright 残留的临时 profiles 目录和 Chromium 锁文件
            # 进程异常退出时这些目录/文件不会被 playwright.stop() 清理
            pw_cleaned = 0
            for entry in os.listdir(temp_dir):
                entry_path = os.path.join(temp_dir, entry)
                if not entry.startswith((
                    "playwright-", "playwright_",
                    "msgstats_pw_",
                    ".org.chromium.Chromium.",
                    "org.chromium.Chromium.",
                    "playwright-artifacts-",
                    "playwright_chromiumdev_profile-",
                    "pulse-",
                )):
                    continue
                try:
                    if os.path.isdir(entry_path):
                        shutil.rmtree(entry_path, ignore_errors=True)
                    else:
                        os.unlink(entry_path)
                    pw_cleaned += 1
                except OSError:
                    pass
            if pw_cleaned > 0:
                self.logger.info(f"启动清理：已删除 {pw_cleaned} 个上次残留的 Playwright 临时文件/目录")
        except Exception as e:
            self.logger.warning(f"清理残留临时文件时出现异常: {e}")
    
    async def _ensure_browser(self):
        """确保浏览器已启动（懒加载）并增加任务计数
        
        使用异步锁防止并发启动。如果是第一个任务则启动浏览器，
        然后增加活跃任务计数器。
        如果 Chromium 浏览器未安装，自动尝试安装后重试。
        """
        async with self._browser_lock:
            self._active_tasks += 1
            if self.browser:
                return
            
            self.logger.info("按需启动浏览器...")
            error_msg = await self._try_launch_browser()
            if error_msg is None:
                return  # 启动成功
            
            # 启动失败，尝试自动安装 Chromium
            self.logger.warning(f"Chromium 启动失败: {error_msg}")
            self.logger.info("正在自动安装 Chromium 浏览器，可能需要 1-2 分钟...")
            
            install_ok = await self._auto_install_chromium()
            if install_ok:
                # 重试启动
                retry_error = await self._try_launch_browser()
                if retry_error is None:
                    self.logger.info("Chromium 浏览器安装并启动成功")
                    return
            
            # 自动安装也失败了
            self._active_tasks -= 1
            raise ImageGenerationError(
                f"Chromium 浏览器未安装或缺少系统依赖。\n"
                f"请手动运行: playwright install chromium\n"
                f"Linux 用户可能还需要: playwright install-deps chromium\n"
                f"原始错误: {error_msg}"
            )
    
    async def _try_launch_browser(self) -> Optional[str]:
        """尝试启动浏览器，成功返回 None，失败返回错误信息字符串"""
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
            return None
        except Exception as e:
            return str(e)
    
    async def _auto_install_chromium(self) -> bool:
        """自动安装 Chromium 浏览器，返回是否成功"""
        try:
            # 先尝试基本的 chromium 安装
            self.logger.info("执行 playwright install chromium...")
            proc = await asyncio.create_subprocess_exec(
                "playwright", "install", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                self.logger.error("playwright install chromium 超时（5 分钟）")
                return False
            if proc.returncode == 0:
                self.logger.info("playwright install chromium 完成")
                # 二进制装好了，补装系统依赖（无头浏览器运行需要 libnspr4.so 等库）
                # deps 失败不影响整体结果——可能已经装过或者非 Linux 环境
                self.logger.info("补充安装系统依赖 (playwright install-deps chromium)...")
                try:
                    proc_deps = await asyncio.create_subprocess_exec(
                        "playwright", "install-deps", "chromium",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(proc_deps.communicate(), timeout=300)
                    if proc_deps.returncode == 0:
                        self.logger.info("系统依赖安装完成")
                except (asyncio.TimeoutError, Exception) as e:
                    self.logger.warning(f"系统依赖安装跳过: {e}")
                return True
            
            # install chromium 失败了，可能是缺少系统依赖
            self.logger.info("正在安装系统依赖 (playwright install-deps chromium)...")
            proc2 = await asyncio.create_subprocess_exec(
                "playwright", "install-deps", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                await asyncio.wait_for(proc2.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc2.kill()
                await proc2.wait()
                self.logger.error("playwright install-deps 超时（5 分钟）")
                return False
            if proc2.returncode == 0:
                self.logger.info("系统依赖安装完成")
                # 再试一次 install chromium
                proc3 = await asyncio.create_subprocess_exec(
                    "playwright", "install", "chromium",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    await asyncio.wait_for(proc3.communicate(), timeout=300)
                except asyncio.TimeoutError:
                    proc3.kill()
                    await proc3.wait()
                    self.logger.error("playwright install chromium 重试超时（5 分钟）")
                    return False
                if proc3.returncode == 0:
                    return True
            
            self.logger.error("自动安装 Chromium 失败")
            return False
        except FileNotFoundError:
            self.logger.error("找不到 playwright 命令，请确保已安装 playwright 包")
            return False
        except Exception as e:
            self.logger.error(f"自动安装 Chromium 异常: {e}")
            return False
    
    async def _close_browser(self):
        """任务完成，减少任务计数，如果计数为0则关闭浏览器释放内存"""
        # 防止取消异常打断清理流程导致任务数泄漏
        import asyncio
        try:
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
        except asyncio.CancelledError:
            # 如果清理过程被取消，我们至少要把任务计数减一，以防止死锁和内存泄漏
            self._active_tasks = max(0, self._active_tasks - 1)
            if self._active_tasks == 0:
                self.browser = None
                self.playwright = None
            raise
    
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
            
            # 动态获取内容实际所需的宽度，如果没生成则默认1200
            dynamic_w = self.width
            if "area_w" in html_content: # 这只是个保守预设，下面直接用 JS 测
                pass
                
            # 设置页面内容（使用 load 而非 networkidle，避免外部资源加载超时）
            await page.set_content(html_content, wait_until="load")
            await self._apply_custom_font(page)
            await self._wait_for_assets(page)

            # 动态调整页面高度和宽度，确保边距一致
            body_height = await page.evaluate("document.body.scrollHeight")
            # 通过获取 container 的宽度加上两侧 padding 来确定精确的截图宽度
            body_width = await page.evaluate("document.querySelector('.container') ? document.querySelector('.container').offsetWidth + 100 : document.body.scrollWidth")
            await page.set_viewport_size({"width": body_width, "height": body_height})
            
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
                    os.unlink(str(temp_path))
                    self.logger.debug(f"已清理失败的临时文件: {temp_path}")
                except Exception as e:
                    self.logger.warning(f"清理临时文件失败: {e}")

    async def generate_rank_pdf(self,
                                users: List[UserData],
                                group_info: GroupInfo,
                                title: str,
                                current_user_id: Optional[str] = None,
                                llm_token_usage: Dict[str, int] = None,
                                titles_map: Optional[Dict[str, str]] = None) -> str:
        """生成排行榜 PDF（复用图片版 HTML，用 Chromium page.pdf() 渲染矢量 PDF）

        与 generate_rank_image 共用同一份 HTML，外观完全一致，区别仅在于输出为
        单页长图式 PDF 文件而非 PNG 截图。page.pdf() 仅在无头 Chromium 下可用，
        本项目 _try_launch_browser 已固定 headless=True，满足要求。

        Args:
            users: 用户数据列表（已按发言数降序排列）
            group_info: 群组信息
            title: 排行榜标题
            current_user_id: 当前用户ID，用于高亮显示
            llm_token_usage: LLM token使用统计
            titles_map: 用户ID到头衔的映射字典 {user_id: title}

        Returns:
            str: 生成的临时 PDF 路径

        Raises:
            ImageGenerationError: PDF 生成失败时抛出（供上层降级到图片/文字）
        """
        # 每次生成时重新检查主题（支持自动主题切换实时生效）
        self._update_template_path()

        # 按需启动浏览器
        await self._ensure_browser()

        temp_path = None
        page = None
        success = False

        try:
            # 创建局部页面变量，防止并发时互相覆盖（开启两倍高清渲染，PDF 会忽略但保持布局测量一致）
            page = await self.browser.new_page(device_scale_factor=2)

            # 设置视口
            await page.set_viewport_size({"width": self.width, "height": self.viewport_height})

            # 生成与图片版完全相同的 HTML（显式传入头衔映射）
            html_content = await self._generate_html(users, group_info, title, current_user_id, llm_token_usage, titles_map)

            # 设置页面内容（使用 load 而非 networkidle，避免外部资源加载超时）
            await page.set_content(html_content, wait_until="load")
            await self._apply_custom_font(page)
            await self._wait_for_assets(page)

            # 强制使用屏幕媒体，避免触发 @media print 改变外观，保证与截图一致
            await page.emulate_media(media="screen")

            # 动态测量内容宽高，保证 PDF 尺寸与截图版一致（复用 generate_rank_image 的测量逻辑）
            body_height = await page.evaluate("document.body.scrollHeight")
            body_width = await page.evaluate("document.querySelector('.container') ? document.querySelector('.container').offsetWidth + 100 : document.body.scrollWidth")

            # 生成临时文件路径
            temp_filename = f"rank_pdf_{uuid.uuid4().hex}.pdf"
            temp_path = Path(tempfile.gettempdir()) / temp_filename

            # 单页长 PDF：显式指定画布宽高、保留背景渐变、去除页边距
            await page.pdf(
                path=str(temp_path),
                width=f"{body_width}px",
                height=f"{body_height}px",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )

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
        except ImageGenerationError:
            # 直接透传，避免被下面的通用分支重新包装
            raise
        except Exception as e:
            # PDF 渲染可能抛出 Playwright 原生异常（非 RuntimeError 子类），统一包装为
            # ImageGenerationError，确保上层能可靠降级到图片/文字
            self.logger.error(f"生成排行榜 PDF 失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"生成 PDF 失败: {e}")

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
                    os.unlink(str(temp_path))
                    self.logger.debug(f"已清理失败的临时 PDF 文件: {temp_path}")
                except Exception as e:
                    self.logger.warning(f"清理临时 PDF 文件失败: {e}")

    @safe_generation(default_return=None)
    async def generate_personal_stats_image(self, data: dict, group_info: GroupInfo) -> str:
        """生成个人资料卡片图片
        
        Args:
            data: 用户数据字典
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
            # 个人卡片使用 480 宽
            page = await self.browser.new_page(device_scale_factor=2)
            await page.set_viewport_size({"width": 480, "height": self.viewport_height})
            
            # 检测当前主题并选择对应模板
            theme = getattr(self.config, 'theme', 'default')
            auto_switch = getattr(self.config, 'auto_theme_switch', False)
            if auto_switch:
                theme = self._get_auto_theme(theme)
            is_dark = theme.endswith('_dark')
            
            # 根据主题选择个人面板模板
            personal_template_map = {
                'default': 'personal_stats.html',
                'cartoon_light': 'personal_stats_cartoon_light.html',
                'cartoon_dark': 'personal_stats_cartoon_dark.html',
                'liquid_glass': 'personal_stats.html',
                'liquid_glass_dark': 'personal_stats.html',
            }
            template_file = personal_template_map.get(theme, 'personal_stats.html')
            template_path = self._templates_dir / template_file
            if not os.path.exists(template_path):
                self.logger.warning(f"个人卡片模板文件不存在: {template_path}")
                return None
            data['is_dark'] = is_dark
            data['custom_font_css'] = self._get_custom_font_css()
            data['current_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 异步读取模板文件（与里程碑模板渲染方式一致，避免 Jinja2 缺少 FileSystemLoader 的问题）
            async with aiofiles.open(template_path, 'r', encoding='utf-8') as f:
                template_content = await f.read()
            if JINJA2_AVAILABLE and self.jinja_env:
                template_obj = self.jinja_env.from_string(template_content)
                html_content = template_obj.render(data=data)
            else:
                html_content = template_content
                # 简易替换 Jinja2 占位符
                import re
                def replace_placeholder(m):
                    key = m.group(1).strip()
                    keys = key.split('.')
                    val = data
                    for k in keys:
                        if isinstance(val, dict):
                            val = val.get(k, "")
                        else:
                            val = ""
                            break
                    return str(val)
                html_content = re.sub(r'\{\{[^}]+\}\}', replace_placeholder, html_content)
            
            # 将HTML内容设置到页面
            await page.set_content(html_content, wait_until='networkidle')
            await self._apply_custom_font(page)
            await self._wait_for_assets(page)

            # 动态调整高度以适应内容，但不小于最小高度
            height = await page.evaluate("() => document.getElementById('inspect-root') ? document.getElementById('inspect-root').offsetHeight : document.body.scrollHeight")
            await page.set_viewport_size({"width": 480, "height": int(height)})
            
            # 生成临时文件路径
            fd, temp_path = tempfile.mkstemp(suffix='.png', prefix='personal_stats_')
            os.close(fd)
            
            # 截图
            await page.screenshot(
                path=temp_path,
                full_page=True,
                type='png'
            )
            
            success = True
            return temp_path
            
        except Exception as e:
            self.logger.exception(f"生成个人卡片异常: {e}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception as e:
                    self.logger.debug(f"关闭页面失败: {e}")
            if not success and temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

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
                                       group_info: GroupInfo,
                                       sticker_count: int = 0,
                                       sticker_percentage: float = 0.0,
                                       avatar_url: str = "") -> str:
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
            percentage: 击败的活跃群友百分比
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
            if os.path.exists(milestone_template_path):
                async with aiofiles.open(milestone_template_path, 'r', encoding='utf-8') as f:
                    template_content = await f.read()
            else:
                self.logger.warning(f"里程碑模板文件不存在: {milestone_template_path}")
                return None
            
            # 准备模板数据
            avatar_url = self._validate_url_safe(str(avatar_url or "")) or self._get_avatar_url(user_id, nickname, group_info)
            group_name = self._get_display_group_name(group_info)
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            template_data = {
                'avatar_url': avatar_url,
                'nickname': self._escape_html_safe(nickname),
                'user_id': self._escape_html_safe(str(user_id)),
                'group_name': self._escape_html_safe(
                    f"{group_name}[{group_info.group_id}]"
                    if not is_official_qq_openid(group_info.group_id)
                    else group_name
                ),
            'show_group_id': not is_official_qq_openid(group_info.group_id),
                'milestone_count': milestone_count,
                'rank': rank,
                'daily_count': daily_count,
                'active_days': active_days,
                'last_date': self._escape_html_safe(last_date or "未知"),
                'group_total_messages': group_total_messages,
                'percentage': f"{percentage:.2f}",
                'sticker_count': sticker_count,
                'sticker_percentage': f"{sticker_percentage:.1f}",
                'current_time': current_time,
                'custom_font_css': self._get_custom_font_css(),
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
            await self._apply_custom_font(page)
            await self._wait_for_assets(page)

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
                    os.unlink(str(temp_path))
                    self.logger.debug(f"已清理失败的里程碑临时文件: {temp_path}")
                except Exception as e:
                    self.logger.warning(f"清理里程碑临时文件失败: {e}")
    
    def _resolve_current_theme(self) -> str:
        """解析当前实际生效的主题（考虑自动切换）"""
        theme = getattr(self.config, 'theme', 'default')
        if getattr(self.config, 'auto_theme_switch', False):
            theme = self._get_auto_theme(theme)
        return theme

    async def build_help_html(self, sections: List[Dict[str, Any]], meta: Dict[str, Any]) -> Optional[str]:
        """构建帮助菜单 HTML

        HTML 中不包含时间戳等易变内容，因此内容不变时哈希值稳定，
        可以直接作为帮助图片的缓存键。

        Args:
            sections: 指令章节数据
            meta: 标题、提示、版本号等附加信息

        Returns:
            str: 渲染好的 HTML，模板缺失时返回 None
        """
        template_path = self._templates_dir / 'help_template.html'
        if not os.path.exists(template_path):
            self.logger.warning(f"帮助菜单模板文件不存在: {template_path}")
            return None

        template_data = {
            'title': meta.get('title', '发言榜帮助'),
            'subtitle': meta.get('subtitle', ''),
            'tips': meta.get('tips', []),
            'version': meta.get('version', ''),
            'sections': sections,
            'is_dark': self._resolve_current_theme().endswith('_dark'),
            'custom_font_css': self._get_custom_font_css(),
        }

        async with aiofiles.open(template_path, 'r', encoding='utf-8') as f:
            template_content = await f.read()

        if JINJA2_AVAILABLE and self.jinja_env:
            return self.jinja_env.from_string(template_content).render(**template_data)

        self.logger.warning("Jinja2 不可用，无法渲染帮助菜单模板")
        return None

    @safe_generation(default_return=None)
    async def generate_help_image(self, html_content: str, output_path: str) -> Optional[str]:
        """把帮助菜单 HTML 渲染成图片并写入指定路径（懒加载浏览器，用完即关）

        Args:
            html_content: build_help_html 生成的 HTML
            output_path: 目标图片路径（通常是缓存目录中的文件）

        Returns:
            str: 图片路径，失败时返回 None

        Raises:
            ImageGenerationError: 浏览器渲染失败时抛出
        """
        if not html_content:
            return None

        await self._ensure_browser()

        page = None
        temp_path = None
        success = False
        target = Path(output_path)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            page = await self.browser.new_page(device_scale_factor=2)
            await page.set_viewport_size({"width": self.width, "height": self.viewport_height})

            await page.set_content(html_content, wait_until="load")
            await self._apply_custom_font(page)
            await self._wait_for_assets(page)

            body_height = await page.evaluate("document.body.scrollHeight")
            body_width = await page.evaluate(
                "document.querySelector('.container') ? document.querySelector('.container').offsetWidth + 100 : document.body.scrollWidth"
            )
            await page.set_viewport_size({"width": body_width, "height": body_height})

            # 先写临时文件再原子替换，避免并发请求读到半张图
            temp_path = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.tmp")
            await page.screenshot(path=temp_path, full_page=True)
            os.replace(str(temp_path), str(target))
            temp_path = None

            success = True
            return str(target)

        except FileNotFoundError as e:
            self.logger.error(f"帮助菜单资源未找到: {e}")
            raise ImageGenerationError(f"文件资源未找到: {e}")
        except PermissionError as e:
            self.logger.error(f"权限错误: {e}")
            raise ImageGenerationError(f"权限不足: {e}")
        except TimeoutError as e:
            self.logger.error(f"浏览器操作超时: {e}")
            raise ImageGenerationError(f"操作超时: {e}")
        except RuntimeError as e:
            self.logger.error(f"生成帮助菜单图片失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"生成图片失败: {e}")

        finally:
            if page:
                try:
                    await page.close()
                except Exception as e:
                    self.logger.warning(f"关闭页面时发生错误: {e}")

            await self._close_browser()

            if not success and temp_path and temp_path.exists():
                try:
                    os.unlink(str(temp_path))
                except Exception as e:
                    self.logger.warning(f"清理帮助菜单临时文件失败: {e}")

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
        
        # 预获取非QQ平台用户的真实头像（TG/Discord），填充到 _avatar_cache
        await self._prefetch_avatars(users, group_info)
        
        # 使用批量处理优化性能（显式传入头衔映射）
        self._current_group_info = group_info
        processed_data = self._process_user_data_batch(users, current_user_id, titles_map, group_info)
        
        # 计算统计数据
        total_messages = processed_data['total_messages']
        
        # 生成完整HTML
        html_template = await self._load_html_template()
        
        # 获取当前时间
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 准备模板数据（使用字典构建优化）
        llm_token_text = ""
        if llm_token_usage and llm_token_usage.get("total_tokens", 0) > 0:
            llm_token_text = f"LLM Token 消耗: {llm_token_usage.get('total_tokens', 0)} (输入{llm_token_usage.get('prompt_tokens', 0)}+输出{llm_token_usage.get('completion_tokens', 0)})"
        
        template_data = {
            'group_name': self._escape_html_safe(self._get_display_group_name(group_info)),
            'group_id': self._escape_html_safe(str(group_info.group_id)),
            'show_group_id': not is_official_qq_openid(group_info.group_id),
            'title': self._escape_html_safe(title),
            'total_messages': self._escape_html_safe(str(total_messages)),
            'user_count': self._escape_html_safe(str(len(users))),
            'current_time': self._escape_html_safe(current_time),
            'custom_font_css': self._get_custom_font_css(),
            'llm_token_info': self._escape_html_safe(llm_token_text) if llm_token_text else ""
        }
        
        # 生成HTML内容（优化渲染逻辑）
        return await self._render_html_template(html_template, template_data, processed_data['user_items'])
    
    def _process_user_data_batch(self, users: List[UserData], current_user_id: Optional[str], 
                                  titles_map: Optional[Dict[str, str]] = None, group_info=None) -> Dict[str, Any]:
        """批量处理用户数据，优化性能
        
        Args:
            users: 用户数据列表
            current_user_id: 当前用户ID，用于高亮显示
            titles_map: 用户ID到头衔的映射字典 {user_id: title}
                      优先使用此参数中的头衔，若无则回退到 user.display_title
            group_info: 群组信息，用于判断平台获取头像
        """
        if not users:
            return {'total_messages': 0, 'user_items': []}
        
        # 预计算统计数据 - 使用时间段内的发言数
        total_messages = sum(user.display_total if user.display_total is not None else user.message_count for user in users)
        max_messages = max((user.display_total if user.display_total is not None else user.message_count) for user in users) if users else 1
        
        # 批量生成用户项目
        sizes, fonts, pos, area_w, area_h = _gen_bubble_layout(users)
        if len(sizes) < len(users):
            sizes.extend([70] * (len(users) - len(sizes)))
            fonts.extend([10] * (len(users) - len(fonts)))
            pos.extend([(area_w/2, area_h/2)] * (len(users) - len(pos)))
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
            
            c = _AVATAR_COLORS[sum(ord(ch) for ch in str(user.user_id)) % 15]
            idx = _AVATAR_COLORS.index(c) if c in _AVATAR_COLORS else sum(ord(ch) for ch in str(user.user_id)) % 15
            g2 = _AVATAR_COLORS[(idx + 5) % 15]
            user_items.append({
                'rank': i + 1,
                'nickname': user.nickname,
                'title': user_title,
                'title_color': user_title_color,
                'avatar_url': self._get_avatar_url(user, user.nickname, self._current_group_info),
                'total': user_messages,
                'percentage': (user_messages / total_messages * 100) if total_messages > 0 else 0,
                'trend_label': getattr(user, 'display_trend_label', None),
                'trend_type': getattr(user, 'display_trend_type', None),
                'fill_ratio': (user_messages / max_messages * 100) if max_messages > 0 else 0,
                'last_date': user.last_date or "未知",
                'is_current_user': is_current_user,
                'is_separator': False,
                'bubble_size': sizes[i], 'bubble_font': fonts[i], 'pos_x': pos[i][0], 'pos_y': pos[i][1],
                'card_grad1': c, 'card_grad2': g2, '_group_info': group_info
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
                    'avatar_url': self._get_avatar_url(current_user_data, current_user_data.nickname, self._current_group_info),
                    'total': current_user_messages,
                    'percentage': (current_user_messages / total_messages * 100) if total_messages > 0 else 0,
                    'trend_label': getattr(current_user_data, 'display_trend_label', None),
                    'trend_type': getattr(current_user_data, 'display_trend_type', None),
                    'last_date': current_user_data.last_date or "未知",
                    'is_current_user': True,
                    'is_separator': True,
                    '_group_info': group_info
                })
        
        return {
            'total_messages': total_messages,
            'user_items': user_items,
            'area_w': area_w,
            'area_h': area_h
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
    _EMPTY_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>发言排行榜</title>
</head>
<body>
    <h1>发言排行榜</h1>
    <p>暂无数据</p>
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
            'group_name': self._escape_html_safe(self._get_display_group_name(group_info)),
            'group_id': self._escape_html_safe(str(group_info.group_id)),
            'show_group_id': not is_official_qq_openid(group_info.group_id),
            'title': self._escape_html_safe(title),
            'custom_font_css': self._get_custom_font_css()
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
        {{ custom_font_css | safe }}
        body {
            font-family: var(--message-stats-font-family, 'Microsoft YaHei', sans-serif);
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
        <div class="title">{{ group_name }}{% if show_group_id %}[{{ group_id }}]{% endif %}</div>
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
        """生成安全的用户条目HTML"""
        # 使用元组和字典预构建减少字符串操作
        css_classes = self._get_css_classes(item_data)
        styles = self._get_item_styles(item_data)
        safe_content = self._get_safe_content(item_data)
        
        # 使用更安全的字符串拼接方式
        # 对所有动态内容进行HTML转义
        safe_nickname = html.escape(safe_content['nickname'])
        safe_avatar_url = html.escape(safe_content['avatar_url'])
        safe_last_date = html.escape(safe_content['last_date'])
        safe_trend_label = html.escape(str(item_data.get('trend_label') or ""))
        safe_trend_type = html.escape(str(item_data.get('trend_type') or "flat"))
        trend_color = {
            "up": "#16A34A",
            "new": "#16A34A",
            "down": "#DC2626",
            "flat": "#6B7280",
        }.get(item_data.get("trend_type"), "#6B7280")
        safe_separator_style = html.escape(styles['separator'])
        safe_rank_color = html.escape(styles['rank_color'])
        safe_avatar_border = html.escape(styles['avatar_border'])
        
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
            f'            <div class="trend-badge trend-{safe_trend_type}" style="color:{trend_color};font-size:12px;font-weight:700;margin-top:3px;max-width:100%;line-height:1.35;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{safe_trend_label}</div>' if safe_trend_label else '',
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
        """获取安全的内容（优化版本）
        
        注意：不在此处进行 HTML 转义，转义推迟到最终渲染阶段：
        - Jinja2 路径：模板引擎的 autoescape 自动处理
        - Fallback 路径：_generate_user_item_html_safe 中手动转义
        避免重复转义导致 &amp; 等乱码。
        """
        # 不做HTML转义，仅提取原始值（转义由接收方负责）
        nickname = str(item_data.get('nickname', '未知用户'))
        last_date = str(item_data.get('last_date', '未知'))
        avatar_url = self._validate_url_safe(str(item_data.get('avatar_url', '')))
        
        # 处理头衔
        title = item_data.get('title', None)
        
        # 如果头像URL无效，使用回退彩色文字头像
        if not avatar_url:
            avatar_url = self._get_avatar_url(
                str(item_data.get('user_id', '0')),
                str(item_data.get('nickname', '')),
                item_data.get('_group_info', None)
            )
        
        content = {
            'nickname': nickname,
            'last_date': last_date,
            'avatar_url': avatar_url
        }
        
        if title:
            content['title'] = title
            
        return content

    def _escape_html_safe(self, text: str) -> str:
        """安全的HTML转义"""
        if not isinstance(text, str):
            text = str(text)
        return html.escape(text, quote=True)
    
    def _validate_url_safe(self, url: str) -> str:
        """验证并清理URL（支持 http/https 和 data URI）"""
        if not isinstance(url, str):
            url = str(url)
        
        if not url:
            return ""
        if not url.startswith(('http://', 'https://', 'data:')):
            return ""
        
        # 移除潜在的恶意字符
        url = url.replace('<', '').replace('>', '').replace('"', '').replace("'", '')
        return url

    _AVATAR_COLORS = ['#F59E0B','#3B82F6','#8B5CF6','#EC4899','#10B981',
                      '#EF4444','#14B8A6','#F97316','#6366F1','#84CC16',
                      '#06B6D4','#D946EF','#0EA5E9','#EAB308','#A855F7']

    @staticmethod
    def _get_avatar_color(seed: str) -> str:
        h = 0
        for ch in seed:
            h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        colors = ImageGenerator._AVATAR_COLORS
        return colors[h % len(colors)]

    @staticmethod
    def _generate_avatar_svg_data_uri(nickname: str, seed: str) -> str:
        letter = '?'
        if nickname and nickname.strip():
            letter = nickname.strip()[0]
        color = ImageGenerator._get_avatar_color(seed or 'x')
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="640" viewBox="0 0 640 640"><circle cx="320" cy="320" r="320" fill="{color}"/><text x="320" y="320" text-anchor="middle" dominant-baseline="central" font-size="280" font-weight="700" font-family="Microsoft YaHei,sans-serif" fill="white">{html.escape(letter)}</text></svg>'
        encoded = quote(svg)
        return f"data:image/svg+xml,{encoded}"

    # ========== 跨平台头像获取（TG / Discord / 飞书 / QQ） ==========

    @staticmethod
    def _detect_platform(group_id: str) -> str:
        """根据群ID特征识别平台
        
        - TG: 以 - 或 -100 开头的负数
        - Discord: 18-19 位纯数字
        - 飞书: 以 oc_ 开头
        - QQ: 5-10 位纯数字
        - 其他: 未知
        """
        gid = str(group_id).strip()
        
        # 飞书：oc_ 前缀
        if gid.startswith('oc_'):
            return 'feishu'
        
        # Telegram：以 - 或 -100 开头的负数
        if gid.startswith('-'):
            return 'telegram'
        
        # 到这里都是正数
        if gid.isdigit():
            length = len(gid)
            # QQ：5-10 位短数字
            if 5 <= length <= 10:
                return 'qq'
            # Discord：18-19 位 Snowflake
            if 18 <= length <= 19:
                return 'discord'
        
        return 'unknown'

    @staticmethod
    def _get_display_group_name(group_info: GroupInfo) -> str:
        group_id = str(group_info.group_id) if group_info else ""
        group_name = getattr(group_info, "group_name", "") if group_info else ""
        if not is_placeholder_group_name(group_name, group_id):
            return str(group_name).strip()
        return get_fallback_group_name(group_id)

    async def _prefetch_avatars(self, users: List[UserData], group_info: GroupInfo):
        """预获取本批用户的头像URL，填充到 _avatar_cache
        
        在生成HTML之前调用，异步并发获取TG/Discord/飞书的真实头像。
        获取失败或未配置Token的用户会自动回退到彩色文字头像。
        
        Args:
            users: 用户数据列表
            group_info: 群组信息
        """
        self._avatar_cache.clear()
        group_id_str = str(group_info.group_id) if group_info else ""
        platform = self._detect_platform(group_id_str)
        
        # QQ 不需要预获取，直接通过 qlogo.cn 拼接
        if platform == 'qq':
            return
        
        # 飞书：保留之前逻辑（回退彩色文字头像）
        if platform == 'feishu':
            return
        
        if platform == 'telegram':
            await self._prefetch_tg_avatars(users)
        elif platform == 'discord':
            await self._prefetch_dc_avatars(users)
    
    async def _prefetch_tg_avatars(self, users: List[UserData]):
        """预获取 Telegram 用户头像
        
        使用 TG Bot API 获取用户最新的头像文件路径，拼接为可下载的 URL。
        API 调用链路：getUserProfilePhotos -> getFile -> 拼接下载链接
        """
        tg_token = getattr(self.config, 'tg_bot_token', '')
        if not tg_token or not AIOHTTP_AVAILABLE:
            self.logger.debug("TG Bot Token 未配置或 aiohttp 不可用，跳过 TG 头像预获取")
            return
        
        self.logger.info(f"开始预获取 {len(users)} 个 TG 用户头像...")
        success_count = 0
        
        async def fetch_single(user_id: str):
            """获取单个用户的头像 URL"""
            try:
                # 步骤1: 获取用户头像列表
                photos_url = f"https://api.telegram.org/bot{tg_token}/getUserProfilePhotos"
                params = {"user_id": int(float(user_id)), "limit": 1}
                
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.get(photos_url, params=params) as resp:
                        if resp.status != 200:
                            return
                        data = await resp.json()
                    
                    if not data.get("ok") or not data.get("result", {}).get("photos"):
                        return
                    
                    # 取最新的头像（数组第一个），取最大分辨率的 file_id（数组最后一个）
                    latest_photos = data["result"]["photos"][0]
                    file_id = latest_photos[-1]["file_id"]
                    
                    # 步骤2: 获取文件路径
                    get_file_url = f"https://api.telegram.org/bot{tg_token}/getFile"
                    async with session.get(get_file_url, params={"file_id": file_id}) as resp2:
                        if resp2.status != 200:
                            return
                        file_data = await resp2.json()
                    
                    if not file_data.get("ok") or not file_data.get("result", {}).get("file_path"):
                        return
                    
                    file_path = file_data["result"]["file_path"]
                    
                    # 步骤3: 拼接最终下载链接
                    avatar_url = f"https://api.telegram.org/file/bot{tg_token}/{file_path}"
                    self._avatar_cache[user_id] = avatar_url
                    return True
            except Exception as e:
                self.logger.warning(f"获取 TG 头像失败 (user_id={user_id}): {e}")
                return None
        
        # 并发获取所有用户头像
        tasks = [fetch_single(u.user_id) for u in users if u.user_id]
        results = await asyncio.gather(*tasks)
        success_count = sum(1 for r in results if r)
        
        if success_count > 0:
            self.logger.info(f"TG 头像预获取完成: {success_count}/{len(users)} 成功")
        else:
            self.logger.warning("TG 头像预获取：所有用户均未获取到头像，将使用彩色文字头像回退")
    
    async def _prefetch_dc_avatars(self, users: List[UserData]):
        """预获取 Discord 用户头像
        
        通过 Discord API 获取用户信息，提取 avatar hash，拼接 CDN URL。
        如果用户的 avatar hash 以 a_ 开头，使用 GIF 格式。
        """
        dc_token = getattr(self.config, 'dc_bot_token', '')
        if not dc_token or not AIOHTTP_AVAILABLE:
            self.logger.debug("Discord Bot Token 未配置或 aiohttp 不可用，跳过 DC 头像预获取")
            return
        
        self.logger.info(f"开始预获取 {len(users)} 个 Discord 用户头像...")
        success_count = 0
        
        # Discord 需要 Bot 认证头
        headers = {
            "Authorization": f"Bot {dc_token}",
            "User-Agent": "DiscordBot (astrbot_plugin_message_stats, 1.0)"
        }
        
        async def fetch_single(user_id: str):
            """获取单个 Discord 用户的头像 URL"""
            try:
                user_url = f"https://discord.com/api/v10/users/{int(float(user_id))}"
                
                async with aiohttp.ClientSession(
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as session:
                    async with session.get(user_url) as resp:
                        if resp.status != 200:
                            return
                        user_data = await resp.json()
                    
                    avatar_hash = user_data.get("avatar")
                    if not avatar_hash:
                        # 用户没有设置头像，使用默认头像算法
                        # Discord 默认头像：user_id 右移 22 位后对 6（默认头像数量）取模
                        try:
                            uid_int = int(float(user_id))
                            default_index = (uid_int >> 22) % 6
                            avatar_url = f"https://cdn.discordapp.com/embed/avatars/{default_index}.png"
                            self._avatar_cache[user_id] = avatar_url
                            return True
                        except (ValueError, OverflowError):
                            return
                    
                    # 有自定义头像：判断是否为 GIF（a_ 开头）
                    ext = "gif" if avatar_hash.startswith("a_") else "png"
                    avatar_url = f"https://cdn.discordapp.com/avatars/{int(float(user_id))}/{avatar_hash}.{ext}"
                    self._avatar_cache[user_id] = avatar_url
                    return True
            except Exception as e:
                self.logger.debug(f"获取 DC 头像失败 (user_id={user_id}): {e}")
                return None
        
        # 并发获取所有用户头像
        tasks = [fetch_single(u.user_id) for u in users if u.user_id]
        results = await asyncio.gather(*tasks)
        success_count = sum(1 for r in results if r)
        
        if success_count > 0:
            self.logger.info(f"DC 头像预获取完成: {success_count}/{len(users)} 成功")
        else:
            self.logger.debug("DC 头像预获取：所有用户均未获取到头像，将使用彩色文字头像回退")

    def _get_avatar_url(self, user_id: str, nickname: str = "", group_info=None) -> str:
        """获取用户头像URL
        
        优先级：
        1. 预获取缓存（_avatar_cache，由 _prefetch_avatars 填充的 TG/Discord/飞书真实头像）
        2. QQ 平台：qlogo.cn 真实头像
        3. 回退：彩色文字 SVG 头像
        
        Args:
            user_id: 用户ID
            nickname: 用户昵称
            group_info: 群组信息
        
        Returns:
            str: 头像URL
        """
        if hasattr(user_id, "avatar_url"):
            avatar_url = self._validate_url_safe(str(getattr(user_id, "avatar_url", "") or ""))
            if avatar_url:
                return avatar_url
            nickname = nickname or getattr(user_id, "nickname", "")
            user_id = getattr(user_id, "user_id", "")

        user_id_str = str(user_id)
        group_id_str = str(group_info.group_id) if group_info else ""
        platform = self._detect_platform(group_id_str)
        
        # 优先级1: 预获取缓存（TG/Discord 的真实头像）
        cached_url = self._avatar_cache.get(user_id_str)
        if cached_url:
            return cached_url
        
        # 优先级2: QQ 平台使用 qlogo.cn 真实头像
        if platform == 'qq':
            return f"https://q1.qlogo.cn/g?b=qq&nk={user_id_str}&s=640"
        
        # 优先级3: 回退彩色文字头像（飞书、未知平台、或预获取失败的 TG/Discord）
        return self._generate_avatar_svg_data_uri(nickname, user_id_str)
    
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
            if os.path.exists(self.template_path):
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
            font-family: var(--message-stats-font-family, 'Microsoft YaHei', 'Segoe UI', sans-serif);
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
    <div class="title">{{ group_name }}{% if show_group_id %}[{{ group_id }}]{% endif %}</div>
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
            font-family: var(--message-stats-font-family, 'Microsoft YaHei', 'Segoe UI', sans-serif);
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
    <div class="title">{{ group_name }}{% if show_group_id %}[{{ group_id }}]{% endif %}</div>
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
            'template_exists': os.path.exists(self.template_path) if self.template_path else False
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
            if os.path.exists(macro_path):
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
