"""
HTML模板模块
包含用于生成排行榜图片的HTML模板
"""

from pathlib import Path
from astrbot.api import logger as logger

# 模板文件路径
TEMPLATE_DIR = Path(__file__).parent
RANK_TEMPLATE_PATH = TEMPLATE_DIR / "rank_template.html"

def get_rank_template() -> str:
    """获取排行榜HTML模板"""
    try:
        if RANK_TEMPLATE_PATH.exists():
            with open(RANK_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
                return f.read()
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
        body {{
            font-family: Arial, sans-serif;
            background: #f5f5f5;
            padding: 20px;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 10px;
            padding: 20px;
        }}
        .title {{
            text-align: center;
            color: #333;
            margin-bottom: 20px;
        }}
        .user-item {{
            display: flex;
            align-items: center;
            padding: 10px;
            border-bottom: 1px solid #eee;
        }}
        .rank {{
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: #007bff;
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-right: 15px;
            font-weight: bold;
        }}
        .info {{
            flex: 1;
        }}
        .nickname {{
            font-weight: bold;
            color: #333;
        }}
        .count {{
            color: #666;
            font-size: 14px;
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

def template_exists() -> bool:
    """检查模板文件是否存在"""
    return RANK_TEMPLATE_PATH.exists()

def load_template() -> str:
    """加载模板（兼容性函数）"""
    return get_rank_template()
