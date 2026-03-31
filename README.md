# OpenClaw Config Panel

用于接管本机 `OpenClaw` 配置的单用户管理面板。

支持的核心能力：

- 模型库维护
- Provider 管理
- Channel 管理
- Agent 管理
- 一键将面板配置刷入 OpenClaw
- Provider 可用模型探测与 diff 回显
- 单用户登录、图形验证码、30 天会话
- 可选的独立 Codex 控制台

## 项目定位

这个项目不是 OpenClaw 官方仓库的一部分，而是一个面向自托管场景的外置控制面板。

设计目标：

- 尽量不侵入 OpenClaw 原始目录结构
- 面板配置与 OpenClaw runtime 配置分离
- 可通过 `systemd` 独立安装、重启、迁移
- 默认仅监听 `127.0.0.1`

## 运行环境

- Linux
- Python `3.10+`
- `systemd`（推荐，用于托管面板服务）
- 已安装并可运行的 `openclaw`

Python 依赖见 `requirements.txt`：

- `requests`

## 目录结构

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

### 1. 本地安装

在已经安装好 `openclaw` 的机器上执行：

```bash
cd /path/to/openclaw-config-panel
python3 -m pip install -r requirements.txt
chmod +x install.sh start.sh
./install.sh --state-dir /你的/openclaw/状态目录 --base-path /xyz/api/config
```

### 2. 远程一键安装

如果你已经部署了一台现成面板服务器，可以通过它暴露出来的 `bootstrap.sh` 远程安装：

```bash
curl -fsSL http://你的面板服务器:5711/xyz/api/config/bootstrap.sh | \
  bash -s -- --state-dir /root/.openclaw --base-path /xyz/api/config
```

如果目标机器上的 OpenClaw 状态目录就是默认 `~/.openclaw`，也可以省略 `--state-dir`：

```bash
curl -fsSL http://你的面板服务器:5711/xyz/api/config/bootstrap.sh | \
  bash -s -- --base-path /xyz/api/config
```

## 常用参数

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

你需要自己设置一次：

- 用户名
- 密码

初始化完成后：

- 自动登录
- 后续使用用户名、密码、图形验证码登录
- 不提供面板内修改用户名和密码入口

## 可选：Codex 控制台

仓库内包含可选的独立 Codex 控制台：

- 默认端口：`5712`
- 默认 base path：`/xyz/codex`

启动方式：

```bash
./start_codex_console.sh
```

如果要自定义 Codex 历史目录，可设置环境变量：

```bash
export CODEX_HISTORY_ROOT=/data/codex/sessions
```

## 存储文件

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

这些都属于运行时数据，不应提交到 Git 仓库。

## 安全说明

- 默认仅绑定 `127.0.0.1`
- 所有页面和接口在初始化完成后都需要登录
- 登录方式为用户名、密码、图形验证码
- 图形验证码有效期为 `5` 分钟
- 会话有效期为 `30` 天

## 开源前建议

发布到 GitHub 前，建议至少检查以下内容：

- 不要提交 `config-panel-*.json` 等运行时状态文件
- 不要提交 `__pycache__/`、`*.pyc`
- 不要提交真实 API key、面板账号、私有域名配置
- 补充仓库 License
- 根据你的发布方式调整 README 中的安装地址

详细说明见：

- `docs/ARCHITECTURE.md`
- `docs/OPEN_SOURCE_CHECKLIST.md`
