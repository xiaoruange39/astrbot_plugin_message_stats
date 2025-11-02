"""
图片生成模块
负责将HTML模板转换为排行榜图片
"""

import asyncio
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
import tempfile
import os
import traceback

from astrbot.api import logger as astrbot_logger

# Jinja2模板引擎
try:
    from jinja2 import Template, Environment, select_autoescape
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
        self.width = 1200
        self.timeout = 10000
        self.viewport_height = 1
        
        # 模板路径
        self.template_path = Path(__file__).parent.parent / "templates" / "rank_template.html"
        
        # 初始化Jinja2环境
        self._init_jinja2_env()
    
    def _init_jinja2_env(self):
        """初始化Jinja2环境
        
        创建Jinja2模板环境，启用自动转义以防止XSS攻击。
        如果Jinja2不可用，将使用不安全的字符串拼接方式作为备用。
        
        Returns:
            None: 无返回值，初始化结果通过日志输出
            
        Example:
            >>> self._init_jinja2_env()
            # 将初始化Jinja2环境或记录警告信息
        """
        if JINJA2_AVAILABLE:
            # 创建Jinja2环境，启用自动转义
            self.jinja_env = Environment(
                autoescape=select_autoescape(['html', 'xml']),
                trim_blocks=True,
                lstrip_blocks=True
            )
            self.logger.info("Jinja2环境初始化成功")
        else:
            self.jinja_env = None
            self.logger.warning("Jinja2不可用，将使用不安全的字符串拼接")
    
    async def initialize(self):
        """初始化图片生成器
        
        异步初始化Playwright浏览器和相关的渲染环境。
        包括启动浏览器实例和配置渲染参数。
        
        Raises:
            ImageGenerationError: 当Playwright未安装或初始化失败时抛出
            OSError: 当浏览器启动失败时抛出
            
        Returns:
            None: 无返回值，初始化成功后浏览器实例可用
            
        Example:
            >>> generator = ImageGenerator(config)
            >>> await generator.initialize()
            >>> print(generator.browser is not None)
            True
        """
        if not PLAYWRIGHT_AVAILABLE:
            self.logger.error("Playwright未安装，图片生成功能将不可用")
            raise ImageGenerationError("Playwright未安装，无法生成图片")
        
        try:
            self.logger.info("开始初始化图片生成器...")
            self.playwright = await async_playwright().start()
            self.logger.info("Playwright启动成功")
            
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
            
            self.logger.info("图片生成器初始化完成")
        except Exception as e:
            self.logger.error(f"初始化图片生成器失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"初始化失败: {e}")
    
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
        
        except Exception as e:
            self.logger.error(f"清理图片生成器资源失败: {e}")
    
    async def generate_rank_image(self, 
                                 users: List[UserData], 
                                 group_info: GroupInfo, 
                                 title: str,
                                 current_user_id: Optional[str] = None) -> str:
        """生成排行榜图片"""
        self.logger.info(f"开始生成排行榜图片: {title}")
        self.logger.info(f"用户数据: {len(users)} 个用户")
        
        if not self.browser:
            self.logger.info("浏览器未初始化，开始初始化...")
            await self.initialize()
        
        try:
            self.logger.info("创建新页面...")
            # 创建页面
            self.page = await self.browser.new_page()
            self.logger.info("页面创建成功")
            
            # 设置视口
            self.logger.info(f"设置视口大小: {self.width}x{self.viewport_height}")
            await self.page.set_viewport_size({"width": self.width, "height": self.viewport_height})
            
            # 生成HTML内容
            self.logger.info("生成HTML内容...")
            html_content = self._generate_html(users, group_info, title, current_user_id)
            self.logger.info(f"HTML内容生成成功，长度: {len(html_content)}")
            
            # 设置页面内容
            self.logger.info("设置页面内容...")
            await self.page.set_content(html_content, wait_until="networkidle")
            
            # 等待页面加载完成
            self.logger.info("等待页面加载完成...")
            await self.page.wait_for_timeout(2000)
            
            # 动态调整页面高度
            self.logger.info("获取页面高度...")
            body_height = await self.page.evaluate("document.body.scrollHeight")
            self.logger.info(f"页面高度: {body_height}")
            await self.page.set_viewport_size({"width": self.width, "height": body_height})
            
            # 生成临时文件路径
            self.logger.info("生成临时文件路径...")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                temp_path = temp_file.name
            self.logger.info(f"临时文件路径: {temp_path}")
            
            # 截图
            self.logger.info("开始截图...")
            await self.page.screenshot(path=temp_path, full_page=True)
            self.logger.info(f"截图完成: {temp_path}")
            
            self.logger.info(f"排行榜图片生成成功: {temp_path}")
            return temp_path
        
        except Exception as e:
            self.logger.error(f"生成排行榜图片失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"生成图片失败: {e}")
        
        finally:
            if self.page:
                self.logger.info("关闭页面...")
                await self.page.close()
                self.page = None
    
    def _generate_html(self, 
                      users: List[UserData], 
                      group_info: GroupInfo, 
                      title: str,
                      current_user_id: Optional[str] = None) -> str:
        """生成HTML内容"""
        if not users:
            return self._generate_empty_html(group_info, title)
        
        # 计算统计数据
        total_messages = sum(user.total for user in users)
        max_messages = max(user.total for user in users) if users else 1
        
        # 构建用户数据列表，在第一次遍历时优化性能
        user_items_data = []
        current_user_data = None
        current_user_rank = 0
        
        # 第一次遍历：同时记录所有用户信息和当前用户数据
        for i, user in enumerate(users):
            # 检查是否是当前用户
            is_current_user = current_user_id and user.user_id == current_user_id
            
            # 计算百分比
            percentage = (user.total / total_messages * 100) if total_messages > 0 else 0
            
            # 获取头像URL
            avatar_url = self._get_avatar_url(user.user_id)
            
            # 格式化最后发言日期
            last_date = user.last_date or "未知"
            
            # 如果是当前用户，保存数据用于后续处理
            if is_current_user:
                current_user_data = {
                    'nickname': user.nickname,
                    'user_id': user.user_id,
                    'total': user.total,
                    'last_date': user.last_date
                }
                current_user_rank = i + 1
            
            # 添加用户数据到列表
            user_items_data.append({
                'rank': i + 1,
                'nickname': user.nickname,
                'avatar_url': avatar_url,
                'total': user.total,
                'percentage': percentage,
                'last_date': last_date,
                'is_current_user': is_current_user,
                'is_separator': False
            })
        
        # 如果当前用户不在排行榜中，添加到末尾
        if current_user_id and not current_user_data:
            # 第二次遍历只为了查找当前用户（性能优化：只在小概率情况下执行）
            for user in users:
                if user.user_id == current_user_id:
                    current_user_data = {
                        'nickname': user.nickname,
                        'user_id': user.user_id,
                        'total': user.total,
                        'last_date': user.last_date
                    }
                    break
            
            if current_user_data:
                # 计算当前用户的排名（基于消息数量）
                current_rank = 1
                for user in users:
                    if user.total > current_user_data['total']:
                        current_rank += 1
                
                percentage = (current_user_data['total'] / total_messages * 100) if total_messages > 0 else 0
                avatar_url = self._get_avatar_url(current_user_data['user_id'])
                last_date = current_user_data['last_date'] or "未知"
                
                user_items_data.append({
                    'rank': current_rank,
                    'nickname': current_user_data['nickname'],
                    'avatar_url': avatar_url,
                    'total': current_user_data['total'],
                    'percentage': percentage,
                    'last_date': last_date,
                    'is_current_user': True,
                    'is_separator': True
                })
        
        # 生成完整HTML
        html_template = self._load_html_template()
        
        # 获取当前时间
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 使用Jinja2渲染或回退到字符串格式化
        try:
            if JINJA2_AVAILABLE and self.jinja_env:
                template = self.jinja_env.from_string(html_template)
                html_content = template.render(
                    group_name=group_info.group_name or f"群{group_info.group_id}",
                    group_id=group_info.group_id,
                    title=title,
                    user_items=user_items_data,
                    total_messages=total_messages,
                    user_count=len(users),
                    current_time=current_time
                )
            else:
                # 回退到字符串格式化（不推荐，但作为备用）
                user_items_html = ""
                for item in user_items_data:
                    user_items_html += self._generate_user_item_html_safe(item)
                html_content = html_template.format(
                    group_name=group_info.group_name or f"群{group_info.group_id}",
                    group_id=group_info.group_id,
                    title=title,
                    user_items=user_items_html,
                    total_messages=total_messages,
                    user_count=len(users),
                    current_time=current_time
                )
        except Exception as e:
            self.logger.error(f"HTML模板渲染失败: {e}")
            # 使用安全的备用方法
            user_items_html = ""
            for item in user_items_data:
                user_items_html += self._generate_user_item_html_safe(item)
            html_content = html_template.format(
                group_name=group_info.group_name or f"群{group_info.group_id}",
                group_id=group_info.group_id,
                title=title,
                user_items=user_items_html,
                total_messages=total_messages,
                user_count=len(users),
                current_time=current_time
            )
        
        return html_content
    
    def _generate_empty_html(self, group_info: GroupInfo, title: str) -> str:
        """生成空数据HTML"""
        html_template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <style>
        body {{
            font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            margin: 0;
            padding: 40px;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            background: rgba(255,255,255,0.95);
            border-radius: 20px;
            padding: 60px;
            text-align: center;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            max-width: 600px;
        }}
        .title {{
            font-size: 32px;
            color: #2c3e50;
            margin-bottom: 20px;
            font-weight: bold;
        }}
        .subtitle {{
            font-size: 24px;
            color: #7f8c8d;
            margin-bottom: 40px;
        }}
        .empty-icon {{
            font-size: 80px;
            margin-bottom: 30px;
        }}
        .empty-text {{
            font-size: 18px;
            color: #95a5a6;
            line-height: 1.6;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="empty-icon">📊</div>
        <div class="title">{{ group_name }}[{{ group_id }}]</div>
        <div class="subtitle">{{ title }}</div>
        <div class="empty-text">
            暂无发言数据<br>
            期待大家的活跃发言！
        </div>
    </div>
</body>
</html>
"""
        
        try:
            if JINJA2_AVAILABLE and self.jinja_env:
                template = self.jinja_env.from_string(html_template)
                return template.render(
                    group_name=group_info.group_name or f"群{group_info.group_id}",
                    group_id=group_info.group_id,
                    title=title
                )
            else:
                # 回退到字符串格式化
                return html_template.format(
                    group_name=group_info.group_name or f"群{group_info.group_id}",
                    group_id=group_info.group_id,
                    title=title
                )
        except Exception as e:
            self.logger.error(f"空数据HTML模板渲染失败: {e}")
            # 使用安全的备用方法
            return html_template.format(
                group_name=group_info.group_name or f"群{group_info.group_id}",
                group_id=group_info.group_id,
                title=title
            )
    
    def _generate_user_item_html_safe(self, item_data: Dict[str, Any]) -> str:
        """生成安全的用户条目HTML（备用方法）"""
        # CSS类名
        item_class = "user-item-current" if item_data['is_current_user'] else "user-item"
        
        # 排名样式
        rank_class = "rank-current" if item_data['is_current_user'] else "rank"
        
        # 头像边框颜色
        avatar_border = "#ffffff"
        
        # 排名样式
        rank_color = "#3B82F6"  # 浅蓝色
        
        # 如果是分隔符，添加特殊样式
        separator_style = "margin-top: 20px; border-top: 2px dashed #bdc3c7;" if item_data['is_separator'] else ""
        
        # 使用基本的HTML转义来防止XSS
        import html
        safe_nickname = html.escape(str(item_data['nickname']))
        safe_last_date = html.escape(str(item_data['last_date']))
        safe_avatar_url = html.escape(str(item_data['avatar_url']))
        
        return f"""
        <div class="{item_class}" style="{separator_style}">
            <div class="rank-number" style="color: {rank_color}; font-weight: bold; font-size: 36px;">#{item_data['rank']}</div>
            <img class="avatar" src="{safe_avatar_url}" style="border-color: {avatar_border};" />
            <div class="info">
                <div class="name-date">
                    <div class="nickname">{safe_nickname}</div>
                    <div class="date">最近发言: {safe_last_date}</div>
                </div>
                <div class="stats">
                    <div class="count">{item_data['total']} 次</div>
                    <div class="percentage">({item_data['percentage']:.2f}%)</div>
                </div>
            </div>
        </div>"""

    def _get_avatar_url(self, user_id: str) -> str:
        """获取用户头像URL"""
        return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    
    def _load_html_template(self) -> str:
        """加载HTML模板"""
        try:
            self.logger.info(f"加载HTML模板: {self.template_path}")
            if self.template_path.exists():
                self.logger.info("模板文件存在，开始读取...")
                with open(self.template_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                self.logger.info(f"模板读取成功，长度: {len(content)}")
                
                # 检查模板是否使用Jinja2语法
                if '{{' in content or '{%' in content:
                    self.logger.info("检测到Jinja2模板语法")
                else:
                    self.logger.warning("模板未使用Jinja2语法，建议更新为安全模板")
                
                return content
            else:
                self.logger.warning(f"模板文件不存在: {self.template_path}")
                # 使用内置模板
                self.logger.info("使用默认内置模板")
                return self._get_default_template()
        except Exception as e:
            self.logger.error(f"加载HTML模板失败: {e}")
            self.logger.error(f"详细错误: {traceback.format_exc()}")
            return self._get_default_template()
    
    def _get_default_template(self) -> str:
        """获取默认HTML模板"""
        return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: 'Microsoft YaHei', 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            padding: 30px;
            min-height: 100vh;
        }}
        .title {{
            text-align: center;
            font-size: 28px;
            color: #2c3e50;
            margin-bottom: 25px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
        }}
        .user-list {{
            max-width: 800px;
            margin: 0 auto;
            background: rgba(255,255,255,0.9);
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            padding: 20px;
        }}
        .user-item {{
            display: flex;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid #eee;
            transition: transform 0.2s;
        }}
        .user-item:hover {{
            transform: translateX(10px);
        }}
        .user-item-current {{
            display: flex;
            align-items: center;
            padding: 15px;
            border-bottom: 1px solid #eee;
            transition: transform 0.2s;
            background-color: #f0e6ff;
            border-radius: 12px;
        }}
        .user-item-current:hover {{
            transform: translateX(10px);
        }}
        .rank {{
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: #3498db;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            font-weight: bold;
            margin-right: 20px;
        }}
        .rank-current {{
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: #e74c3c;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            font-weight: bold;
            margin-right: 20px;
        }}
        .avatar {{
            width: 60px;
            height: 60px;
            border-radius: 50%;
            margin: 0 20px;
            border: 3px solid #3498db;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .info {{
            flex: 1;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .name-date {{
            display: flex;
            flex-direction: column;
        }}
        .nickname {{
            font-size: 20px;
            color: #34495e;
            font-weight: 500;
            line-height: 1.2;
        }}
        .date {{
            color: #666;
            font-size: 14px;
            margin-top: 4px;
        }}
        .stats {{
            text-align: right;
            font-size: 18px;
            min-width: 120px;
        }}
        .count {{
            color: #e74c3c;
            font-weight: bold;
        }}
        .percentage {{
            color: #27ae60;
            font-size: 16px;
        }}
    </style>
</head>
<body>
    <div class="title">{{ group_name }}[{{ group_id }}]</div>
    <div class="title">{{ title }}</div>
    <div class="user-list">
        {% for item in user_items %}
        <div class="{{ 'user-item-current' if item.is_current_user else 'user-item' }}" 
             style="{{ 'margin-top: 20px; border-top: 2px dashed #bdc3c7;' if item.is_separator else '' }}">
            <div class="{{ 'rank-current' if item.is_current_user else 'rank' }}" 
                 style="color: #3B82F6; font-weight: bold; font-size: 36px;">#{{ item.rank }}</div>
            <img class="avatar" src="{{ item.avatar_url }}" style="border-color: #ffffff;" />
            <div class="info">
                <div class="name-date">
                    <div class="nickname">{{ item.nickname }}</div>
                    <div class="date">最近发言: {{ item.last_date }}</div>
                </div>
                <div class="stats">
                    <div class="count">{{ item.total }} 次</div>
                    <div class="percentage">({{ "%.2f"|format(item.percentage) }}%)</div>
                </div>
            </div>
        </div>
        {% endfor %}
    </div>
</body>
</html>
"""
    
    async def test_browser_connection(self) -> bool:
        """测试浏览器连接"""
        try:
            if not self.browser:
                await self.initialize()
            
            # 创建一个测试页面
            test_page = await self.browser.new_page()
            
            # 设置基本内容
            await test_page.set_content("<html><body><h1>Test</h1></body></html>")
            
            # 验证页面可以正常加载
            title = await test_page.title()
            
            await test_page.close()
            
            return title == "Test"
        
        except Exception as e:
            self.logger.error(f"测试浏览器连接失败: {e}")
            return False
    
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
        
        except Exception as e:
            return {"status": "error", "error": str(e)}
