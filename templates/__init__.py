"""
HTML模板模块
包含用于生成排行榜图片的HTML模板
"""

import aiofiles
import aiofiles.os
import logging
from pathlib import Path

# 设置日志记录器
logger = logging.getLogger(__name__)

# 模板文件路径
TEMPLATE_DIR = Path(__file__).parent
RANK_TEMPLATE_PATH = TEMPLATE_DIR / "rank_template.html"

async def get_rank_template() -> str:
    """获取排行榜HTML模板"""
    try:
        if await aiofiles.os.path.exists(RANK_TEMPLATE_PATH):
            async with aiofiles.open(RANK_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
                return await f.read()
        else:
            # 返回默认模板
            return get_default_template()
    except (IOError, UnicodeDecodeError) as e:
        logger.warning(f"读取模板文件失败: {e}")
        return get_default_template()

def get_default_template() -> str:
    """获取默认HTML模板"""
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>发言排行榜</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: Arial, sans-serif;
            background-color: #f5f5f5;
            padding: 20px;
            margin: 0;
            line-height: 1.6;
        }}
        
        .container {{
            max-width: 800px;
            margin: 0 auto;
            background-color: #ffffff;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }}
        
        .title {{
            text-align: center;
            color: #333333;
            margin-bottom: 20px;
            font-size: 24px;
            font-weight: bold;
        }}
        
        .user-item {{
            display: flex;
            align-items: center;
            padding: 10px;
            border-bottom: 1px solid #eeeeee;
            transition: background-color 0.3s ease;
        }}
        
        .user-item:hover {{
            background-color: #f9f9f9;
        }}
        
        .user-item:last-child {{
            border-bottom: none;
        }}
        
        .rank {{
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background-color: #007bff;
            color: #ffffff;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 15px;
            font-weight: bold;
            font-size: 14px;
            flex-shrink: 0;
        }}
        
        .info {{
            flex: 1;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        
        .nickname {{
            font-weight: bold;
            color: #333333;
            font-size: 16px;
            margin-bottom: 2px;
        }}
        
        .count {{
            color: #666666;
            font-size: 14px;
            font-weight: 500;
        }}
        
        /* 响应式设计 */
        @media (max-width: 768px) {{
            body {{
                padding: 10px;
            }}
            
            .container {{
                padding: 15px;
            }}
            
            .title {{
                font-size: 20px;
            }}
            
            .user-item {{
                padding: 8px;
            }}
            
            .rank {{
                width: 35px;
                height: 35px;
                font-size: 12px;
                margin-right: 10px;
            }}
            
            .nickname {{
                font-size: 14px;
            }}
            
            .count {{
                font-size: 12px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="title">{title}</h1>
        {user_items}
    </div>
</body>
</html>
"""

async def template_exists() -> bool:
    """异步检查模板文件是否存在"""
    try:
        return await aiofiles.os.path.exists(RANK_TEMPLATE_PATH)
    except Exception as e:
        logger.warning(f"检查模板文件存在性失败: {e}")
        return False

# 移除冗余的load_template函数，统一使用get_rank_template()
