"""
AstrBot 群发言统计插件
统计群成员发言次数，生成排行榜

插件信息:
- 名称: astrbot_plugin_message_stats
- 显示名称: 群发言统计
- 描述: 统计群成员发言次数，生成排行榜
- 版本: 1.0
- 作者: xiaoruange39

功能特性:
- 群消息监听和统计
- 多种排行榜类型（总榜、日榜、周榜、月榜）
- 图片和文字两种显示模式
- 完整的配置管理系统
- 权限控制和安全管理

使用方法:
1. 将插件文件放置到 AstrBot 插件目录
2. 安装依赖: pip install -r requirements.txt
3. 重启 AstrBot 服务
4. 在群聊中使用相关命令

命令列表:
- #发言榜 / #水群榜 / #B话榜 - 查看总排行榜
- #今日发言榜 - 查看今日排行榜
- #本周发言榜 - 查看本周排行榜
- #本月发言榜 - 查看本月排行榜
- #发言榜设置 - 查看当前设置
- #清除发言榜单 - 清除当前群数据
- #设置发言榜数量 数量 - 设置显示人数
- #设置发言榜图片 模式 - 设置图片模式

配置说明:
- 可配置显示人数（5-50人）
- 支持图片/文字两种模式
- 完整的权限控制

依赖要求:
- astrbot >= 4.5.0
- aiofiles >= 23.0.0
- playwright >= 1.40.0
- pydantic >= 2.0.0
- python-dateutil >= 2.8.0
- orjson >= 3.9.0
- cachetools >= 5.0.0

注意事项:
- 需要群主或管理员权限进行设置
- 图片生成需要安装 Playwright 浏览器
- 建议定期清理旧数据以节省存储空间
- 插件会自动创建必要的数据目录

技术支持:
- 如有问题请查看日志文件
- 支持数据导入导出功能
- 提供完整的错误处理机制
"""

__version__ = "1.0"
__author__ = "xiaoruange39"
__description__ = "群发言统计插件，支持排行榜"

# 导出主要组件
from .main import MessageStatsPlugin

# 插件元数据
PLUGIN_INFO = {
    "name": "astrbot_plugin_message_stats",
    "display_name": "群发言统计",
    "description": __description__,
    "version": __version__,
    "author": __author__,
    "entry_point": "message_stats",
    "main_class": "MessageStatsPlugin"
}

# 插件配置默认值
DEFAULT_CONFIG = {
    "auto_record_enabled": True,
    "rand": 20,
    "if_send_pic": 1
}

# 支持的命令列表
SUPPORTED_COMMANDS = [
    "发言榜", "水群榜", "B话榜",
    "今日发言榜", "本周发言榜", "本月发言榜",
    "发言榜设置",
    "设置发言榜数量", "设置发言榜图片",
    "清除发言榜单"
]

# 权限要求
PERMISSION_REQUIREMENTS = {
    "admin_only": [
        "设置发言榜数量", "设置发言榜图片", "清除发言榜单"
    ],
    "public": [
        "发言榜", "水群榜", "B话榜",
        "今日发言榜", "本周发言榜", "本月发言榜",
        "发言榜设置"
    ]
}

def get_plugin_info():
    """获取插件信息"""
    return PLUGIN_INFO.copy()

def get_default_config():
    """获取默认配置"""
    return DEFAULT_CONFIG.copy()

def get_supported_commands():
    """获取支持的命令列表"""
    return SUPPORTED_COMMANDS.copy()

def get_permission_requirements():
    """获取权限要求"""
    return PERMISSION_REQUIREMENTS.copy()
