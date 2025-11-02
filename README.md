# AstrBot 群发言统计插件

> 🤖 **此插件由AI辅助生成**

一个功能强大的AstrBot群消息统计插件，支持自动统计群成员发言次数并生成排行榜。

## 📋 功能特性

### 🔢 统计功能
- **自动统计**：自动监听群聊消息并统计发言次数
- **多时间维度**：支持总排行、今日排行、本周排行、本月排行
- **实时更新**：消息实时记录，排行榜实时更新

### 📊 展示方式
- **图片模式**：生成精美的排行榜图片
- **文字模式**：简洁的文字排行榜显示
- **自定义数量**：可设置排行榜显示人数（1-100人）

### ⚙️ 管理功能
- **手动更新**：支持手动记录当前用户发言
- **配置调整**：可调整显示模式和排行数量
- **数据清理**：支持清除指定群的统计数据

## 🚀 安装说明

### 方式一：直接下载
1. 下载插件压缩包 `astrbot_plugin_message_stats.zip`
2. 解压到AstrBot插件目录：`/AstrBot/data/plugins/`
3. 重启AstrBot

### 方式二：Git克隆
```bash
cd /AstrBot/data/plugins/
git clone https://github.com/xiaoruange39/astrbot_plugin_message_stats.git
```

## 📖 使用方法

### 基础命令

#### 查看排行榜
- `#发言榜` - 查看总发言排行榜
- `#今日发言榜` - 查看今日发言排行榜  
- `#本周发言榜` - 查看本周发言排行榜
- `#本月发言榜` - 查看本月发言排行榜

#### 管理命令
- `#更新发言统计` - 手动记录当前用户发言
- `#设置发言榜数量 [数量]` - 设置排行榜显示人数（1-100）
- `#设置发言榜图片 [模式]` - 设置显示模式（1=图片，0=文字）
- `#清除发言榜单` - 清除本群发言统计数据

### 使用示例

```
#发言榜
总发言排行榜
发言总数: 156
━━━━━━━━━━━━━━
第1名：小明·45次（占比28.85%）
第2名：小红·32次（占比20.51%）
第3名：小刚·28次（占比17.95%）
```

```
#设置发言榜数量 10
排行榜显示人数已设置为 10 人！
```

```
#设置发言榜图片 1
排行榜显示模式已设置为 图片模式！
```

## ⚙️ 配置说明

### 插件配置
插件支持以下配置选项：
- `rand`: 排行榜显示人数（默认20人，范围1-100）
- `if_send_pic`: 显示模式（1=图片模式，0=文字模式）

### 配置方式
1. 通过命令配置（推荐）
2. 编辑配置文件：`data/config.json`

## 📁 文件结构

```
astrbot_plugin_message_stats/
├── main.py                 # 主程序文件
├── metadata.yaml          # 插件元数据
├── README.md              # 说明文档
├── requirements.txt       # 依赖包
├── config.yaml           # 配置文件
├── data/                 # 数据目录
│   └── config.json       # 用户配置
├── templates/            # 模板目录
│   └── rank_template.html # 排行榜模板
└── utils/                # 工具模块
    ├── __init__.py
    ├── data_manager.py   # 数据管理
    ├── image_generator.py # 图片生成
    ├── models.py         # 数据模型
    └── validators.py     # 数据验证
```

## 📝 更新日志

### v1.0 (2025-11-02)
- ✅ 完整群昵称获取功能
- ✅ 群名称自动获取
- ✅ 异步调用优化
- ✅ 配置界面清理
- ✅ 错误处理增强

### v0.9 (之前版本)
- 基础消息统计功能
- 排行榜生成
- 图片模式支持

## 🤝 贡献指南

欢迎提交Issue和Pull Request！

### 开发环境
```bash
# 安装依赖
pip install -r requirements.txt

# 运行测试
python -m pytest tests/
```

## 📄 许可证

本项目采用 MIT 许可证。

## 👨‍💻 作者

**xiaoruange39**
- GitHub: [@xiaoruange39](https://github.com/xiaoruange39)
- 插件开发：AstrBot生态贡献者

## 🙏 致谢

感谢以下项目和插件的参考：
- [AstrBot框架](https://github.com/SKStudio/AstrBot) - 强大的多平台聊天机器人框架
- [yunzai-plugin-example](https://github.com/KaedeharaLu/yunzai-plugin-example) - 原始插件基础架构参考
- AstrBot社区 - 提供的开发文档和技术支持

---

**如果这个插件对您有帮助，请给个⭐支持一下！**
