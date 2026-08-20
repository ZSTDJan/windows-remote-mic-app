# RC003 Windows 维护记录

本文是本开源派生版本的维护入口，记录上游基线、修改原因、验证证据和
尚未完成的人工验收。它不替代 Git 历史，也不把自动测试写成真机通过。

## 上游与许可基线

- 派生仓库：`miaomiaozii/windows-remote-mic-app`
- 本轮起点：`271ed7947eec19c4c691ed3ba97f338461be8051`
- 起点标签：`v0.1.0-windows`
- 许可：GPL-3.0-only；原始归属与第三方组件边界见 `ATTRIBUTION.md`
- HID tap 参考实现：`remote-bridge-hub` 提交
  `8a93f321ac71a602300c6cd77f7256fa4b63068e`

## 当前维护批次

### 2026-08-20 RC003 基础功能修复

状态：检查点待实测，自动验证与真机验收分开记录。

用户可见问题：

1. 设置页的“检测真实按键”只能识别 Windows Raw Input 暴露的按键，
   返回键和音量键等由 Windows 键盘栈丢弃的 usage 无法在该入口出现。
2. 遥控器可以唤起和结束宿主语音输入，但讲话没有声音进入识别程序。

已确认原因：

- `BUG-001`：配置保存了 `Windows WDM-KS` 播放端点；当前 PortAudio
  阻塞式输出流不支持该 host API，流在收到 PCM 前即打开失败。
- `BUG-002`：设置页只启动 Raw Input；Frida HID tap 的启动函数只证明
  后台线程已创建，注入、连接、心跳和真实 HID IO 状态没有进入应用日志。

本批次范围：

- 只修复 RC003 的音频路由、语音触发配置独立性、HID tap 状态与设置页
  按键检测链路。
- 保留现有 BLE、ATVV、Raw Input、SendInput、Frida 和 VB-CABLE 架构。
- Quicker 仅保留为未来可选动作出口，本批次不做深度集成。
- 不把 DJI Mic 2 入口扩展为新的开发主线。

关联记录：

- `bugs/BUG-001-audio-endpoint.md`
- `bugs/BUG-002-hid-tap-readiness.md`
- `TESTING.md`

实施结果：

- 音频端点现在明确拒绝 `Windows WDM-KS`，同名端点优先使用
  `Windows WASAPI`，其次使用 `Windows DirectSound`，保存前执行真实
  `open/start/stop/close` 预检。
- 语音生命周期与宿主快捷键已分开保存，“按住说话/按一下切换”不再覆盖
  用户录制的快捷键。
- HID tap 改为隐藏独立子进程，保留目标 PID、WUDFHost 名称和固定 Gadget
  哈希校验，并把启动到有效 HID IO 的状态写入 `app.log`。
- 设置页的按键检测同时监听 Raw Input 与 HID tap；任一路径捕获后都会统一
  停止监听，检测期间不会执行已配置动作。

自动验证证据：

- 音频定向测试：284 项通过，2 项平台相关跳过。
- HID、入口、Qt 与应用定向测试：164 项通过，1 项平台相关跳过。
- 最终完整测试：947 项通过，7 项安全或平台相关跳过，退出码 0。
- 公开边界扫描：通过，扫描 209 个文件。
- PyInstaller 候选构建：成功；冻结程序 `--dry-run` 和 `--help` 退出码均为
  0，隐藏注入器对无效 PID 稳定返回退出码 4。
- 候选目录：`apps/windows/rc003/dist/RemoteMicRC003/`。
- 候选 EXE SHA-256：
  `7DBA3E5036FD00528B15D06BAD9C0F0973BC97A3FB0053B5CDD6AB31AFB457C0`。

仍未完成：真实 RC003 逐键、语音识别、断连重连、休眠恢复、长期运行与
杀软兼容验收。对应提交在提交完成后回写。

## 维护纪律

- 每次修改都应写明：上游起点、故障现象、根因、改动文件、自动检查、
  人工验收和对应提交。
- 自动测试通过但真实 RC003 尚未复测时，状态只能写“检查点待实测”。
- 不记录真实蓝牙地址、个人绝对路径、凭据或用户语音内容。
- 发布或分发派生构建时继续遵守 GPL-3.0-only，并保留
  `ATTRIBUTION.md`、`COPYRIGHT.md` 和第三方通知。
