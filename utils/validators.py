"""
数据验证模块
负责验证输入参数和数据格式
"""

import re
import os
from typing import Any, Optional, List, Dict, Callable
from datetime import datetime, date
from astrbot.api import logger as astrbot_logger
import html
from functools import wraps

try:
    import bleach
except ImportError:
    bleach = None




class ValidationError(Exception):
    """验证异常
    
    当数据验证失败时抛出的自定义异常。
    
    Attributes:
        message (str): 错误描述信息
        
    Example:
        >>> try:
        ...     Validators.validate_group_id("")
        ... except ValidationError as e:
        ...     print(f"验证失败: {e}")
    """
    pass


class Validators:
    """数据验证器集合
    
    提供各种数据验证功能，包括群组ID、用户ID、昵称、
    时间格式、配置参数等的验证。
    
    所有方法都是静态方法，可以直接通过类名调用。
    
    Example:
        >>> group_id = Validators.validate_group_id("123456789")
        >>> user_id = Validators.validate_user_id("987654321")
        >>> nickname = Validators.validate_nickname("用户昵称")
    """
    
    logger = astrbot_logger
    
    @staticmethod
    def validate_group_id(group_id: Any) -> str:
        """验证群组ID格式
        
        验证群组ID是否符合规范，必须是5-12位数字。
        
        Args:
            group_id (Any): 要验证的群组ID，可以是任何类型
            
        Returns:
            str: 验证通过后的群组ID字符串
            
        Raises:
            ValidationError: 当群组ID为空、非数字或长度不符合要求时抛出
            
        Example:
            >>> Validators.validate_group_id("123456789")
            '123456789'
            >>> Validators.validate_group_id(123456789)
            '123456789'
            >>> Validators.validate_group_id("")  # 抛出异常
        """
        if not group_id:
            raise ValidationError("群组ID不能为空")
        
        group_id_str = str(group_id)
        
        # 检查是否为数字
        if not group_id_str.isdigit():
            raise ValidationError("群组ID必须是数字")
        
        # 检查长度
        if len(group_id_str) < 5 or len(group_id_str) > 12:
            raise ValidationError("群组ID长度应在5-12位之间")
        
        return group_id_str
    
    @staticmethod
    def validate_user_id(user_id: Any) -> str:
        """验证用户ID格式
        
        验证用户ID是否符合规范，必须是1-20位数字。
        
        Args:
            user_id (Any): 要验证的用户ID，可以是任何类型
            
        Returns:
            str: 验证通过后的用户ID字符串
            
        Raises:
            ValidationError: 当用户ID为空、非数字或长度不符合要求时抛出
            
        Example:
            >>> Validators.validate_user_id("987654321")
            '987654321'
            >>> Validators.validate_user_id(987654321)
            '987654321'
            >>> Validators.validate_user_id("abc")  # 抛出异常
        """
        if not user_id:
            raise ValidationError("用户ID不能为空")
        
        user_id_str = str(user_id)
        
        # 检查是否为数字
        if not user_id_str.isdigit():
            raise ValidationError("用户ID必须是数字")
        
        # 检查长度 - 放宽限制，支持各种长度的用户ID
        if len(user_id_str) < 1 or len(user_id_str) > 20:
            raise ValidationError("用户ID长度应在1-20位之间")
        
        return user_id_str
    
    @staticmethod
    def validate_nickname(nickname: Any) -> str:
        """验证用户昵称格式
        
        验证用户昵称是否符合规范，进行长度检查和HTML转义。
        
        Args:
            nickname (Any): 要验证的昵称，可以是任何类型
            
        Returns:
            str: 验证并转义后的昵称字符串
            
        Raises:
            ValidationError: 当昵称为空、长度超过50字符或包含可疑编码时抛出
            
        Example:
            >>> Validators.validate_nickname("用户昵称")
            '用户昵称'
            >>> Validators.validate_nickname("<script>alert('xss')</script>")
            '&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;'
            >>> Validators.validate_nickname("")  # 抛出异常
        """
        if not nickname:
            raise ValidationError("昵称不能为空")
        
        nickname_str = str(nickname).strip()
        
        if len(nickname_str) == 0:
            raise ValidationError("昵称不能为空")
        
        if len(nickname_str) > 50:
            raise ValidationError("昵称长度不能超过50个字符")
        
        # 使用HTML转义
        return html.escape(nickname_str)
    
    @staticmethod
    def validate_time_format(time_str: str) -> str:
        """验证时间格式
        
        验证时间是否符合HH:MM格式（24小时制）。
        
        Args:
            time_str (str): 要验证的时间字符串
            
        Returns:
            str: 验证通过的时间字符串
            
        Raises:
            ValidationError: 当时间格式错误或为空时抛出
            
        Example:
            >>> Validators.validate_time_format("08:30")
            '08:30'
            >>> Validators.validate_time_format("23:59")
            '23:59'
            >>> Validators.validate_time_format("8:30")  # 抛出异常
        """
        if not time_str:
            raise ValidationError("时间不能为空")
        
        # 匹配 HH:MM 格式
        time_pattern = r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'
        
        if not re.match(time_pattern, time_str):
            raise ValidationError("时间格式错误，请使用 HH:MM 格式（如：08:00）")
        
        return time_str
    
    @staticmethod
    def validate_report_type(report_type: str) -> str:
        """验证报告类型
        
        验证报告类型是否为支持的类型（daily、weekly、monthly）。
        
        Args:
            report_type (str): 要验证的报告类型
            
        Returns:
            str: 验证通过的小写报告类型字符串
            
        Raises:
            ValidationError: 当报告类型不支持或为空时抛出
            
        Example:
            >>> Validators.validate_report_type("daily")
            'daily'
            >>> Validators.validate_report_type("WEEKLY")
            'weekly'
            >>> Validators.validate_report_type("invalid")  # 抛出异常
        """
        valid_types = ['daily', 'weekly', 'monthly']
        
        if not report_type:
            raise ValidationError("报告类型不能为空")
        
        report_type = report_type.lower().strip()
        
        if report_type not in valid_types:
            raise ValidationError(f"报告类型错误，可选值：{', '.join(valid_types)}")
        
        return report_type
    

    
    @staticmethod
    def validate_image_mode(mode: Any) -> int:
        """验证图片模式
        
        验证图片模式参数，支持多种输入格式：
        - 数字：0（文字模式）、1（图片模式）
        - 字符串：'false'/'true'、'否'/'是'、'关'/'开'、'文字'/'图片'等
        
        Args:
            mode (Any): 要验证的图片模式参数
            
        Returns:
            int: 验证通过后的模式值（0或1）
            
        Raises:
            ValidationError: 当模式参数不支持时抛出
            
        Example:
            >>> Validators.validate_image_mode(1)
            1
            >>> Validators.validate_image_mode("图片")
            1
            >>> Validators.validate_image_mode("关闭")
            0
            >>> Validators.validate_image_mode("invalid")  # 抛出异常
        """
        if mode is None:
            return 1  # 默认图片模式
        
        # 转换为字符串并清理
        mode_str = str(mode).lower().strip()
        
        # 数字模式
        if mode_str.isdigit():
            mode_int = int(mode_str)
            if mode_int not in [0, 1]:
                raise ValidationError("图片模式必须是 0（文字）或 1（图片）")
            return mode_int
        
        # 文字模式
        text_modes = ['0', 'false', '否', '关', '关闭', '文字', 'text']
        if mode_str in text_modes:
            return 0
        
        # 图片模式
        image_modes = ['1', 'true', '是', '开', '开启', '图片', 'image', '图片模式']
        if mode_str in image_modes:
            return 1
        
        raise ValidationError("图片模式参数错误，请使用 0（文字）或 1（图片）")
    
    @staticmethod
    def validate_rank_limit(limit: Any) -> int:
        """验证排行榜显示人数
        
        验证排行榜显示人数参数，范围在5-50之间。
        
        Args:
            limit (Any): 要验证的显示人数，可以是任何类型
            
        Returns:
            int: 验证通过后的显示人数
            
        Raises:
            ValidationError: 当显示人数不是数字或超出范围时抛出
            
        Example:
            >>> Validators.validate_rank_limit(20)
            20
            >>> Validators.validate_rank_limit("15")
            15
            >>> Validators.validate_rank_limit(3)  # 抛出异常
            >>> Validators.validate_rank_limit(100)  # 抛出异常
        """
        if limit is None:
            return 20  # 默认值
        
        try:
            limit_int = int(limit)
        except (ValueError, TypeError):
            raise ValidationError("显示人数必须是数字")
        
        if limit_int < 5:
            raise ValidationError("显示人数不能少于5人")
        
        if limit_int > 50:
            raise ValidationError("显示人数不能超过50人")
        
        return limit_int
    
    @staticmethod
    def validate_message_content(message: Any) -> str:
        """验证消息内容
        
        验证消息内容是否符合规范，长度不超过200字符。
        
        Args:
            message (Any): 要验证的消息内容，可以是任何类型
            
        Returns:
            str: 验证通过后的消息内容字符串
            
        Raises:
            ValidationError: 当消息内容为空或超过长度限制时抛出
            
        Example:
            >>> Validators.validate_message_content("这是一条消息")
            '这是一条消息'
            >>> Validators.validate_message_content("")  # 抛出异常
        """
        if not message:
            raise ValidationError("消息内容不能为空")
        
        message_str = str(message).strip()
        
        if len(message_str) == 0:
            raise ValidationError("消息内容不能为空")
        
        if len(message_str) > 200:
            raise ValidationError("消息内容不能超过200个字符")
        
        return message_str
    
    @staticmethod
    def validate_date_string(date_str: str) -> str:
        """验证日期字符串格式
        
        验证日期是否符合YYYY-MM-DD格式。
        
        Args:
            date_str (str): 要验证的日期字符串
            
        Returns:
            str: 验证通过后的日期字符串
            
        Raises:
            ValidationError: 当日期格式错误或为空时抛出
            
        Example:
            >>> Validators.validate_date_string("2024-01-15")
            '2024-01-15'
            >>> Validators.validate_date_string("2024-13-01")  # 抛出异常
        """
        if not date_str:
            raise ValidationError("日期字符串不能为空")
        
        try:
            # 尝试解析日期
            datetime.strptime(date_str, "%Y-%m-%d")
            return date_str
        except ValueError:
            raise ValidationError("日期格式错误，请使用 YYYY-MM-DD 格式")
    
    @staticmethod
    def validate_config_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
        """验证配置更新
        
        验证配置更新字典中的各个配置项。
        
        Args:
            updates (Dict[str, Any]): 要验证的配置更新字典
            
        Returns:
            Dict[str, Any]: 验证通过后的配置更新字典
            
        Raises:
            ValidationError: 当配置格式错误或包含无效配置项时抛出
            
        Example:
            >>> Validators.validate_config_updates({"rand": 10, "if_send_pic": 1})
            {'rand': 10, 'if_send_pic': 1}
            >>> Validators.validate_config_updates("invalid")  # 抛出异常
        """
        if not isinstance(updates, dict):
            raise ValidationError("配置更新必须是字典格式")
        
        validated_updates = {}
        
        # 验证各个配置项
        for key, value in updates.items():
            try:
                if key == "rand":
                    validated_updates[key] = Validators.validate_rank_limit(value)
                elif key == "if_send_pic":
                    validated_updates[key] = Validators.validate_image_mode(value)

                else:
                    # 未知配置项，记录警告但继续处理
                    validated_updates[key] = value
            
            except ValidationError as e:
                raise ValidationError(f"配置项 {key} 验证失败: {e}")
        
        return validated_updates
    

    
    @staticmethod
    def validate_command_args(args: List[str], expected_count: Optional[int] = None) -> List[str]:
        """验证命令参数
        
        验证命令参数列表，可选检查参数数量。
        
        Args:
            args (List[str]): 要验证的命令参数列表
            expected_count (Optional[int]): 期望的参数数量，None表示不检查数量
            
        Returns:
            List[str]: 验证并清理后的参数列表
            
        Raises:
            ValidationError: 当参数数量不符合期望时抛出
            
        Example:
            >>> Validators.validate_command_args(["arg1", "arg2"], 2)
            ['arg1', 'arg2']
            >>> Validators.validate_command_args(["arg1", " arg2 "])
            ['arg1', 'arg2']
        """
        if expected_count is not None and len(args) != expected_count:
            raise ValidationError(f"参数数量错误，期望 {expected_count} 个，实际 {len(args)} 个")
        
        validated_args = []
        for arg in args:
            validated_args.append(str(arg).strip())
        
        return validated_args
    
    @staticmethod
    def sanitize_html_content(content: str) -> str:
        """清理HTML内容
        
        使用bleach库清理HTML内容，移除危险标签和属性。
        如果bleach库未安装，则回退到使用html.escape进行基础HTML转义。
        
        Args:
            content (str): 要清理的HTML内容
            
        Returns:
            str: 清理后的安全HTML内容
            
        Example:
            >>> Validators.sanitize_html_content('<script>alert("xss")</script><p>Hello</p>')
            '<p>Hello</p>'
        """
        if not content:
            return ""
        
        if bleach is None:
            # 记录警告日志
            Validators.logger.warning("bleach库未安装，使用基础HTML转义作为备选方案")
            # 回退到基础HTML转义
            return html.escape(content)
        
        # 定义允许的标签和属性
        allowed_tags = ['p', 'br', 'strong', 'em', 'u', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 
                      'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'span', 'div']
        allowed_attributes = {
            '*': ['class'],
            'a': ['href', 'title'],
            'img': ['src', 'alt', 'title']
        }
        allowed_protocols = ['http', 'https', 'mailto']
        
        # 清理HTML内容
        cleaned_content = bleach.clean(
            content,
            tags=allowed_tags,
            attributes=allowed_attributes,
            protocols=allowed_protocols,
            strip=True
        )
        
        return cleaned_content.strip()
    

    
    @staticmethod
    def _normalize_path(file_path: str) -> str:
        """规范化文件路径"""
        normalized_path = os.path.abspath(file_path)
        return os.path.realpath(normalized_path)
    
    @staticmethod
    def _check_path_security(path: str) -> None:
        """检查路径安全性"""
        # 检查路径遍历攻击
        if '..' in path.split(os.sep):
            raise ValidationError("文件路径包含路径遍历攻击")
    
    @staticmethod
    def _validate_path_length(path: str) -> None:
        """验证路径长度"""
        if len(path) > 500:
            raise ValidationError("文件路径过长")
    
    @staticmethod
    def _check_dangerous_chars(path: str) -> None:
        """检查危险字符"""
        dangerous_chars = ['<', '>', ':', '"', '|', '?', '*', '\x00', '\x0a', '\x0d']
        for char in dangerous_chars:
            if char in path:
                raise ValidationError(f"文件路径包含危险字符: {char}")
    
    @staticmethod
    def _validate_base_path(path: str, allowed_base_path: Optional[str]) -> None:
        """验证基础路径限制"""
        if not allowed_base_path:
            return
        
        try:
            allowed_abs_path = os.path.abspath(allowed_base_path)
            allowed_real_path = os.path.realpath(allowed_abs_path)
            
            if not path.startswith(allowed_real_path + os.sep) and path != allowed_real_path:
                raise ValidationError("文件路径超出允许的目录范围")
        except (OSError, ValueError):
            raise ValidationError("允许的基础路径无效")
    
    @staticmethod
    def _validate_extensions(path: str, allowed_extensions: Optional[List[str]]) -> None:
        """验证文件扩展名"""
        if not allowed_extensions:
            return
        
        _, ext = os.path.splitext(path.lower())
        if ext not in [ext.lower() for ext in allowed_extensions]:
            raise ValidationError(f"文件类型不支持，允许的类型: {', '.join(allowed_extensions)}")
    
    @staticmethod
    def validate_file_path(file_path: str, allowed_extensions: Optional[List[str]] = None, 
                          allowed_base_path: Optional[str] = None) -> str:
        """验证文件路径（简化版）
        
        验证文件路径的安全性，防止路径遍历攻击。
        支持可选的扩展名白名单和基础路径限制。
        
        Args:
            file_path (str): 要验证的文件路径
            allowed_extensions (Optional[List[str]]): 允许的文件扩展名列表
            allowed_base_path (Optional[str]): 允许的基础路径
            
        Returns:
            str: 验证通过后的规范化文件路径
            
        Raises:
            ValidationError: 当文件路径无效、包含危险字符或超出允许范围时抛出
            
        Example:
            >>> Validators.validate_file_path("/safe/path/file.txt", [".txt", ".json"])
            '/safe/path/file.txt'
            >>> Validators.validate_file_path("../../../etc/passwd")  # 抛出异常
        """
        if not file_path:
            raise ValidationError("文件路径不能为空")
        
        try:
            # 路径规范化
            final_path = Validators._normalize_path(file_path)
        except (OSError, ValueError) as e:
            raise ValidationError(f"文件路径无效: {e}")
        
        # 各项安全检查
        Validators._check_path_security(final_path)
        Validators._validate_path_length(final_path)
        Validators._check_dangerous_chars(final_path)
        Validators._validate_base_path(final_path, allowed_base_path)
        Validators._validate_extensions(final_path, allowed_extensions)
        
        return final_path
    
    @staticmethod
    def validate_json_data(data: Any, required_fields: Optional[List[str]] = None) -> Dict[str, Any]:
        """验证JSON数据"""
        if not isinstance(data, dict):
            raise ValidationError("数据必须是字典格式")
        
        if required_fields:
            for field in required_fields:
                if field not in data:
                    raise ValidationError(f"缺少必需字段: {field}")
        
        return data
    
    @staticmethod
    def validate_url(url: str) -> str:
        """验证URL格式
        
        验证URL是否符合基本格式要求。
        
        Args:
            url (str): 要验证的URL字符串
            
        Returns:
            str: 验证通过后的URL字符串
            
        Raises:
            ValidationError: 当URL格式错误或为空时抛出
            
        Example:
            >>> Validators.validate_url("https://example.com")
            'https://example.com'
            >>> Validators.validate_url("http://test.org/path")
            'http://test.org/path'
            >>> Validators.validate_url("invalid-url")  # 抛出异常
        """
        if not url:
            raise ValidationError("URL不能为空")
        
        url_pattern = r'^https?://[^\s/$.?#].[^\s]*$'
        if not re.match(url_pattern, url):
            raise ValidationError("URL格式错误")
        
        return url
    
    @staticmethod
    def validate_phone_number(phone: str) -> str:
        """验证手机号码
        
        验证中国手机号码格式（1[3-9]开头的11位数字）。
        
        Args:
            phone (str): 要验证的手机号码
            
        Returns:
            str: 验证通过后的手机号码字符串
            
        Raises:
            ValidationError: 当手机号码格式错误或为空时抛出
            
        Example:
            >>> Validators.validate_phone_number("13812345678")
            '13812345678'
            >>> Validators.validate_phone_number("15987654321")
            '15987654321'
            >>> Validators.validate_phone_number("123456789")  # 抛出异常
        """
        if not phone:
            raise ValidationError("手机号码不能为空")
        
        phone_pattern = r'^1[3-9]\d{9}$'
        if not re.match(phone_pattern, phone):
            raise ValidationError("手机号码格式错误")
        
        return phone
    
    @staticmethod
    def validate_email(email: str) -> str:
        """验证邮箱地址
        
        验证邮箱地址是否符合基本格式要求。
        
        Args:
            email (str): 要验证的邮箱地址
            
        Returns:
            str: 验证通过后的小写邮箱地址
            
        Raises:
            ValidationError: 当邮箱地址格式错误或为空时抛出
            
        Example:
            >>> Validators.validate_email("user@example.com")
            'user@example.com'
            >>> Validators.validate_email("TEST@DOMAIN.ORG")
            'test@domain.org'
            >>> Validators.validate_email("invalid-email")  # 抛出异常
        """
        if not email:
            raise ValidationError("邮箱地址不能为空")
        
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            raise ValidationError("邮箱地址格式错误")
        
        return email.lower()
    
    @staticmethod
    def validate_range(value: Any, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
        """验证数值范围
        
        验证数值是否在指定范围内。
        
        Args:
            value (Any): 要验证的数值，可以是任何类型
            min_val (Optional[float]): 最小值，None表示不限制
            max_val (Optional[float]): 最大值，None表示不限制
            
        Returns:
            float: 验证通过后的数值
            
        Raises:
            ValidationError: 当值不是数字或超出范围时抛出
            
        Example:
            >>> Validators.validate_range(5, 1, 10)
            5.0
            >>> Validators.validate_range("3.5", 0, 10)
            3.5
            >>> Validators.validate_range(15, 1, 10)  # 抛出异常
        """
        try:
            num_value = float(value)
        except (ValueError, TypeError):
            raise ValidationError("值必须是数字")
        
        if min_val is not None and num_value < min_val:
            raise ValidationError(f"值不能小于 {min_val}")
        
        if max_val is not None and num_value > max_val:
            raise ValidationError(f"值不能大于 {max_val}")
        
        return num_value
    
    @staticmethod
    def validate_choice(value: Any, choices: List[Any]) -> Any:
        """验证选择项
        
        验证值是否在允许的选择列表中。
        
        Args:
            value (Any): 要验证的值
            choices (List[Any]): 允许的选择列表
            
        Returns:
            Any: 验证通过后的值
            
        Raises:
            ValidationError: 当值不在允许的选择列表中时抛出
            
        Example:
            >>> Validators.validate_choice("red", ["red", "green", "blue"])
            'red'
            >>> Validators.validate_choice(1, [1, 2, 3])
            1
            >>> Validators.validate_choice("yellow", ["red", "green"])  # 抛出异常
        """
        if value not in choices:
            raise ValidationError(f"值必须是以下选项之一: {', '.join(str(c) for c in choices)}")
        
        return value
    
    @staticmethod
    def validate_length(value: Any, min_len: Optional[int] = None, max_len: Optional[int] = None) -> str:
        """验证字符串长度
        
        验证字符串长度是否在指定范围内。
        
        Args:
            value (Any): 要验证的值，会转换为字符串
            min_len (Optional[int]): 最小长度，None表示不限制
            max_len (Optional[int]): 最大长度，None表示不限制
            
        Returns:
            str: 验证通过后的字符串
            
        Raises:
            ValidationError: 当字符串长度超出范围时抛出
            
        Example:
            >>> Validators.validate_length("hello", 3, 10)
            'hello'
            >>> Validators.validate_length(123, 1, 5)
            '123'
            >>> Validators.validate_length("toolong", 3, 5)  # 抛出异常
        """
        str_value = str(value)
        
        if min_len is not None and len(str_value) < min_len:
            raise ValidationError(f"长度不能少于 {min_len} 个字符")
        
        if max_len is not None and len(str_value) > max_len:
            raise ValidationError(f"长度不能超过 {max_len} 个字符")
        
        return str_value


class CommandValidator:
    """命令验证器
    
    提供专门针对命令参数的验证功能，简化命令处理逻辑。
    每个方法都对应一个特定的命令类型验证。
    
    Example:
        >>> time_str = CommandValidator.validate_set_time_command(["08:30"])
        >>> report_type = CommandValidator.validate_set_type_command(["daily"])
        >>> image_mode = CommandValidator.validate_set_image_mode_command(["1"])
    """
    
    @staticmethod
    def validate_set_time_command(args: List[str]) -> str:
        """验证设置时间命令
        
        验证设置时间命令的参数，确保只有一个时间参数且格式正确。
        
        Args:
            args (List[str]): 命令参数列表
            
        Returns:
            str: 验证通过后的时间字符串
            
        Raises:
            ValidationError: 当参数数量不正确或时间格式错误时抛出
            
        Example:
            >>> CommandValidator.validate_set_time_command(["08:30"])
            '08:30'
            >>> CommandValidator.validate_set_time_command(["25:00"])  # 抛出异常
        """
        if len(args) != 1:
            raise ValidationError("设置时间命令需要一个参数")
        
        return Validators.validate_time_format(args[0])
    
    @staticmethod
    def validate_set_type_command(args: List[str]) -> str:
        """验证设置类型命令
        
        验证设置类型命令的参数，确保只有一个类型参数且类型有效。
        
        Args:
            args (List[str]): 命令参数列表
            
        Returns:
            str: 验证通过后的报告类型（小写）
            
        Raises:
            ValidationError: 当参数数量不正确或报告类型无效时抛出
            
        Example:
            >>> CommandValidator.validate_set_type_command(["daily"])
            'daily'
            >>> CommandValidator.validate_set_type_command(["WEEKLY"])
            'weekly'
            >>> CommandValidator.validate_set_type_command(["invalid"])  # 抛出异常
        """
        if len(args) != 1:
            raise ValidationError("设置类型命令需要一个参数")
        
        return Validators.validate_report_type(args[0])
    

    
    @staticmethod
    def validate_set_image_mode_command(args: List[str]) -> int:
        """验证设置图片模式命令
        
        验证设置图片模式命令的参数，确保只有一个模式参数且有效。
        
        Args:
            args (List[str]): 命令参数列表
            
        Returns:
            int: 验证通过后的模式值（0或1）
            
        Raises:
            ValidationError: 当参数数量不正确或模式参数无效时抛出
            
        Example:
            >>> CommandValidator.validate_set_image_mode_command(["1"])
            1
            >>> CommandValidator.validate_set_image_mode_command(["图片"])
            1
            >>> CommandValidator.validate_set_image_mode_command(["invalid"])  # 抛出异常
        """
        if len(args) != 1:
            raise ValidationError("设置图片模式命令需要一个参数")
        
        return Validators.validate_image_mode(args[0])
    
    @staticmethod
    def validate_set_message_command(args: List[str]) -> str:
        """验证设置消息命令
        
        验证设置消息命令的参数，将所有参数合并为一条消息并验证。
        
        Args:
            args (List[str]): 命令参数列表
            
        Returns:
            str: 验证通过后的消息内容
            
        Raises:
            ValidationError: 当参数为空或消息内容过长时抛出
            
        Example:
            >>> CommandValidator.validate_set_message_command(["Hello", "World"])
            'Hello World'
            >>> CommandValidator.validate_set_message_command([])  # 抛出异常
        """
        if len(args) < 1:
            raise ValidationError("设置消息命令至少需要一个参数")
        
        return Validators.validate_message_content(' '.join(args))
    
    @staticmethod
    def validate_set_rank_limit_command(args: List[str]) -> int:
        """验证设置排行榜人数命令
        
        验证设置排行榜人数命令的参数，确保只有一个人数参数且在有效范围内。
        
        Args:
            args (List[str]): 命令参数列表
            
        Returns:
            int: 验证通过后的人数（5-50之间）
            
        Raises:
            ValidationError: 当参数数量不正确或人数超出范围时抛出
            
        Example:
            >>> CommandValidator.validate_set_rank_limit_command(["20"])
            20
            >>> CommandValidator.validate_set_rank_limit_command(["10"])
            10
            >>> CommandValidator.validate_set_rank_limit_command(["3"])  # 抛出异常
        """
        if len(args) != 1:
            raise ValidationError("设置排行榜人数命令需要一个参数")
        
        return Validators.validate_rank_limit(args[0])
