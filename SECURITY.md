# Security

## Scope

这个项目面向 **单用户、自托管、受控环境** 的运维场景。

默认安全假设：

- 服务仅监听本机回环地址
- 通过 Nginx / Caddy / 其他反向代理统一暴露
- 部署者自行负责 HTTPS、访问控制、主机加固

## Built-in protections

- 默认绑定 `127.0.0.1`
- 登录需要用户名、密码、图形验证码
- 会话有有效期
- 运行时状态文件默认不纳入 Git 仓库

## Deployment recommendations

建议至少做到：

- 仅对可信网络或可信用户暴露
- 通过 HTTPS 提供访问
- 如有必要叠加 Basic Auth、WAF、IP 白名单
- 不要把运行时状态文件提交到 Git
- 不要把真实 key、token、密码写入源码仓

## Reporting security issues

如果你发现安全问题，请不要直接公开提交敏感细节。

更稳妥的方式是：

- 先通过私下渠道联系维护者
- 提供最小复现与影响范围
- 在修复完成前避免公开可利用细节
