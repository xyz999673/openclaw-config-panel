# Deployment Guide

本文档给出一套偏实用主义的部署建议，适合 Linux 自托管 OpenClaw 的单用户管理场景。

## 1. 前置条件

建议环境：

- Linux 主机
- Python 3.10+
- 已安装并可运行的 `openclaw`
- `systemd`（推荐）
- Nginx / Caddy / 其他反向代理（推荐）

## 2. 推荐部署方式

推荐：

- 主面板只监听 `127.0.0.1:5711`
- 可选 Codex 控制台只监听 `127.0.0.1:5712`
- 通过 Nginx 统一反代到 HTTPS 域名和子路径

典型映射：

- `/xyz/api/config/` -> `127.0.0.1:5711`
- `/xyz/codex/` -> `127.0.0.1:5712`

这样做的好处：

- 减少直接暴露本地管理端口
- 方便复用已有域名和 HTTPS
- 更适合在同一台机器上挂多个内部服务

## 3. 安装步骤

```bash
cd /path/to/openclaw-config-panel
python3 -m pip install -r requirements.txt
chmod +x install.sh start.sh start_codex_console.sh
./install.sh --state-dir /你的/openclaw/状态目录 --base-path /xyz/api/config
```

## 4. 手动启动

### 主面板

```bash
./start.sh
```

### 可选 Codex 控制台

```bash
./start_codex_console.sh
```

## 5. 环境变量

### 主面板

- `HOST`：默认 `127.0.0.1`
- `PORT`：默认 `5711`
- `STATE_DIR`：默认 `/root/.openclaw`
- `BASE_PATH`：默认空
- `OPENCLAW_HOME`：默认根据 `STATE_DIR` 推导

示例：

```bash
HOST=127.0.0.1 PORT=5711 STATE_DIR=/root/.openclaw BASE_PATH=/xyz/api/config ./start.sh
```

### Codex 控制台

- `HOST`：默认 `127.0.0.1`
- `PORT`：默认 `5712`
- `STATE_DIR`：默认 `/root/.openclaw`
- `BASE_PATH`：默认 `/xyz/codex`
- `CODEX_HISTORY_ROOT`：Codex 历史目录

示例：

```bash
CODEX_HISTORY_ROOT=/data/codex/sessions ./start_codex_console.sh
```

## 6. 反向代理建议

反向代理时建议注意：

- 保留子路径前缀
- 只把服务暴露到需要的内部用户
- 加 HTTPS
- 如有必要叠加 Basic Auth、WAF 或 IP 白名单

## 7. systemd 建议

如果你准备长期托管，建议：

- 用 systemd 管理主面板
- 可选地单独托管 Codex 控制台
- 保持服务与 OpenClaw runtime 解耦

## 8. 运行时文件

默认写入 `~/.openclaw`：

- `config-panel-store.json`
- `config-panel-presets.json`
- `config-panel-auth.json`
- `config-panel-sessions.json`
- 可选的 Codex store / auth / jobs / uploads

这些文件都应视为运行时状态，不应提交到 Git 仓库。

## 9. 发布前自查

建议至少检查：

- README 中无真实域名 / 私有路径 / 密钥
- `.gitignore` 已覆盖运行时状态文件
- 无 `__pycache__` / `.bak` / 本地调试垃圾文件
- `python3 -m py_compile *.py` 通过
- 反代子路径和登录流程可用
