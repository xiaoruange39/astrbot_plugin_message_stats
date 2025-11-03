"""
文件操作工具模块
提供异步JSON文件读写功能
"""

import json
import aiofiles
from pathlib import Path
from typing import Dict, Any


async def load_json_file(file_path: str) -> Dict[str, Any]:
    """异步加载JSON文件"""
    async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
        content = await f.read()
        return json.loads(content)


async def save_json_file(file_path: str, data: Dict[str, Any]) -> None:
    """异步保存JSON文件，自动创建目录"""
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    
    async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))