"""
数据模型定义
定义插件中使用的所有数据结构

此模块仅包含数据模型的定义，不包含业务逻辑或工具函数。
工具函数已拆分到独立的模块中：
- file_utils: 文件操作工具
- date_utils: 日期时间处理工具
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from enum import Enum



class RankType(Enum):
    """排行榜类型枚举
    
    定义了插件支持的排行榜类型，包括总榜、日榜、周榜和月榜。
    
    Attributes:
        TOTAL (str): 总排行榜，包含历史所有发言统计
        DAILY (str): 日排行榜，仅包含当日发言统计
        WEEKLY (str): 周排行榜，仅包含本周发言统计
        MONTHLY (str): 月排行榜，仅包含本月发言统计
        
    Example:
        >>> rank_type = RankType.TOTAL
        >>> print(rank_type.value)
        'total'
    """
    TOTAL = "total"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class MessageDate:
    """消息日期记录
    
    用于记录消息的日期信息，支持与标准date对象的相互转换。
    提供完整的比较和格式化功能。
    
    Attributes:
        year (int): 年份
        month (int): 月份
        day (int): 日期
        
    Methods:
        to_date(): 转换为标准date对象
        to_datetime(): 转换为标准datetime对象
        from_date(): 从date对象创建实例
        from_datetime(): 从datetime对象创建实例
        
    Example:
        >>> msg_date = MessageDate(2024, 1, 15)
        >>> print(msg_date.to_date())
        datetime.date(2024, 1, 15)
    """
    year: int
    month: int
    day: int
    
    def to_date(self) -> date:
        """转换为date对象
        
        将MessageDate实例转换为Python标准库中的date对象。
        
        Returns:
            date: 标准date对象，年月日信息相同
            
        Example:
            >>> msg_date = MessageDate(2024, 1, 15)
            >>> date_obj = msg_date.to_date()
            >>> print(type(date_obj))
            <class 'datetime.date'>
        """
        return date(self.year, self.month, self.day)
    
    def to_datetime(self) -> datetime:
        """转换为datetime对象
        
        将MessageDate实例转换为Python标准库中的datetime对象。
        时间部分将设置为00:00:00。
        
        Returns:
            datetime: 标准datetime对象，时间部分为00:00:00
            
        Example:
            >>> msg_date = MessageDate(2024, 1, 15)
            >>> dt = msg_date.to_datetime()
            >>> print(dt.time())
            00:00:00
        """
        return datetime.combine(self.to_date(), datetime.min.time())
    
    @classmethod
    def from_date(cls, date_obj: date):
        """从date对象创建
        
        从Python标准库中的date对象创建MessageDate实例。
        
        Args:
            date_obj (date): 标准date对象
            
        Returns:
            MessageDate: 对应的MessageDate实例
            
        Example:
            >>> from datetime import date
            >>> d = date(2024, 1, 15)
            >>> msg_date = MessageDate.from_date(d)
            >>> print(msg_date.year)
            2024
        """
        return cls(date_obj.year, date_obj.month, date_obj.day)
    
    @classmethod
    def from_datetime(cls, datetime_obj: datetime):
        """从datetime对象创建
        
        从Python标准库中的datetime对象创建MessageDate实例。
        只使用日期部分，忽略时间部分。
        
        Args:
            datetime_obj (datetime): 标准datetime对象
            
        Returns:
            MessageDate: 对应的MessageDate实例
            
        Example:
            >>> from datetime import datetime
            >>> dt = datetime(2024, 1, 15, 14, 30, 0)
            >>> msg_date = MessageDate.from_datetime(dt)
            >>> print(msg_date)
            2024-01-15
        """
        return cls.from_date(datetime_obj.date())
    
    def __str__(self) -> str:
        return f"{self.year}-{self.month:02d}-{self.day:02d}"
    
    def __eq__(self, other) -> bool:
        if not isinstance(other, MessageDate):
            return NotImplemented
        return (self.year == other.year and 
                self.month == other.month and 
                self.day == other.day)
    
    def __lt__(self, other) -> bool:
        if not isinstance(other, MessageDate):
            return NotImplemented
        return (self.year, self.month, self.day) < (other.year, other.month, other.day)


@dataclass
class UserData:
    """用户数据
    
    存储用户在群组中的发言统计数据，包括用户信息、发言总数和历史记录。
    支持数据序列化和反序列化，便于JSON存储。
    
    Attributes:
        user_id (str): 用户唯一标识符
        nickname (str): 用户昵称
        message_count (int): 总发言次数，默认为0
        history (List[MessageDate]): 发言日期历史记录列表
        last_date (Optional[str]): 最后发言日期的字符串表示
        first_message_time (Optional[int]): 首次发言时间戳
        last_message_time (Optional[int]): 最后发言时间戳
        
    Methods:
        add_message(): 添加新的消息记录
        get_last_message_date(): 获取最后发言日期
        get_message_count_in_period(): 获取指定时间段内的发言数量
        to_dict(): 转换为字典格式
        from_dict(): 从字典创建实例
        
    Example:
        >>> user = UserData("123456", "用户昵称")
        >>> user.add_message(MessageDate(2024, 1, 15))
        >>> print(user.message_count)
        1
    """
    user_id: str
    nickname: str
    message_count: int = 0
    history: List[MessageDate] = field(default_factory=list)
    last_date: Optional[str] = None
    first_message_time: Optional[int] = None
    last_message_time: Optional[int] = None
    
    def add_message(self, message_date: MessageDate):
        """添加消息记录
        
        增加用户的发言计数并记录发言日期。每次发言都会记录到历史中，
        保持message_count字段与实际发言次数的一致性。
        
        Args:
            message_date (MessageDate): 消息日期对象
            
        Returns:
            None: 无返回值，直接修改对象状态
            
        Example:
            >>> user = UserData("123", "用户")
            >>> user.add_message(MessageDate(2024, 1, 15))
            >>> print(user.message_count)
            1
        """
        self.message_count += 1
        
        # 每次发言都添加到历史记录中
        self.history.append(message_date)
        
        # 更新最后发言日期
        self.last_date = str(message_date)
    
    def get_last_message_date(self) -> Optional[MessageDate]:
        """获取最后消息日期
        
        返回用户最后一次发言的日期，如果无发言记录则返回None。
        
        Returns:
            Optional[MessageDate]: 最后发言日期，如果无记录则返回None
            
        Example:
            >>> user = UserData("123", "用户")
            >>> user.add_message(MessageDate(2024, 1, 15))
            >>> last_date = user.get_last_message_date()
            >>> print(last_date.year)
            2024
        """
        return self.history[-1] if self.history else None
    
    def get_message_count_in_period(self, start_date: date, end_date: date) -> int:
        """获取指定时间段内的消息数量
        
        计算用户在指定日期范围内的发言次数。
        
        Args:
            start_date (date): 开始日期（包含）
            end_date (date): 结束日期（包含）
            
        Returns:
            int: 指定时间段内的发言次数
            
        Example:
            >>> user = UserData("123", "用户")
            >>> user.add_message(MessageDate(2024, 1, 15))
            >>> user.add_message(MessageDate(2024, 1, 16))
            >>> count = user.get_message_count_in_period(date(2024, 1, 1), date(2024, 1, 31))
            >>> print(count)
            2
        """
        count = 0
        for hist_date in self.history:
            if start_date <= hist_date.to_date() <= end_date:
                count += 1
        return count
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典
        
        将UserData实例转换为字典格式，便于JSON序列化。
        
        Returns:
            Dict[str, Any]: 包含用户数据的字典，包括：
                - user_id: 用户ID
                - nickname: 用户昵称
                - message_count: 总发言次数
                - history: 发言日期历史（字符串列表）
                - last_date: 最后发言日期
                - first_message_time: 首次发言时间戳
                - last_message_time: 最后发言时间戳
                
        Example:
            >>> user = UserData("123", "用户")
            >>> data = user.to_dict()
            >>> print(data['nickname'])
            '用户'
        """
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "message_count": self.message_count,
            "history": [str(h) for h in self.history],
            "last_date": self.last_date,
            "first_message_time": self.first_message_time,
            "last_message_time": self.last_message_time
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserData':
        """从字典创建
        
        从字典数据创建UserData实例，自动重建发言历史记录。
        
        Args:
            data (Dict[str, Any]): 用户数据字典，必须包含user_id和nickname字段
            
        Returns:
            UserData: 对应的UserData实例
            
        Raises:
            KeyError: 当缺少必需字段时抛出
            ValueError: 当数据格式错误时抛出
            
        Example:
            >>> data = {"user_id": "123", "nickname": "用户", "message_count": 5}
            >>> user = UserData.from_dict(data)
            >>> print(user.user_id)
            '123'
        """
        user_data = cls(
            user_id=data["user_id"],
            nickname=data["nickname"],
            message_count=data.get("message_count", 0),
            last_date=data.get("last_date"),
            first_message_time=data.get("first_message_time"),
            last_message_time=data.get("last_message_time")
        )
        
        # 重建history
        if "history" in data:
            try:
                for hist_str in data["history"]:
                    try:
                        year, month, day = map(int, hist_str.split('-'))
                        user_data.history.append(MessageDate(year, month, day))
                    except (ValueError, IndexError) as e:
                        # 跳过格式错误的日期记录
                        continue
            except TypeError:
                # 如果history不是可迭代对象，跳过
                pass
        
        return user_data
    
    def __lt__(self, other) -> bool:
        """按总消息数排序"""
        if not isinstance(other, UserData):
            return NotImplemented
        return self.message_count < other.message_count  # 升序排列，用于sorted()函数


@dataclass
class PluginConfig:
    """插件配置
    
    存储插件的配置参数，包括显示设置和权限控制。
    支持数据序列化和反序列化，便于配置文件的读写。
    
    Attributes:
        is_admin_restricted (int): 是否限制管理员操作，0为不限制，1为限制
        rand (int): 排行榜显示人数，默认为20人
        send_pic (int): 是否发送图片，0为文字模式，1为图片模式
        
    Methods:
        to_dict(): 转换为字典格式
        from_dict(): 从字典创建实例
        
    Example:
        >>> config = PluginConfig()
        >>> config.rand = 15
        >>> config.send_pic = 1
    """
    is_admin_restricted: int = 0
    rand: int = 20
    send_pic: int = 1
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典
        
        将PluginConfig实例转换为字典格式，便于JSON序列化。
        
        Returns:
            Dict[str, Any]: 包含配置数据的字典，包括：
                - is_admin_restricted: 管理员限制设置
                - rand: 排行榜显示人数
                - send_pic: 图片模式设置
                
        Example:
            >>> config = PluginConfig()
            >>> data = config.to_dict()
            >>> print(data['rand'])
            20
        """
        return {
            "is_admin_restricted": self.is_admin_restricted,
            "rand": self.rand,
            "send_pic": self.send_pic
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PluginConfig':
        """从字典创建
        
        从字典数据创建PluginConfig实例，使用默认值填充缺失字段。
        
        Args:
            data (Dict[str, Any]): 配置数据字典
            
        Returns:
            PluginConfig: 对应的PluginConfig实例
            
        Example:
            >>> data = {"rand": 15, "send_pic": 0}
            >>> config = PluginConfig.from_dict(data)
            >>> print(config.rand)
            15
        """
        return cls(
            is_admin_restricted=data.get("is_admin_restricted", 0),
            rand=data.get("rand", 20),
            send_pic=data.get("send_pic", 1)
        )


@dataclass
class GroupInfo:
    """群组信息
    
    存储群组的基本信息，包括群ID、群名称和成员数量。
    用于排行榜显示和群组识别。
    
    Attributes:
        group_id (str): 群组唯一标识符
        group_name (str): 群组名称，默认为空字符串
        member_count (int): 群组成员数量，默认为0
        
    Methods:
        to_dict(): 转换为字典格式
        
    Example:
        >>> group = GroupInfo("123456789", "测试群", 50)
        >>> print(group.group_name)
        '测试群'
    """
    group_id: str
    group_name: str = ""
    member_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典
        
        将GroupInfo实例转换为字典格式，便于JSON序列化。
        
        Returns:
            Dict[str, Any]: 包含群组信息的字典，包括：
                - group_id: 群组ID
                - group_name: 群组名称
                - member_count: 成员数量
                
        Example:
            >>> group = GroupInfo("123", "测试群")
            >>> data = group.to_dict()
            >>> print(data['group_name'])
            '测试群'
        """
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "member_count": self.member_count
        }


@dataclass
class RankData:
    """排行榜数据
    
    存储完整的排行榜信息，包括群组信息、标题、用户数据和统计信息。
    用于排行榜的生成和显示。
    
    Attributes:
        group_info (GroupInfo): 群组信息对象
        title (str): 排行榜标题
        users (List[UserData]): 用户数据列表
        total_messages (int): 总消息数
        generated_at (datetime): 生成时间，默认为当前时间
        
    Methods:
        to_dict(): 转换为字典格式
        
    Example:
        >>> group_info = GroupInfo("123", "测试群")
        >>> rank_data = RankData(group_info, "排行榜", [], 100)
        >>> print(rank_data.title)
        '排行榜'
    """
    group_info: GroupInfo
    title: str
    users: List[UserData]
    total_messages: int
    generated_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典
        
        将RankData实例转换为字典格式，便于JSON序列化。
        
        Returns:
            Dict[str, Any]: 包含排行榜数据的字典，包括：
                - group_info: 群组信息字典
                - title: 排行榜标题
                - users: 用户数据字典列表
                - total_messages: 总消息数
                - generated_at: 生成时间（ISO格式字符串）
                
        Example:
            >>> rank_data = RankData(group_info, "排行榜", [], 100)
            >>> data = rank_data.to_dict()
            >>> print(data['title'])
            '排行榜'
        """
        return {
            "group_info": self.group_info.to_dict(),
            "title": self.title,
            "users": [user.to_dict() for user in self.users],
            "total_messages": self.total_messages,
            "generated_at": self.generated_at.isoformat()
        }