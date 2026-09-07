# 无线麦 · Windows 版本（小米遥控器2 Pro）

> [!IMPORTANT]
> **当前公开版本请暂时以管理员身份启动。** 普通权限运行时仍有已知异常；在问题修复并完成验证前，请右键程序或快捷方式，选择“以管理员身份运行”。

这是 `ZSTDJan/windows-remote-mic-app` 仓库，面向小米遥控器2 Pro（内部型号 RC003）的 Windows 蓝牙桥接软件：把遥控器的按键和语音转成 Windows 能识别的键盘按键与语音输入，从而在电脑上操控豆包、微信、WPS 等应用。macOS 应用、Swift 工程和 macOS 发布资源不属于本仓库。

Windows 客户端位于 [`apps/windows/rc003`](apps/windows/rc003/README.md)，提供：

- WinRT BLE 连接与 ATVV 语音解码；
- Windows Raw Input + Frida HID 旁路按键监听，SendInput 键盘与鼠标动作映射；
- 语音输出到用户明确选择的音频端点（配合虚拟声卡供输入法识别）；
- PySide6/Qt Quick 三页桌面程序、通知区域控制、分项检查和 PyInstaller/Inno Setup 构建；
- 每个 Windows 登录会话只保留一个长期主进程，桥接和元素导航在主进程内运行。

元素导航的独立项目 **OrthoFocus** 位于
[`apps/windows/orthofocus`](apps/windows/orthofocus/README.md)。它从 RC003
当前使用的同一份导航源码导出可独立安装和测试的项目，不复制维护第二套算法；独立源码
按 `GPL-3.0-only` 发布于 [`ZSTDJan/orthofocus`](https://github.com/ZSTDJan/orthofocus)。

当前版本是**已获得部分真实硬件验证、仍待完整复测的源码/构建候选**：真实
小米遥控器2 Pro 已验证多候选连接、13 个实体按键识别、记事本普通映射、有效语音
PCM 与可见识别文字。2026-09-07 的未发布源码以 `0.2.0-candidate.23` 为最近测试包
基线，保留方向键、输入释放、单实例、旧版接管和管理员按键助手权限修复，并补齐按键
重复来源交接、快捷键录入、声音通道测试恢复、搜狗语音窗口收口及通知区域窗口恢复；
微信输入法已改为直接控制其语音按钮，不再录入或模拟快捷键。助手运行失败时会区分权限、目标进程、运行文件和 DLL
载入环节。代码仍以普通权限主程序配合按需管理员助手为目标；
但当前公开版本在普通权限下还有已知异常，修复并完成复测前请按页首提示临时以管理员
身份启动。新配置关闭窗口时默认隐藏到通知区域，已有合法选择保留。实体方向键、重新
登录自启、真实旧版升级和长期运行仍需真机复测。产物未签名；自动检查不能替代真实
硬件验收。

便携版从其它目录或旧版本切换时，会先明确询问并等待旧版正常退出；同一副本重复双击
仍只恢复现有窗口。旧版 5 秒内没有接收退出请求时会立即提示从通知区域“完全退出”；
旧版完全退出后，用户这次双击的当前版本才继续打开，不再只唤出旧版。

## 下载与安装

最新公开候选：[v0.2.0-windows-rc003-candidate.2](https://github.com/ZSTDJan/windows-remote-mic-app/releases/tag/v0.2.0-windows-rc003-candidate.2)。

从 Release 页面 Assets 下载，二选一：

| 资产 | 适用场景 |
| --- | --- |
| `RemoteMicRC003Setup-0.2.0-candidate.2-unsigned.exe` | 推荐，安装到开始菜单/桌面并创建快捷方式 |
| `RemoteMicRC003-0.2.0-candidate.2-portable-unsigned.zip` | 免安装，解压到任意目录直接运行 |

两个都未签名，Windows SmartScreen 会提示，点“更多信息 → 仍要运行”即可。
建议同时下载 `SHA256SUMS.txt` 校验文件哈希。

### 程序内手动检查更新

在“设备”页“运行日志”一行点击“检查更新”。程序只在用户点击后访问本仓库的
GitHub Releases，不会在启动时或后台自动联网，也不需要 GitHub 账号或令牌。
发现新版后，安装版下载对应安装器，便携版或源码运行下载便携 ZIP；程序会核对
`SHA256SUMS.txt`，并在 GitHub 提供资产摘要时一并核对。最新版文件未上传完整时
会直接提示，不会推荐旧版本。文件保存到
`%LOCALAPPDATA%\RemoteMic\RC003\updates\<版本号>`；程序只清理自己创建的过期缓存。

下载完成后只提供“打开文件夹”，不会自动运行安装器或覆盖当前程序。请先完全退出
无线麦，再手动运行安装器；便携版请解压到新的文件夹使用。

## 快速开始（安装版）

1. 下载 `RemoteMicRC003Setup-...exe` 并运行，一路“下一步”完成安装；
2. 当前公开版本每次启动时，右键“无线麦”或其快捷方式，选择“以管理员身份运行”；
3. 在 Windows 设置 → 蓝牙中把小米遥控器2 Pro 与电脑配对；
4. 回到“设备”页重新检查设备和按键接收；在“语音”页选择输出端点、语音程序和
   语音按键，再点击“应用”；
5. 在“按键”页把实体语音键的主映射设为“按住说话”，或改成普通动作并保存映射；
6. 回到“设备”页点击“启动遥控器服务”，等待程序连接小米遥控器2 Pro；服务运行但
   遥控器尚未连接时，可点击“立即重连”跳过本轮等待；
7. 按已映射为普通动作的按键验证 Windows 操作，再按已映射的语音按键验证
   输入法语音。需要登录后自动运行时，在“设备”页底部开启“随 Windows 启动”；
   “启动程序时自动启动遥控器服务”只是程序打开后自动执行一次启动，可以单独开关。
   关闭窗口默认隐藏到通知区域；需要彻底结束时，可在“设备”页改为“完全退出”，或从
   通知区域菜单选择“完全退出”。

覆盖安装新版时，安装器会先让已运行的旧版完整退出；当前版本有未保存的按键修改时，
旧窗口会出现保存或放弃提示。较早的双进程版本会先正常停止遥控器服务，再把旧设置窗口
唤到前台，等待用户从通知区域选择“完全退出”。只有确认旧进程已经结束后才替换程序
文件；无法安全退出时安装会停止，不会强杀后继续覆盖。安装完成页默认勾选打开新版本，
窗口标题会显示版本号。升级不会删除 `%LOCALAPPDATA%\RemoteMic\RC003` 下的设置、日志
或采集数据。

详细配对、虚拟声卡配置、按键映射和故障排查见
[`apps/windows/rc003/README.md`](apps/windows/rc003/README.md)。

## 从源码本地运行

在 Windows PowerShell 中执行：

```powershell
Set-Location apps/windows/rc003
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m ovb_rc003 --settings
```

兼容启动形式（隐藏打开同一个桌面程序，并在其中启动遥控器服务）：

```powershell
.\.venv\Scripts\python.exe -m ovb_rc003 --bridge
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v
```

构建未签名候选版：

```powershell
.\build\build-candidate.ps1
```

完整安装、配对、VB-CABLE 配置、已知限制和发布流程见 [`apps/windows/rc003/README.md`](apps/windows/rc003/README.md)。

## 仓库来源

- **Fork 自**：[`HD838A/remote-mic-app`](https://github.com/HD838A/remote-mic-app)（无线麦 Remote Mic：把小米蓝牙遥控器 2 Pro / RC003 变成 Mac 语音输入设备）。本仓库只保留并继续维护其中的 Windows RC003 部分，macOS/Swift 部分不在此仓库维护。
- **Windows 上游参考实现**：[`nijez/open-voice-bridge`](https://github.com/nijez/open-voice-bridge)（GPL-3.0-only），提供 WinRT BLE、ATVV 语音协议、Raw Input、SendInput 和 Qt/QML 设置页的参考实现。
- **RC003 HID 旁路参考**：[`xxb26553663-star/remote-bridge-hub`](https://github.com/xxb26553663-star/remote-bridge-hub)（GPL-3.0-only），提供用 Frida Gadget 读取 Windows 普通输入链路拿不到的 RC003 返回/音量 HID 报告的实现思路。

Windows 实现的改动说明与第三方边界见
[`apps/windows/rc003/ATTRIBUTION.md`](apps/windows/rc003/ATTRIBUTION.md)、
[`COPYRIGHT.md`](COPYRIGHT.md) 和 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 参与、安全与发布状态

- 报告普通问题或提交修改前，请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)；
- 安全漏洞不要公开复现细节，按 [`SECURITY.md`](SECURITY.md) 使用私密入口；
- 社区讨论遵守 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)；
- Windows 二进制的完整第三方许可位于
  [`THIRD_PARTY_LICENSES`](THIRD_PARTY_LICENSES/README.md)，Qt/PySide6 对应源码
  与素材授权分别由 [`THIRD_PARTY_SOURCE.md`](THIRD_PARTY_SOURCE.md) 和
  [`ASSET_LICENSES.md`](ASSET_LICENSES.md) 管理。

普通分支和 Pull Request 可以运行源码、测试与构建检查，但只有正式 tag 才允许上传
可分发 CI artifact。tag 构建还必须先通过素材授权和第三方对应源码门禁；检查未通过时
不会为了赶发布绕过。

## 许可证

代码按 `GPL-3.0-only` 发布。完整许可证见 [`LICENSE.md`](LICENSE.md)。第三方组件和
素材不因进入同一个安装包就自动改为 GPL，仍按各自许可和授权记录分发。

## 维护边界

- 主程序源码只在 `apps/windows/rc003`；
- OrthoFocus 独立发布模板位于 `apps/windows/orthofocus`，正式导航源码仍由
  `apps/windows/rc003/scripts` 单一维护；
- Windows CI 位于 `.github/workflows/windows-rc003-ci.yml`；
- `device-profiles` 只保留 Windows 客户端使用的设备目录；
- `LICENSE.md`、`COPYRIGHT.md`、`THIRD_PARTY_NOTICES.md` 和
  [`apps/windows/rc003/ATTRIBUTION.md`](apps/windows/rc003/ATTRIBUTION.md) 保留 GPL
  与上游来源义务，不代表继续维护原 macOS 应用。
