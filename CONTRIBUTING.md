# 参与贡献

感谢参与无线麦 Windows 版。这个仓库只维护小米蓝牙遥控器 2 Pro（RC003）的 Windows
客户端；macOS、其它遥控器和独立 OrthoFocus 项目有各自的维护边界。

## 报告问题

提交前请先搜索现有 Issue，并确认问题仍能在最新候选或当前源码中复现。普通问题使用
Issue 模板；安全漏洞按 [`SECURITY.md`](SECURITY.md) 私密报告。

问题报告请写清：

- 使用的 release tag、commit、安装版或便携版；
- Windows 版本、RC003 配对状态和相关宿主软件；
- 最短复现步骤、实际结果和预期结果；
- 已执行的相关检查及其真实结果。

不要上传真实蓝牙地址、HID 路径、完整用户配置、个人绝对路径或未脱敏日志。

## 本地开发

在 Windows PowerShell 中进入 `apps/windows/rc003`：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v
```

完整构建、真机验收和发布不是每个改动的默认步骤。测试入口见
[`TESTING.md`](apps/windows/rc003/TESTING.md)，真实 RC003、语音输入、音频端点和
长期运行仍需人工检查。

## 提交修改

- 保持改动围绕一个明确问题，不顺带重构无关模块；
- 新增依赖时同步更新锁定版本、`THIRD_PARTY_NOTICES.md` 和许可门禁；
- 不提交用户数据、日志、构建目录、下载的第三方二进制或签名材料；
- 行为变化应补自动测试，真实硬件未验证时明确写成“检查点待实测”；
- Pull Request 说明问题、方案、验证结果和仍未完成的人工项。

向本仓库提交代码或文档，表示贡献者有权提供这些内容，并同意按项目现行的
`GPL-3.0-only` 许可证发布。第三方代码和素材必须同时保留原始许可与归属。
