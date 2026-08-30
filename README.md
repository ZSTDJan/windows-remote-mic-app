# 无线麦 · Windows 版本（小米遥控器2 Pro）

这是 `miaomiaozii/windows-remote-mic-app` 仓库，面向小米遥控器2 Pro（内部型号 RC003）的 Windows 蓝牙桥接软件：把遥控器的按键和语音转成 Windows 能识别的键盘按键与语音输入，从而在电脑上操控豆包、微信、WPS 等应用。macOS 应用、Swift 工程和 macOS 发布资源不属于本仓库。

Windows 客户端位于 [`apps/windows/rc003`](apps/windows/rc003/README.md)，提供：

- WinRT BLE 连接与 ATVV 语音解码；
- Windows Raw Input + Frida HID 旁路按键监听，SendInput 按键映射；
- 语音输出到用户明确选择的音频端点（配合虚拟声卡供输入法识别）；
- PySide6/Qt Quick 三页桌面程序、通知区域控制、分项检查和 PyInstaller/Inno Setup 构建。

元素导航的独立发布模板位于
[`apps/windows/element-navigation`](apps/windows/element-navigation/README.md)。它从 RC003
当前使用的同一份导航源码导出可独立安装和测试的项目，不复制维护第二套算法；目前尚未
建立独立 Git 仓库或公开发布，导出版本的许可证仍是发布前待办。

当前版本是**已获得部分真实硬件验证、仍待完整复测的源码/构建候选**：真实
小米遥控器2 Pro 已验证多候选连接、13 个实体按键在映射页识别、记事本普通映射、有效语音
PCM 与可见识别文字，且最新实测未再泄漏原生 F5 的日期时间。当前未发布源码把
“开关型语音/按住型语音”改为按键主映射动作，并允许麦克风键改做普通动作；
非麦克风键启动持续 ATVV 语音、两种生命周期完整区分、重连、休眠和长期运行仍
待 `fix15` 真机复测。产物未签名；CI 和自动构建不能替代真实硬件验收。

## 下载与安装

最新候选发布：[v0.1.0-windows-rc003-candidate.1](https://github.com/miaomiaozii/windows-remote-mic-app/releases/tag/v0.1.0-windows-rc003-candidate.1)。

从 Release 页面 Assets 下载，二选一：

| 资产 | 适用场景 |
| --- | --- |
| `RemoteMicRC003Setup-0.1.0-candidate-unsigned.exe` | 推荐，安装到开始菜单/桌面并创建快捷方式 |
| `RemoteMicRC003-0.1.0-candidate-portable-unsigned.zip` | 免安装，解压到任意目录直接运行 |

两个都未签名，Windows SmartScreen 会提示，点“更多信息 → 仍要运行”即可。
建议同时下载 `SHA256SUMS.txt` 校验文件哈希。

## 快速开始（安装版）

1. 下载 `RemoteMicRC003Setup-...exe` 并运行，一路“下一步”完成安装；
2. 首次运行，或双击开始菜单的“无线麦”，打开程序窗口；
3. 在 Windows 设置 → 蓝牙中把小米遥控器2 Pro 与电脑配对；
4. 回到“设备”页重新检查设备和按键接收；在“语音”页选择输出端点、语音程序和
   语音按键，再点击“应用”；
5. 在“按键”页把实体语音键的主映射设为“按住说话”，或改成普通动作并保存映射；
6. 回到“设备”页点击“启动桥接”，等待遥控器服务连接小米遥控器2 Pro；
7. 按已映射为普通动作的按键验证 Windows 操作，再按已映射的语音按键验证
   输入法语音。需要登录后自动运行时，在“设备”页底部开启“随 Windows 启动”；
   “启动程序时自动启动桥接”只是程序打开后自动执行一次“启动桥接”，可以单独开关。
   关闭窗口默认隐藏到通知区域，“完全退出”会先正常停止遥控器服务。

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

运行桥接（单独的桥接进程）：

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

## 许可证

代码按 `GPL-3.0-only` 发布。完整许可证见 [`LICENSE.md`](LICENSE.md)。

## 维护边界

- 主程序源码只在 `apps/windows/rc003`；
- 元素导航独立发布模板位于 `apps/windows/element-navigation`，正式导航源码仍由
  `apps/windows/rc003/scripts` 单一维护；
- Windows CI 位于 `.github/workflows/windows-rc003-ci.yml`；
- `device-profiles` 只保留 Windows 客户端使用的设备目录；
- `LICENSE.md`、`COPYRIGHT.md`、`THIRD_PARTY_NOTICES.md` 和
  [`apps/windows/rc003/ATTRIBUTION.md`](apps/windows/rc003/ATTRIBUTION.md) 保留 GPL
  与上游来源义务，不代表继续维护原 macOS 应用。
