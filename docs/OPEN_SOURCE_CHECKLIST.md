# Open Source Checklist

发布到 GitHub 前，建议逐项确认：

## 代码与仓库

- [x] 补充 `LICENSE`
- [x] 检查 README 的安装地址、域名、截图是否仍为私有环境
- [x] 确认 `.gitignore` 已覆盖运行时状态文件
- [x] 删除 `__pycache__/`、`*.pyc`
- [x] 补充 `CHANGELOG.md`
- [x] 补充部署与 FAQ 文档

## 敏感信息

- [x] 不包含真实 API key
- [x] 不包含真实账号密码
- [x] 不包含真实会话 token
- [x] 不包含私有域名、私有 IP、内部路径说明

## 部署文档

- [x] 说明 Python 版本要求
- [x] 说明 `requests` 依赖
- [x] 说明默认端口和 base path
- [x] 说明需要已安装 `openclaw`
- [x] 补充推荐部署方式与运行时文件说明

## 运行验证

- [x] `python3 -m py_compile *.py`
- [x] 安装脚本可执行
- [ ] 首次初始化可用
- [ ] 登录 / 登出可用
- [ ] Provider 保存 / 删除 / 刷新可用
- [ ] Agent 应用与全量应用可用

## 可选能力

如果你准备一并开源可选组件，再额外确认：

- [x] Codex 控制台说明清楚是“可选”
- [x] 导航页中的链接全部改成通用相对路径或占位说明
- [x] Metapi / QingLong 等外部系统仅作为导航，不绑定私有部署细节

## 还建议补充

- [ ] README 真实截图
- [ ] GitHub release
- [ ] 仓库 topics
- [ ] 在干净环境再做一次完整部署验证

## 说明

上面已打勾项目表示当前仓库版本已完成基础开源清理；未打勾项目是接下来最值得补的发布项。
