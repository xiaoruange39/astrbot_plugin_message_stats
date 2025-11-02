"""
数据模型定义
定义插件中使用的所有数据结构
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from enum import Enum
import json


class RankType(Enum):
    """排行榜类型"""
    TOTAL = "total"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class MessageDate:
    """消息日期记录"""
    year: int
    month: int
    day: int
    
    def to_date(self) -> date:
        """转换为date对象"""
        return date(self.year, self.month, self.day)
    
    def to_datetime(self) -> datetime:
        """转换为datetime对象"""
        return datetime.combine(self.to_date(), datetime.min.time())
    
    @classmethod
    def from_date(cls, date_obj: date):
        """从date对象创建"""
        return cls(date_obj.year, date_obj.month, date_obj.day)
    
    @classmethod
    def from_datetime(cls, datetime_obj: datetime):
        """从datetime对象创建"""
        return cls.from_date(datetime_obj.date())
    
    def __str__(self) -> str:
        return f"{self.year}-{self.month:02d}-{self.day:02d}"
    
    def __eq__(self, other) -> bool:
        if not isinstance(other, MessageDate):
            return False
        return (self.year == other.year and 
                self.month == other.month and 
                self.day == other.day)
    
    def __lt__(self, other) -> bool:
        if not isinstance(other, MessageDate):
            return NotImplemented
        return (self.year, self.month, self.day) < (other.year, other.month, other.day)


@dataclass
class UserData:
    """用户数据"""
    user_id: str
    nickname: str
    total: int = 0
    history: List[MessageDate] = field(default_factory=list)
    last_date: Optional[str] = None
    
    def add_message(self, message_date: MessageDate):
        """添加消息记录"""
        self.total += 1
        
        # 检查是否是同一天，避免重复添加
        if not self.history or self.history[-1] != message_date:
            self.history.append(message_date)
        
        # 更新最后发言日期
        self.last_date = str(message_date)
    
    def get_last_message_date(self) -> Optional[MessageDate]:
        """获取最后消息日期"""
        return self.history[-1] if self.history else None
    
    def get_message_count_in_period(self, start_date: date, end_date: date) -> int:
        """获取指定时间段内的消息数量"""
        count = 0
        for hist_date in self.history:
            if start_date <= hist_date.to_date() <= end_date:
                count += 1
        return count
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "total": self.total,
            "history": [str(h) for h in self.history],
            "last_date": self.last_date
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserData':
        """从字典创建"""
        user_data = cls(
            user_id=data["user_id"],
            nickname=data["nickname"],
            total=data.get("total", 0),
            last_date=data.get("last_date")
        )
        
        # 重建history
        if "history" in data:
            for hist_str in data["history"]:
                try:
                    year, month, day = map(int, hist_str.split('-'))
                    user_data.history.append(MessageDate(year, month, day))
                except (ValueError, IndexError):
                    # 忽略无效的历史记录
                    continue
        
        return user_data
    
    def __lt__(self, other) -> bool:
        """按总消息数排序"""
        if not isinstance(other, UserData):
            return NotImplemented
        return self.total < other.total  # 升序排列，用于sorted()函数


@dataclass
class PluginConfig:
    """插件配置"""
    is_arr: int = 0
    rand: int = 20
    if_send_pic: int = 1
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "is_arr": self.is_arr,
            "rand": self.rand,
            "if_send_pic": self.if_send_pic
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PluginConfig':
        """从字典创建"""
        return cls(
            is_arr=data.get("is_arr", 0),
            rand=data.get("rand", 20),
            if_send_pic=data.get("if_send_pic", 1)
        )


@dataclass
class GroupInfo:
    """群组信息"""
    group_id: str
    group_name: str = ""
    member_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "member_count": self.member_count
        }


@dataclass
class RankData:
    """排行榜数据"""
    group_info: GroupInfo
    title: str
    users: List[UserData]
    total_messages: int
    generated_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "group_info": self.group_info.to_dict(),
            "title": self.title,
            "users": [user.to_dict() for user in self.users],
            "total_messages": self.total_messages,
            "generated_at": self.generated_at.isoformat()
        }


# 工具函数
def load_json_file(file_path: str) -> Optional[Dict[str, Any]]:
    """安全加载JSON文件"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        return None


def save_json_file(file_path: str, data: Dict[str, Any]) -> bool:
    """安全保存JSON文件"""
    try:
        import os
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except (IOError, OSError) as e:
        print(f"保存文件失败: {e}")
        return False


def get_current_date() -> MessageDate:
    """获取当前日期"""
    now = datetime.now()
    return MessageDate.from_datetime(now)


def get_week_start(date_obj: date) -> date:
    """获取周开始日期（周一）"""
    days_since_monday = date_obj.weekday()
    return date_obj - timedelta(days=days_since_monday)


def get_month_start(date_obj: date) -> date:
    """获取月开始日期"""
    return date_obj.replace(day=1)


def is_same_day(date1: date, date2: date) -> bool:
    """判断是否是同一天"""
    return date1 == date2


def is_same_week(date1: date, date2: date) -> bool:
    """判断是否是同一周"""
    return get_week_start(date1) == get_week_start(date2)


def is_same_month(date1: date, date2: date) -> bool:
    """判断是否是同一月"""
    return date1.year == date2.year and date1.month == date2.month


from datetime import timedelta