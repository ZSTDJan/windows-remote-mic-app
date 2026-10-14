# 参与贡献

这个仓库维护无线麦 Windows 客户端，支持小米蓝牙语音遥控器 2 Pro 和图示谷歌
Chromecast 遥控器。macOS 和独立 OrthoFocus 项目有各自的维护入口。

## 报告问题

提交前请先搜索现有 Issue，并核对最新正式版是否已修复。普通问题使用
Issue 模板；安全漏洞按 [`SECURITY.md`](SECURITY.md) 私密报告。

问题报告请写清：

- 使用的程序版本和下载来源；源码运行时附 commit；
- Windows 版本、遥控器型号、配对状态和相关输入法或应用；
- 最短复现步骤、实际结果和预期结果；
- 已执行的相关检查及其真实结果。

不要上传真实蓝牙地址、HID 路径、完整用户配置、个人绝对路径或未脱敏日志。

## 本地开发

在 Windows PowerShell 中进入 `apps/windows/rc003`：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v
```

完整构建、真机验收和发布不是每个改动的默认步骤。验证深度按
[`VALIDATION-AND-DELIVERY.md`](apps/windows/rc003/VALIDATION-AND-DELIVERY.md)
选择，真实 RC003、语音输入、音频端点和长期运行仍需人工检查。

## 提交修改

- 保持改动围绕一个明确问题，不顺带重构无关模块；
- 新增依赖时同步更新锁定版本、`THIRD_PARTY_NOTICES.md` 和许可门禁；
- 不提交用户数据、日志、构建目录、下载的第三方二进制或签名材料；
- 行为变化应补自动测试，真实硬件未验证时明确写成“检查点待实测”；
- Pull Request 说明问题、方案、验证结果和仍未完成的人工项。

向本仓库提交代码或文档，表示贡献者有权提供这些内容，并同意按项目现行的
`GPL-3.0-only` 许可证发布。第三方代码和素材必须同时保留原始许可与归属。
