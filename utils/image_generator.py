"""
图片生成模块
负责将HTML模板转换为排行榜图片
"""

import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
import tempfile
import os

try:
    from playwright.async_api import async_playwright, Browser, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logging.warning("Playwright未安装，图片生成功能将不可用")

from .models import UserData, GroupInfo, PluginConfig

logger = logging.getLogger('message_stats_plugin')


class ImageGenerationError(Exception):
    """图片生成异常"""
    pass


class ImageGenerator:
    """图片生成器"""
    
    def __init__(self, config: PluginConfig):
        self.config = config
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.playwright = None
        
        # 图片生成配置
        self.width = 1200
        self.timeout = 10000
        self.viewport_height = 1
        
        # 模板路径
        self.template_path = Path(__file__).parent.parent / "templates" / "rank_template.html"
    
    async def initialize(self):
        """初始化图片生成器"""
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("Playwright未安装，图片生成功能将不可用")
            raise ImageGenerationError("Playwright未安装，无法生成图片")
        
        try:
            logger.info("开始初始化图片生成器...")
            self.playwright = await async_playwright().start()
            logger.info("Playwright启动成功")
            
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
            logger.info("Chromium浏览器启动成功")
            
            logger.info("图片生成器初始化完成")
        except Exception as e:
            logger.error(f"初始化图片生成器失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"初始化失败: {e}")
        
        except Exception as e:
            logger.error(f"初始化图片生成器失败: {e}")
            raise ImageGenerationError(f"初始化失败: {e}")
    
    async def cleanup(self):
        """清理资源"""
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
            
            logger.info("图片生成器资源已清理")
        
        except Exception as e:
            logger.error(f"清理图片生成器资源失败: {e}")
    
    async def generate_rank_image(self, 
                                 users: List[UserData], 
                                 group_info: GroupInfo, 
                                 title: str,
                                 current_user_id: Optional[str] = None) -> str:
        """生成排行榜图片"""
        logger.info(f"开始生成排行榜图片: {title}")
        logger.info(f"用户数据: {len(users)} 个用户")
        
        if not self.browser:
            logger.info("浏览器未初始化，开始初始化...")
            await self.initialize()
        
        try:
            logger.info("创建新页面...")
            # 创建页面
            self.page = await self.browser.new_page()
            logger.info("页面创建成功")
            
            # 设置视口
            logger.info(f"设置视口大小: {self.width}x{self.viewport_height}")
            await self.page.set_viewport_size({"width": self.width, "height": self.viewport_height})
            
            # 生成HTML内容
            logger.info("生成HTML内容...")
            html_content = self._generate_html(users, group_info, title, current_user_id)
            logger.info(f"HTML内容生成成功，长度: {len(html_content)}")
            
            # 设置页面内容
            logger.info("设置页面内容...")
            await self.page.set_content(html_content, wait_until="networkidle")
            
            # 等待页面加载完成
            logger.info("等待页面加载完成...")
            await self.page.wait_for_timeout(2000)
            
            # 动态调整页面高度
            logger.info("获取页面高度...")
            body_height = await self.page.evaluate("document.body.scrollHeight")
            logger.info(f"页面高度: {body_height}")
            await self.page.set_viewport_size({"width": self.width, "height": body_height})
            
            # 生成临时文件路径
            logger.info("生成临时文件路径...")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                temp_path = temp_file.name
            logger.info(f"临时文件路径: {temp_path}")
            
            # 截图
            logger.info("开始截图...")
            await self.page.screenshot(path=temp_path, full_page=True)
            logger.info(f"截图完成: {temp_path}")
            
            logger.info(f"排行榜图片生成成功: {temp_path}")
            return temp_path
        
        except Exception as e:
            logger.error(f"生成排行榜图片失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            raise ImageGenerationError(f"生成图片失败: {e}")
        
        finally:
            if self.page:
                logger.info("关闭页面...")
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
        
        # 构建用户条目HTML
        user_items_html = ""
        current_user_found = False
        
        for i, user in enumerate(users):
            # 检查是否是当前用户
            is_current_user = current_user_id and user.user_id == current_user_id
            if is_current_user:
                current_user_found = True
            
            # 计算百分比
            percentage = (user.total / total_messages * 100) if total_messages > 0 else 0
            
            # 获取头像URL
            avatar_url = self._get_avatar_url(user.user_id)
            
            # 格式化最后发言日期
            last_date = user.last_date or "未知"
            
            # 生成用户条目
            user_items_html += self._generate_user_item_html(
                rank=i + 1,
                user=user,
                avatar_url=avatar_url,
                percentage=percentage,
                is_current_user=is_current_user,
                last_date=last_date
            )
        
        # 如果当前用户不在排行榜中，添加到末尾
        if current_user_id and not current_user_found:
            current_user = None
            for user in users:
                if user.user_id == current_user_id:
                    current_user = user
                    break
            
            if current_user:
                # 找到当前用户的排名
                current_rank = 1
                for user in users:
                    if user.total > current_user.total:
                        current_rank += 1
                
                percentage = (current_user.total / total_messages * 100) if total_messages > 0 else 0
                avatar_url = self._get_avatar_url(current_user.user_id)
                last_date = current_user.last_date or "未知"
                
                user_items_html += self._generate_user_item_html(
                    rank=current_rank,
                    user=current_user,
                    avatar_url=avatar_url,
                    percentage=percentage,
                    is_current_user=True,
                    last_date=last_date,
                    is_separator=True
                )
        
        # 生成完整HTML
        html_template = self._load_html_template()
        
        # 获取当前时间
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 替换模板变量
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
    <title>{title}</title>
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
        <div class="title">{group_name}[{group_id}]</div>
        <div class="subtitle">{title}</div>
        <div class="empty-text">
            暂无发言数据<br>
            期待大家的活跃发言！
        </div>
    </div>
</body>
</html>
"""
        
        return html_template.format(
            group_name=group_info.group_name or f"群{group_info.group_id}",
            group_id=group_info.group_id,
            title=title
        )
    
    def _generate_user_item_html(self, 
                                rank: int, 
                                user: UserData, 
                                avatar_url: str, 
                                percentage: float,
                                is_current_user: bool,
                                last_date: str,
                                is_separator: bool = False) -> str:
        """生成用户条目HTML"""
        # CSS类名 - 只为当前用户添加高亮
        if is_current_user:
            item_class = "user-item-current"
        else:
            item_class = "user-item"
        
        # 排名样式
        rank_class = "rank-current" if is_current_user else "rank"
        
        # 头像边框颜色（改为白色）
        avatar_border = "#ffffff" if is_current_user else "#ffffff"
        
        # 排名样式（改为#格式，浅蓝色，增大字体）
        rank_text = f"#{rank}"
        rank_color = "#3B82F6"  # 浅蓝色
        
        # 如果是分隔符，添加特殊样式
        separator_style = "margin-top: 20px; border-top: 2px dashed #bdc3c7;" if is_separator else ""
        
        return f"""
        <div class="{item_class}" style="{separator_style}">
            <div class="rank-number" style="color: {rank_color}; font-weight: bold; font-size: 36px;">{rank_text}</div>
            <img class="avatar" src="{avatar_url}" style="border-color: {avatar_border};" />
            <div class="info">
                <div class="name-date">
                    <div class="nickname">{user.nickname}</div>
                    <div class="date">最近发言: {last_date}</div>
                </div>
                <div class="stats">
                    <div class="count">{user.total} 次</div>
                    <div class="percentage">({percentage:.2f}%)</div>
                </div>
            </div>
        </div>"""
    
    def _get_avatar_url(self, user_id: str) -> str:
        """获取用户头像URL"""
        return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    
    def _load_html_template(self) -> str:
        """加载HTML模板"""
        try:
            logger.info(f"加载HTML模板: {self.template_path}")
            if self.template_path.exists():
                logger.info("模板文件存在，开始读取...")
                with open(self.template_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                logger.info(f"模板读取成功，长度: {len(content)}")
                return content
            else:
                logger.warning(f"模板文件不存在: {self.template_path}")
                # 使用内置模板
                logger.info("使用默认内置模板")
                return self._get_default_template()
        except Exception as e:
            logger.error(f"加载HTML模板失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return self._get_default_template()
    
    def _get_default_template(self) -> str:
        """获取默认HTML模板"""
        return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
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
    <div class="title">{group_name}[{group_id}]</div>
    <div class="title">{title}</div>
    <div class="user-list">
        {user_items}
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
            logger.error(f"测试浏览器连接失败: {e}")
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
