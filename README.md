# mkbot

mkbot 是一个正在开发的自主角色框架。角色拥有持续状态、经历、关系、计划和可组合的插件能力。

当前已实现可运行的内核、管理面板、有界主会话、统一模型网关和插件 SDK。项目仍处于早期开发，尚未接管线上 QQ；分层记忆和自主调度将在此基础上继续建设。

## 本地开发

需要 Python 3.12+ 和 uv。

```powershell
uv sync --group dev
uv run pytest
uv run mkbot init
uv run mkbot serve
```

默认监听 `127.0.0.1:8096`。实例数据放在 `.data/`，管理令牌写入 `.data/admin-token`。
接口采用 Bearer 认证。实例目录和令牌不应提交到 Git。

管理面板可以配置人格、模型路由与价格，并手动运行一轮活动。默认没有供应商或模型路由，因此首次启动不会产生模型费用。`uv run mkbot --help` 查看命令；指定实例目录时，将 `--data-dir PATH` 放在 `init` / `serve` 前面。

## 模型配置

面板的「模型与路由」保存 `models` 配置。每次新调用读取当前版本，正在执行的请求使用启动时的快照。供应商密钥仅填写环境变量名称，例如 `MKBOT_MODEL_KEY`，不要把密钥写入配置或 Git。

可参照 `examples/models.json` 填入服务地址、模型 ID 和实际渠道价格，再保存到面板。示例中的价格仅为格式演示，不代表任何真实模型报价。`roles.planner` 数组按主用、备用顺序填写；主循环和插件的模型服务共用同一入口。

当前支持非流式 Chat Completions 的文本与工具调用；适配器不做隐藏重试、不自动跟随重定向、不继承进程代理变量。网关负责 180 秒默认超时、持久化尝试次数与退避、备用路由和熔断。响应未提供 usage 时保留预留，实际费用缺失不记成零；现阶段费用是按供应商 token 数和配置价格计算，未与上游账单对账。

`micro_usd` 为百万分之一美元。当前日界为 UTC，日预算与根活动预算共用预留检查；预估并不等同于供应商的硬性计费上限。限次重试、根活动步数和重复无进展检查共同限制调用。

## 插件与主会话

插件以 `Manifest + Tool + PluginContext` 注册。能力描述、专业说明和参数 schema 进入主会话；工具需要额外模型处理时，调用 `context.model()` 继承当前活动预算。内置 `notebook` 是最小可运行示例，笔记按会话保存；它不是长期记忆系统。

主循环保存模型决策、工具结果和执行位置。一个活动的最终文本只进入一次发送队列，实际平台发送通过独立适配器确认回执。外部工具或投递超时保留 unknown，不盲目重做。

Python 插件目前在进程内运行，属于受信代码。SDK 提供统一约定，尚不是防止恶意插件越权访问文件或网络的隔离边界。

## 代码结构

```text
src/mkbot/
  domain/       类型、错误与状态契约
  storage/      SQLite 事务与版本迁移
  runtime/      时间、生命周期、任务、持久收件和有界主会话
  models/       类型化请求、HTTP 协议、路由和尝试账本
  plugins/      能力契约、受计量上下文、持久工具回执与示例
  actions/      发送队列、回执与重复执行控制
  accounting/   预算预留与用量账本
  api/          管理接口及面板
tests/          行为与故障场景
```

设计参考 AstrBot 的事件分发和插件 Context、MaiBot 的类型化工具与服务边界。
当前代码为独立实现。上游代码引用与后续引入的许可证按实际文件登记；发布前完成许可证清单。

协议参考：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[错误码](https://api-docs.deepseek.com/quick_start/error_codes/)。测试使用本地模拟供应商与 HTTP MockTransport，尚未完成真实供应商或 QQ 联调。
