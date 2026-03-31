# Architecture

## 1. 整体结构

项目分成三层：

1. **前端静态页**
   - `static/index.html`
   - `static/login.html`
   - `static/home.html`
   - `static/codex-console.html`

2. **HTTP 服务层**
   - `server.py`
   - `codex_console_server.py`

3. **配置与运行时核心**
   - `openclaw_config_manager.py`
   - `panel_codex_runtime.py`
   - `codex_job_runner.py`
   - `ccswitch_import.py`

## 2. 核心数据流

### 面板配置流

```text
浏览器
  -> server.py
  -> openclaw_config_manager.py
  -> config-panel-store.json
  -> apply_store_config()
  -> OpenClaw runtime config
  -> restart_openclaw()
```

### Provider 探测流

```text
浏览器点击“一键刷新可用模型”
  -> /api/store/providers/refresh-models
  -> refresh_provider_available_models()
  -> _refresh_single_provider_available_models()
  -> /models + 各协议探测
  -> diff 结果返回前端
```

### Codex 控制台流

```text
浏览器
  -> codex_console_server.py
  -> panel_codex_runtime.py
  -> panel-codex-jobs/
  -> codex_job_runner.py
  -> Codex CLI
```

## 3. 关键设计

### 3.1 Store 与 Runtime 分离

面板内部维护自己的 store：

- `config-panel-store.json`

只有用户点击“应用配置”或 Agent 级应用按钮时，才会把数据翻译成 OpenClaw runtime 配置。

这样做的好处：

- 面板可保存比 OpenClaw 原生更丰富的元数据
- 可以先编辑、再应用
- 可以做 diff、探测、导入、排序等增强能力

### 3.2 Provider 模型探测

Provider 探测不是只看单一协议，而是组合：

- 模型列表接口
- 多种 API 类型
- 多种路由后缀
- 多个 API key

探测成功后只回写 OpenClaw 支持的路由组合。

### 3.3 单用户鉴权

当前面板仅考虑单用户场景：

- 首次初始化设置用户名 / 密码
- 登录后写入本地 session store
- 所有页面和接口统一鉴权

### 3.4 Codex 可选集成

Codex 控制台是独立服务，不依赖 OpenClaw 主面板启动。

这样做的目的：

- 降低面板主服务复杂度
- 允许独立部署、独立反向代理
- 将 OpenClaw 管理和 Codex 对话隔离

## 4. 建议的开源演进方向

如果后续准备继续公开维护，优先建议：

1. 将超长单文件前端拆分为模块化构建
2. 为 Provider 探测补单元测试
3. 为 `openclaw_config_manager.py` 中的 store schema 增加显式版本迁移
4. 将导航页、Codex、Metapi 等可选集成做成 feature flag
