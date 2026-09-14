# Windows 版归属与改动说明

## 仓库历史

本仓库的历史来源为 [`HD838A/remote-mic-app`](https://github.com/HD838A/remote-mic-app)。
当前无线麦 Windows 版由本项目独立维护；macOS/Swift 工程不属于本仓库维护范围。
此处记录代码和仓库沿革，具体第三方代码与许可见下文及仓库根目录的
[`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md)。

本目录中的 Windows RC003 客户端基于以下 GPL-3.0-only 项目改造：

- 上游项目：[`nijez/open-voice-bridge`](https://github.com/nijez/open-voice-bridge)
- 上游 Windows 实现：`apps/windows/rc003/`
- 本仓库：[`ZSTDJan/windows-remote-mic-app`](https://github.com/ZSTDJan/windows-remote-mic-app)

上游项目已经提供了 RC003 的 Windows 参考实现，包括 WinRT BLE、ATVV 语音协议、
Windows Raw Input、SendInput、PortAudio 音频输出、Qt/QML 设置页、诊断、测试和
PyInstaller/Inno Setup 构建流程。本仓库在 GPL-3.0-only 条件下保留并适配这些能力，
并做了以下面向无线麦的改动：

- 用户可见名称统一为“无线麦”；配置目录、互斥锁、EXE、安装器文件名和发布产物
  继续保留 `RemoteMic` / `RC003` 内部兼容标识；
- 适配本仓库现有的 `LICENSE.md`、`COPYRIGHT.md` 和第三方声明文件；
- 补充中文安装、配对、VB-CABLE 配置、按键映射和故障排查说明；
- 保留上游的失败关闭策略、隐私约束、跨平台协议测试和 Windows CI 校验；
- 持续维护单进程桌面、按键映射、元素导航、输入法适配及链路诊断；具体演进见
  `CHANGELOG.md`，已通过范围与待测项见 `TESTING.md`，不以历史验收替代当前版本验证。

源码中仍保留 `ovb_rc003` 这一内部 Python 包名，以兼容现有导入、启动和构建入口；
它不是用户看到的应用名称。上游源码及其 GPL 许可适用于本目录中的派生代码，
本仓库根目录的 [`LICENSE.md`](../../../LICENSE.md) 是随源码发布的完整许可证。

## 其他参考来源

- `scripts/voice_hotkey_probe.py` 的手动扫描码对照参考
  [上游 v0.2.5 的 send_input_windows.rs](https://github.com/GetSayAll/remote-mic-app-windows/blob/v0.2.5/crates/sayall-windows/src/send_input_windows.rs)
  中的修饰键编码和 80 毫秒边沿间隔。该脚本仅用于显式诊断，不作为运行后端或打包内容。

- [`GetSayAll/remote-mic-app-windows`](https://github.com/GetSayAll/remote-mic-app-windows)
  v0.2.2，标签提交 `1335b82b0028690340c7a604af058bf0e40958a0`：参考其已验证的
  WeType 固定 `Ctrl+Win` 配方，包括独立 STA 会话级输入法激活、冷切换等待 50 毫秒和
  `SendInput` 逐边沿 80 毫秒间隔。当前 Python 实现沿用本仓库既有输入回滚、物理键预检、
  HID tap/F5 拦截和清理重试合同，没有引入其程序二进制或配置系统。

RC003 的 ATVV UUID、控制命令、IMA/DVI ADPCM 解码和 HID 映射事实见仓库根目录的
[`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md)；本分支不依赖另一个平台
的实现文件。

VB-CABLE 是 VB-Audio 的独立第三方软件，不属于本项目的 GPL 代码。Windows 构建
脚本只在显式执行、哈希固定的步骤中获取官方安装包；应用不会静默安装或修改系统
默认音频设备。使用 VB-CABLE 前请阅读 Windows README 中的方向说明和许可证信息。
应用只会在用户明确确认后调用 Windows 的 `runas`/UAC 流程启动厂商安装器，
自身进程不会提权。

搜狗及用户选择的其它语音输入程序同样是独立第三方软件，不属于本项目，也不会
随无线麦打包。可选的语音程序管理只负责发现或启动本机已有程序；管理员
启动必须由用户明确选择并通过 Windows UAC，无线麦自身进程不会因此提权。
