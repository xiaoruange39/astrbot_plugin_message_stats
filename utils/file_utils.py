"""
文件操作工具模块
提供异步JSON文件读写功能
"""

import json
import aiofiles
from pathlib import Path
from typing import Dict, Any


async def load_json_file(file_path: str) -> Dict[str, Any]:
    """异步加载JSON文件
    
    Args:
        file_path (str): JSON文件路径
        
    Returns:
        Dict[str, Any]: 解析后的JSON数据
        
    Raises:
        FileNotFoundError: 当文件不存在时抛出
        json.JSONDecodeError: 当文件内容不是有效JSON时抛出
        IOError: 当文件读取失败时抛出
    """
    try:
        async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
            content = await f.read()
            return json.loads(content)
    except FileNotFoundError:
        raise FileNotFoundError(f"文件不存在: {file_path}")
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(f"文件内容不是有效JSON: {file_path}", e.doc, e.pos)
    except IOError as e:
        raise IOError(f"文件读取失败: {file_path}, 错误: {e}")


async def save_json_file(file_path: str, data: Dict[str, Any]) -> None:
    """异步保存JSON文件，自动创建目录"""
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    
    async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))