# OpenClaw Config Panel

一个面向 **OpenClaw 自托管场景** 的轻量级单用户管理面板，支持通过浏览器维护模型、Provider、Channel、Agent，并将面板配置写回 OpenClaw runtime。

仓库还包含一个**可选的独立 Codex 控制台**，适合与你的 OpenClaw 面板并排部署。

> 这不是 OpenClaw 官方仓库的一部分，而是一个外置、自托管、偏运维场景的控制面板项目。

## 功能概览

- 模型库维护
- Provider 管理
- Channel 管理
- Agent 管理
- 一键将面板配置应用到 OpenClaw
- Provider 可用模型探测与 diff 回显
- 单用户登录、图形验证码、30 天会话
- 可选的独立 Codex 控制台
- 可通过 `systemd` 独立托管

## 设计目标

- 尽量不侵入 OpenClaw 原始目录结构
- 面板配置与 OpenClaw runtime 配置分离
- 可独立安装、启动、迁移、重启
- 默认仅监听 `127.0.0.1`
- 优先适配 Nginx 反向代理 + 子路径部署

## 运行环境

- Linux
- Python `3.10+`
- `systemd`（推荐）
- 已安装并可运行的 `openclaw`

依赖：

- `requests>=2.31.0`

## 项目结构

```text
openclaw-config-panel/
├── server.py                     # 面板 HTTP 服务
├── openclaw_config_manager.py    # store 校验、探测、写回 OpenClaw 的核心逻辑
├── install_panel.py              # 一键安装与 systemd 注册
├── restart_panel.py              # 安全面板重启
├── ccswitch_import.py            # CCSwitch SQL 导入
├── codex_console_server.py       # 可选的独立 Codex 控制台
├── codex_job_runner.py           # Codex 后台任务执行器
├── panel_codex_runtime.py        # Codex store / job / systemd 运行时辅助
├── static/                       # 前端页面与 bootstrap 脚本
└── docs/                         # 架构与发布文档
```

## 快速开始

### 1) 本地安装

在已经安装好 `openclaw` 的机器上执行：

```bash
cd /path/to/openclaw-config-panel
python3 -m pip install -r requirements.txt
chmod +x install.sh start.sh start_codex_console.sh
./install.sh --state-dir /你的/openclaw/状态目录 --base-path /xyz/api/config
```

### 2) 手动启动主面板

```bash
./start.sh
```

默认值：

- host: `127.0.0.1`
- port: `5711`
- state dir: `/root/.openclaw`
- base path: 空（可通过反代子路径传入）

你也可以通过环境变量覆盖：

```bash
HOST=127.0.0.1 PORT=5711 STATE_DIR=/root/.openclaw BASE_PATH=/xyz/api/config ./start.sh
```

### 3) 可选：启动独立 Codex 控制台

```bash
./start_codex_console.sh
```

默认值：

- host: `127.0.0.1`
- port: `5712`
- state dir: `/root/.openclaw`
- base path: `/xyz/codex`

如果要自定义 Codex 历史目录，可设置：

```bash
export CODEX_HISTORY_ROOT=/data/codex/sessions
./start_codex_console.sh
```

## 安装参数

```bash
./install.sh \
  --state-dir /data/openclaw \
  --host 127.0.0.1 \
  --port 5711 \
  --base-path /xyz/api/config \
  --reset-auth
```

参数说明：

- `--state-dir`：OpenClaw 状态目录，需包含 `openclaw.json`
- `--host`：监听地址，默认 `127.0.0.1`
- `--port`：监听端口，默认 `5711`
- `--base-path`：反向代理子路径，例如 `/xyz/api/config`
- `--reset-auth`：删除面板登录配置，重新走首次初始化

## 首次初始化

首次打开登录页时，没有默认账号密码。

你需要自行设置：

- 用户名
- 密码

初始化完成后：

- 自动登录
- 后续使用用户名、密码、图形验证码登录
- 当前不提供面板内修改用户名和密码入口

## 运行时文件

默认状态目录为 `~/.openclaw`，面板会在其中写入：

- `config-panel-store.json`
- `config-panel-presets.json`
- `config-panel-auth.json`
- `config-panel-sessions.json`

如果启用了 Codex 控制台，还会写入：

- `config-panel-codex-store.json`
- `config-panel-codex-auth.json`
- `panel-codex-jobs/`
- `panel-codex-uploads/`

这些都属于**运行时数据**，不应提交到 Git 仓库。

## 推荐部署方式

推荐把服务只监听在本机回环地址，再由 Nginx 做反向代理：

- `/xyz/api/config` → 主面板
- `/xyz/codex` → Codex 控制台

这样可以：

- 避免直接暴露本地端口
- 统一 HTTPS
- 方便挂在子路径下
- 配合额外访问控制（如 Basic Auth、WAF、IP 白名单）

## 安全说明

- 默认仅绑定 `127.0.0.1`
- 所有页面和接口在初始化完成后都需要登录
- 登录方式为用户名、密码、图形验证码
- 图形验证码有效期为 `5` 分钟
- 会话有效期为 `30` 天

## 开源说明

这个仓库是源码仓，不包含以下内容：

- 真实 API key
- 面板账号密码
- 会话 token
- 本机运行态 store / auth / jobs / uploads 数据

如果你基于本项目继续发布自己的部署版本，建议先阅读：

- `docs/ARCHITECTURE.md`
- `docs/OPEN_SOURCE_CHECKLIST.md`

## 路线图建议

后续如果继续演进，比较值得优先做的方向是：

- 将超长单文件前端拆分为模块化构建
- 为 Provider 探测补单元测试
- 给 store schema 增加版本迁移
- 把可选集成做成更清晰的 feature flag

## License

MIT
