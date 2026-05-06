# 更新日志

## v1.9.0 (2026-05-05)

### ✨ 新功能

- **Web 数据管理面板大升级**：全新的发言统计 Web 面板，支持选择群组查看排行榜、彩色头像显示、群名称自动展示。
- **群组数据删除**：Web 面板新增红色 🗑️ 删除数据 按钮，支持二次确认弹窗，可一键清除群组发言统计数据。
- **群名称持久化缓存**：生成发言榜时自动获取群名称并持久化到文件，Web 面板直接读取，群改名后发一次发言榜指令即可同步。

### 🐛 Bug 修复

- **修复 Web 面板返回按钮无效**：ES Module 作用域中的函数未暴露到全局 window，返回按钮点击无反应。已修复：改用 window 挂载 + 事件委托方式绑定。
- **修复 Web 面板用户缺少头像**：排行榜用户列表只显示数字序号，没有头像元素。已修复：根据昵称首字生成彩色圆形文字头像（15色哈希分配）。
- **修复群聊只显示群号**：Web 面板群列表显示格式改为"群名称 - 群号"，群名称在生成发言榜时自动获取。
- **修复删除按钮不工作**：`confirm()` 在浏览器安全策略下被阻止，且字符串 onclick 传参出错。已修复：改用自定义 CSS modal 弹窗 + `element.onclick` 事件委托绑定。
- **修复 Web 面板按钮颜色在暗色模式下看不清**：返回按钮和删除按钮的颜色在暗色模式下使用固定蓝/红色，在白底上不清晰。已修复：全部改为 CSS 变量，暗色模式下自适应更柔和的亮色。

### 🔧 其他改进

- **Web 面板支持跟随系统深色/浅色模式**：将 `@media(prefers-color-scheme)` 方案升级为 `:root + html[data-theme]` CSS 变量方案，添加 `matchMedia` 监听系统主题变化实时自动切换。所有颜色值（背景、卡片、文字、按钮、提示、头衔标签等）均使用 CSS 变量，暗色模式下正确适配，并添加 0.3s 过渡动画，切换更平滑。

## v1.8.8 (2026-05-05)

### ✨ 改进

- **放宽排行榜昵称显示限制到15字**：当用户有头衔时，昵称可完整显示最多15个字，超出部分自动以省略号代替。

### 🐛 Bug 修复

- **修复图片生成失败时 tmp 文件积累**：`generate_rank_image()` 和 `generate_milestone_image()` 在生成失败时未清理已创建的临时文件，导致 tmp 目录文件不断积累。已修复：失败时自动删除自身创建的 tmp 文件。

- **修复手动查询里程碑后 tmp 文件不清理**：调用 `#发言榜里程碑` 命令后，生成的里程碑卡片临时图片未删除，日积月累会占用磁盘空间。已修复：发送图片后自动删除临时文件。

- **unified_msg_origin 持久化**：定时推送依赖 unified_msg_origin（消息路由地址），但该信息仅保存在内存中。重启 Docker 后插件不知道往哪个群发消息，导致定时推送静默失败。已修复：每次收集到 UMO 时写入 `data/unified_msg_origins.json` 文件，启动时自动恢复，不再依赖群友先发消息。

## v1.8.7 (2026-05-04)

### ✨ 新功能

- **LLM 头衔配色大师**：LLM 现在可以为每个用户的头衔指定独特的配色（`color` 字段），头衔徽章会动态显示对应的颜色和背景色，告别单一的紫色样式。

### 🐛 Bug 修复

- **修复定时推送头衔颜色丢失（严重）**：`_generate_rank_image()` 构造 `titles_map` 时只取了 `display_title` 纯字符串，丢弃了 `display_title_color`。导致定时推送的排行榜图片中所有 LLM 生成的头衔颜色退化为默认紫色。已修复：构建 `titles_map` 时同时携带 `color` 字段。

- **修复 LLM 头衔不显示的问题**：`_parse_titles()` 传入的是用户昵称，但后续渲染需要 user_id，导致头衔无法匹配到用户。已改为昵称→user_id 映射后再解析头衔。

- **修复头衔显示为字典字符串的问题**：LLM 返回的 `{"title": "...", "color": "..."}` 字典格式未被解析，直接显示为字符串。已修复：`_process_user_data_batch()` 中正确提取 `title` 和 `color` 字段。

- **修复头衔颜色硬编码**：模板中 `color: #7C3AED` 和 `background: #EDE9FE` 写死，LLM 返回的颜色不生效。已改为动态 `{{ item.title_color }}` 渲染。

- **修复头衔徽章垂直不对齐**：`.nickname-row` 使用 `align-items: baseline` 导致 24px 昵称和 13px 头衔徽章按基线对齐，头衔偏下。已改为 `align-items: center` 居中。

- **修复液态玻璃主题头徽章垂直不对齐（修复遗漏）**：v1.8.7 只修复了 `rank_template.html`，漏掉了 `rank_template_liquid_glass.html`、`rank_template_liquid_glass_dark.html` 和 `user_item_macro.html` 三个模板。已统一将 `align-items: baseline` 改为 `align-items: center`。

- **LLM 分析范围与排行榜显示人数绑定**：手动查询排行榜时，LLM 只分析排行榜实际显示的用户（即 config.rand 指定的数量），不再分析全群有发言记录的用户，避免 Token 浪费。

- **修复暗色主题 CSS 类样式与 inline style 冲突**：`rank_template_liquid_glass_dark.html` 的 `.user-title` CSS 类中 `color` 和 `background` 写死，可能造成 FOUC。已移除硬编码颜色，颜色完全由 inline style 控制。

- **修复 Fallback 渲染路径忽略头衔颜色**：当 Jinja2 不可用时，fallback 路径的头衔样式完全硬编码，未使用 `title_color`。已修复：fallback 路径也动态渲染头衔颜色。

- **修复 `metadata.yaml` 版本号不一致**：`metadata.yaml` 版本仍为 `1.8.6`，已同步更新至 `1.8.7`。

### 🔧 其他改进

- **优化 Web 面板配置文案**：里程碑次数列表提示去掉方括号改用顿号分隔；LLM 提示词模板描述更清晰。

## v1.8.5 (2026-05-04)

### 🐛 Bug 修复

- **修复 `_handle_command_exception()` 缺少 `yield` 关键字**：`event.plain_result()` 是生成器方法，直接调用无法发送消息到聊天中。已将方法重构为仅记录日志，避免静默失败。

- **修复昵称双重 HTML 转义导致显示乱码**：`validate_nickname()` 原本使用 `html.escape()` 转义后存储，渲染图片时又再次转义，导致昵称出现 `<` 等双重转义乱码。已修复：存储时不再做 HTML 转义，统一在渲染阶段进行一次转义。

- **修复 `_get_avatar_url()` 处理 Telegram 负数用户 ID 时的潜在崩溃**：`int(user_id) % 5` 对负数字符串 ID 直接 `int()` 转换可能抛出异常。现已改用 `abs(int(user_id))` 取绝对值，并添加 `ValueError`/`TypeError` 兜底处理。

- **修复定时推送没有数据的问题**：`timer_manager._filter_data_by_rank_type()` 只检查旧的 `user.history` 列表来判断用户是否有发言记录，但新版数据改用 `_message_dates` 字典存储。修复方案：添加 `user._ensure_message_dates()` 兜底保护，并同时检查 `_message_dates` 和 `history`。

### 🚀 性能与内存优化

- **修复群组锁清理条件缺陷**：`_get_group_lock()` 的过期锁清理条件原为 `len > 100 and len % 100 == 0`，导致 101~199 个锁时不会触发清理。现已改为 `>= 100 and (len % 50 == 0 or len > 1000)`，确保锁数量持续增长时仍能自动清理。

- **`_dirty_cache` 添加大小上限**：原脏缓存字典无容量限制，高负载场景下大量群组数据长期驻留内存。现已添加 500 条上限，达到上限时强制立即写盘防止内存泄漏。

## v1.8.2 (2026-05-03)

### 🐛 Bug 修复

- **修复数据丢失问题**：`data_cache`（TTL=5分钟）过期后，`get_group_data` 从文件重新加载数据，但 `GroupDataStore` 的延迟批量写入策略（积累10次修改才写盘一次）导致文件中的数据不是最新的，从而丢失了部分消息记录。修复方案：`get_group_data` 优先检查 `_dirty_cache`（延迟写入缓存），确保获取到最新的内存数据。

- **修复今日发言榜无法使用的问题**：数据存储格式升级后（从 `history` 列表改为 `_message_dates` 字典），排行榜计算逻辑未同步更新。修复方案：`_calculate_daily_rank` 和 `_calculate_period_rank_optimized` 改用 `user.get_message_count_in_period()` 方法，兼容新旧两种数据格式。

### 🚀 性能优化

- **浏览器懒加载与高并发支持**：引入了浏览器并发计数器和异步锁，实现"懒加载且支持高并发"。渲染和截图过程在互相独立的 Page 标签页中并行执行，完美解决了之前高并发下图片串台、以及因强行关闭浏览器导致的 `Target closed` 报错问题，兼顾了低内存占用与高并发性能。
- **按天聚合字典存储**：10万条消息最多 365 个键值对，内存占用从 O(n) 降到 O(365)。
- **延迟批量写入优化**：积累 10 次修改后批量写盘，减少磁盘 I/O 约 90%。
- **群组锁自动清理**：`_group_locks` 字典增加 TTL 机制，自动清理超过 1 小时未使用的锁，防止长期运行的内存泄漏。
- **批量写入循环优化**：增加 60 秒超时兜底写入，防止数据长时间滞留在内存中丢失；优化 `asyncio.wait` 为 `asyncio.wait_for`，减少不必要的 Task 创建。

### ✨ 新功能

- **发言里程碑推送**：当用户发言达到里程碑次数时，自动推送个人成就卡片。
- **更新日志文档**：新增 `CHANGELOG.md` 更新日志。

### 🔧 其他改进

- 插件关闭时确保脏缓存数据落盘（`flush_all`）。
- 紧凑 JSON 格式（`indent=None, separators=(',', ':')`），文件体积减少约 50%。
- 优化异常处理，替换过于宽泛的 `except Exception` 为具体异常类型。
