# OpenClaw Config Panel

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Self-hosted](https://img.shields.io/badge/self--hosted-friendly-8a2be2)
![OpenClaw](https://img.shields.io/badge/OpenClaw-companion-orange)

一个面向 **OpenClaw 自托管场景** 的单用户 Web 管理面板，用来在浏览器里维护模型、Provider、Channel、Agent，并将面板配置安全写回 OpenClaw runtime。

仓库同时包含一个**可选的独立 Codex 控制台**，适合与你的 OpenClaw 面板并排部署；但主线定位仍然是 **OpenClaw Config Panel**。

> 这不是 OpenClaw 官方仓库的一部分，而是一个外置、自托管、偏运维场景的 companion project。

## Why this exists

如果你是通过 Linux 自托管 OpenClaw，通常很快会遇到这些问题：

- Provider、Channel、Agent 逐渐变多，手改配置越来越容易出错
- 想先编辑、校验、对比，再应用到 runtime，而不是直接改线上配置
- 需要一个更适合 Nginx 反代、子路径部署、单机运维的管理入口
- 希望把 OpenClaw runtime 配置维护从“手工改 JSON”变成“浏览器中的有状态操作”

这个项目的核心目标，就是把这套维护流程做得更稳定、更可视化、更适合长期自托管。

## Screenshots

待补充：

- 登录页
- 配置总览页
- Provider 管理 / 可用模型探测页
- Agent 管理页
- （可选）Codex 控制台页

> 建议在发布前补上真实截图；对开源项目的第一印象提升会非常明显。

## Feature highlights

- 在浏览器中维护 OpenClaw 模型、Provider、Channel、Agent
- 将面板 store 与 OpenClaw runtime 配置分离
- 支持一键将面板配置应用到 OpenClaw runtime
- Provider 可用模型探测与 diff 回显
- 单用户登录、图形验证码、30 天会话
- 默认仅监听 `127.0.0.1`
- 适合 Nginx 反向代理 + 子路径部署
- 可通过 `systemd` 独立托管
- 可选的独立 Codex 控制台

## Use cases

适合以下场景：

- 你在 Linux 主机上自托管 OpenClaw
- 你经常调整 Provider / Channel / Agent 配置
- 你不想频繁手改 runtime 配置文件
- 你希望先编辑、校验，再统一应用到运行环境
- 你通过 Nginx / Caddy / 反代子路径暴露内部管理入口
- 你希望为单用户运维场景准备一个更直观的控制台

## Design goals

- 尽量不侵入 OpenClaw 原始目录结构
- 面板配置与 OpenClaw runtime 配置分离
- 可独立安装、启动、迁移、重启
- 默认仅监听 `127.0.0.1`
- 优先适配 Nginx 反向代理 + 子路径部署

## Runtime requirements

- Linux
- Python `3.10+`
- `systemd`（推荐）
- 已安装并可运行的 `openclaw`

依赖：

- `requests>=2.31.0`

## Quick start

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

也可以通过环境变量覆盖：

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

## Installation parameters

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

更多部署细节见：

- `docs/DEPLOYMENT.md`
- `docs/FAQ.md`

## First-time setup

首次打开登录页时，没有默认账号密码。

你需要自行设置：

- 用户名
- 密码

初始化完成后：

- 自动登录
- 后续使用用户名、密码、图形验证码登录
- 当前不提供面板内修改用户名和密码入口

## Runtime files

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

## Recommended deployment model

推荐把服务只监听在本机回环地址，再由 Nginx 做反向代理：

- `/xyz/api/config` → 主面板
- `/xyz/codex` → Codex 控制台

这样可以：

- 避免直接暴露本地端口
- 统一 HTTPS
- 方便挂在子路径下
- 配合额外访问控制（如 Basic Auth、WAF、IP 白名单）

## Project structure

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

## Security notes

- 默认仅绑定 `127.0.0.1`
- 所有页面和接口在初始化完成后都需要登录
- 登录方式为用户名、密码、图形验证码
- 图形验证码有效期为 `5` 分钟
- 会话有效期为 `30` 天

## Open source notes

这个仓库是源码仓，不包含以下内容：

- 真实 API key
- 面板账号密码
- 会话 token
- 本机运行态 store / auth / jobs / uploads 数据

如果你基于本项目继续发布自己的部署版本，建议先阅读：

- `docs/ARCHITECTURE.md`
- `docs/OPEN_SOURCE_CHECKLIST.md`
- `docs/DEPLOYMENT.md`
- `docs/FAQ.md`
- `CHANGELOG.md`

## Roadmap ideas

后续如果继续演进，比较值得优先做的方向是：

- 将超长单文件前端拆分为模块化构建
- 为 Provider 探测补单元测试
- 给 store schema 增加版本迁移
- 把可选集成做成更清晰的 feature flag
- 补充真实截图、release 说明与部署示例

## License

MIT
