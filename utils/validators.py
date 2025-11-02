"""
数据验证模块
负责验证输入参数和数据格式
"""

import re
import logging
from typing import Any, Optional, List, Dict
from datetime import datetime, date

logger = logging.getLogger('message_stats_plugin')


class ValidationError(Exception):
    """验证异常"""
    pass


class Validators:
    """验证器集合"""
    
    @staticmethod
    def validate_group_id(group_id: Any) -> str:
        """验证群组ID"""
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
        """验证用户ID"""
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
        """验证昵称"""
        if not nickname:
            raise ValidationError("昵称不能为空")
        
        nickname_str = str(nickname).strip()
        
        if len(nickname_str) == 0:
            raise ValidationError("昵称不能为空")
        
        if len(nickname_str) > 50:
            raise ValidationError("昵称长度不能超过50个字符")
        
        # 检查是否包含危险字符
        dangerous_chars = ['<', '>', '&', '"', "'", '\\', '/']
        for char in dangerous_chars:
            if char in nickname_str:
                raise ValidationError(f"昵称不能包含特殊字符: {char}")
        
        return nickname_str
    
    @staticmethod
    def validate_time_format(time_str: str) -> str:
        """验证时间格式"""
        if not time_str:
            raise ValidationError("时间不能为空")
        
        # 匹配 HH:MM 格式
        time_pattern = r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$'
        
        if not re.match(time_pattern, time_str):
            raise ValidationError("时间格式错误，请使用 HH:MM 格式（如：08:00）")
        
        return time_str
    
    @staticmethod
    def validate_report_type(report_type: str) -> str:
        """验证报告类型"""
        valid_types = ['daily', 'weekly', 'monthly']
        
        if not report_type:
            raise ValidationError("报告类型不能为空")
        
        report_type = report_type.lower().strip()
        
        if report_type not in valid_types:
            raise ValidationError(f"报告类型错误，可选值：{', '.join(valid_types)}")
        
        return report_type
    

    
    @staticmethod
    def validate_image_mode(mode: Any) -> int:
        """验证图片模式"""
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
        """验证排行榜显示人数"""
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
        """验证消息内容"""
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
        """验证日期字符串格式"""
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
        """验证配置更新"""
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
                    logger.warning(f"未知配置项: {key}")
                    validated_updates[key] = value
            
            except ValidationError as e:
                raise ValidationError(f"配置项 {key} 验证失败: {e}")
        
        return validated_updates
    

    
    @staticmethod
    def validate_command_args(args: List[str], expected_count: Optional[int] = None) -> List[str]:
        """验证命令参数"""
        if expected_count is not None and len(args) != expected_count:
            raise ValidationError(f"参数数量错误，期望 {expected_count} 个，实际 {len(args)} 个")
        
        validated_args = []
        for arg in args:
            validated_args.append(str(arg).strip())
        
        return validated_args
    
    @staticmethod
    def sanitize_html_content(content: str) -> str:
        """清理HTML内容"""
        if not content:
            return ""
        
        # 移除危险标签
        dangerous_tags = ['script', 'object', 'embed', 'link', 'style', 'iframe', 'frame', 'frameset']
        
        for tag in dangerous_tags:
            pattern = f'<{tag}[^>]*>.*?</{tag}>'
            content = re.sub(pattern, '', content, flags=re.IGNORECASE | re.DOTALL)
        
        # 移除危险属性
        dangerous_attrs = ['onclick', 'ondblclick', 'onmousedown', 'onmouseup', 'onmouseover', 
                          'onmousemove', 'onmouseout', 'onkeypress', 'onkeydown', 'onkeyup',
                          'onload', 'onunload', 'onfocus', 'onblur', 'onchange', 'onsubmit']
        
        for attr in dangerous_attrs:
            pattern = f'{attr}\\s*=\\s*["\'][^"\']*["\']'
            content = re.sub(pattern, '', content, flags=re.IGNORECASE)
        
        return content.strip()
    
    @staticmethod
    def validate_file_path(file_path: str, allowed_extensions: Optional[List[str]] = None) -> str:
        """验证文件路径"""
        if not file_path:
            raise ValidationError("文件路径不能为空")
        
        file_path = file_path.strip()
        
        # 检查路径长度
        if len(file_path) > 500:
            raise ValidationError("文件路径过长")
        
        # 检查危险字符
        dangerous_chars = ['..', '<', '>', ':', '"', '|', '?', '*']
        for char in dangerous_chars:
            if char in file_path:
                raise ValidationError(f"文件路径包含危险字符: {char}")
        
        # 检查扩展名
        if allowed_extensions:
            import os
            _, ext = os.path.splitext(file_path.lower())
            if ext not in [ext.lower() for ext in allowed_extensions]:
                raise ValidationError(f"文件类型不支持，允许的类型: {', '.join(allowed_extensions)}")
        
        return file_path
    
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
        """验证URL格式"""
        if not url:
            raise ValidationError("URL不能为空")
        
        url_pattern = r'^https?://[^\s/$.?#].[^\s]*$'
        if not re.match(url_pattern, url):
            raise ValidationError("URL格式错误")
        
        return url
    
    @staticmethod
    def validate_phone_number(phone: str) -> str:
        """验证手机号码"""
        if not phone:
            raise ValidationError("手机号码不能为空")
        
        phone_pattern = r'^1[3-9]\d{9}$'
        if not re.match(phone_pattern, phone):
            raise ValidationError("手机号码格式错误")
        
        return phone
    
    @staticmethod
    def validate_email(email: str) -> str:
        """验证邮箱地址"""
        if not email:
            raise ValidationError("邮箱地址不能为空")
        
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            raise ValidationError("邮箱地址格式错误")
        
        return email.lower()
    
    @staticmethod
    def validate_range(value: Any, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
        """验证数值范围"""
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
        """验证选择项"""
        if value not in choices:
            raise ValidationError(f"值必须是以下选项之一: {', '.join(str(c) for c in choices)}")
        
        return value
    
    @staticmethod
    def validate_length(value: Any, min_len: Optional[int] = None, max_len: Optional[int] = None) -> str:
        """验证字符串长度"""
        str_value = str(value)
        
        if min_len is not None and len(str_value) < min_len:
            raise ValidationError(f"长度不能少于 {min_len} 个字符")
        
        if max_len is not None and len(str_value) > max_len:
            raise ValidationError(f"长度不能超过 {max_len} 个字符")
        
        return str_value


class CommandValidator:
    """命令验证器"""
    
    @staticmethod
    def validate_set_time_command(args: List[str]) -> str:
        """验证设置时间命令"""
        if len(args) != 1:
            raise ValidationError("设置时间命令需要一个参数")
        
        return Validators.validate_time_format(args[0])
    
    @staticmethod
    def validate_set_type_command(args: List[str]) -> str:
        """验证设置类型命令"""
        if len(args) != 1:
            raise ValidationError("设置类型命令需要一个参数")
        
        return Validators.validate_report_type(args[0])
    

    
    @staticmethod
    def validate_set_image_mode_command(args: List[str]) -> int:
        """验证设置图片模式命令"""
        if len(args) != 1:
            raise ValidationError("设置图片模式命令需要一个参数")
        
        return Validators.validate_image_mode(args[0])
    
    @staticmethod
    def validate_set_message_command(args: List[str]) -> str:
        """验证设置消息命令"""
        if len(args) < 1:
            raise ValidationError("设置消息命令至少需要一个参数")
        
        return Validators.validate_message_content(' '.join(args))
    
    @staticmethod
    def validate_set_rank_limit_command(args: List[str]) -> int:
        """验证设置排行榜人数命令"""
        if len(args) != 1:
            raise ValidationError("设置排行榜人数命令需要一个参数")
        
        return Validators.validate_rank_limit(args[0])
