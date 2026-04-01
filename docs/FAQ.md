# FAQ

## 这是 OpenClaw 官方项目吗？

不是。

这是一个面向 OpenClaw 自托管场景的外置 companion project，更偏运维与控制面板用途。

## 适合多用户吗？

当前主要面向单用户场景。

如果你需要更复杂的权限体系、多角色协作或团队级审批流，这个项目暂时不是主打方向。

## 它会直接修改 OpenClaw 源码目录吗？

设计目标之一就是尽量不侵入 OpenClaw 原始目录结构。

面板内部维护自己的 store，只有在你主动应用配置时，才会把数据写回 OpenClaw runtime 配置。

## 为什么不直接手改配置文件？

手改配置文件当然可行，但当 Provider / Channel / Agent 变多以后：

- 容易改错
- 不方便对比和回顾
- 不适合频繁调整
- 不利于浏览器中的可视化维护

这个项目的目标是把那套流程做得更直观、更稳妥。

## 必须启用 Codex 控制台吗？

不是。

Codex 控制台是可选能力，主线项目依然是 OpenClaw Config Panel。

## 支持反向代理和子路径部署吗？

支持，这正是推荐部署方式之一。

例如：

- `/xyz/api/config/` -> 主面板
- `/xyz/codex/` -> Codex 控制台

## 运行时数据存在哪里？

默认在 `~/.openclaw` 中。

其中包括：

- panel store
- presets
- auth
- sessions
- 可选的 Codex store / jobs / uploads

这些都是运行时数据，不应该提交到 Git 仓库。
