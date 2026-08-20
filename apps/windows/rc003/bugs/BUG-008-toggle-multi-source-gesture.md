# BUG-008 切换模式把同一次按下的多来源事件当成多次点击

状态：`fix7` 部分生效，但真机发现音频停止早于物理释放；后续见
`BUG-009-f5-hook-release-race.md`

记录日期：2026-08-20

## 现象

`fix6` 已能从两个 WinRT 候选中选择唯一可用的 RC003，所有实体按键也能在
映射页识别。按住麦克风键讲话时能够产生识别文字，但切换模式短按一次后
讲话没有反应；实际表现仍像必须持续按住才能传音。

## 现场证据

- 长按会话记录到约 3.6 秒非零 PCM，汇总为 `result=signal`，证明 BLE、
  ATVV 解码、VB-CABLE 播放和宿主识别链路已经工作。
- 失败的短按会话只有约 180、225 或 360 毫秒，均汇总为
  `result=too_short`。
- 同一次实体按下会从多个 Windows/ATVV 路径到达，典型顺序包括：
  `AudioStarted`、低层 legacy F5、HID 物理麦克风边沿和
  `MicButtonPressed`。
- 旧接线只分别记住“等待 Raw Input”或“等待 AudioStarted fallback”；
  第一个重复来源会清除局部门闩，后一个来源随即被当成第二次独立按下，
  日志马上出现 `voice toggle closing: sending MIC_CLOSE`。

## 根因边界

这不是无 PCM、VB-CABLE 未安装、宿主快捷键错误或 RC003 多候选连接失败。
长按成功和 `result=signal` 已把故障缩小到应用层的按键手势合并。一个实体
按键动作拥有多个异步报告来源，而切换状态机需要的是“一次实体手势”，
不能把每条报告都作为一次开关动作。

## 修复设计

- 增加跨来源麦克风手势门闩；首个到达的 HID、legacy F5、ATVV mic 或
  `AudioStarted` 负责改变一次语音状态。
- 同一手势后续到达的其他来源只登记为重复，不再次发送宿主快捷键，也不
  发送 `MIC_CLOSE`。
- 记录仍处于按下状态的物理来源，避免一个来源先释放时过早开放下一次
  触发；可靠的 `AudioStopped` 作为流会话结束边界统一释放门闩。
- 第一次短按结束后仍按 `BUG-005` 的设计续发 `MIC_OPEN`；释放完成后，
  下一次独立按下才能关闭宿主和设备麦克风。
- HOLD 模式继续保持一次 key-down、一次 key-up，不改变按住说话行为。

## 自动验证

- 新增 `AudioStarted -> F5 -> HID -> ATVV` 乱序：只开启一次；释放后下一次
  按下才关闭。
- 新增 `HID -> ATVV -> AudioStarted -> F5` 乱序：只开启一次。
- 新增 HOLD 多来源回归：仍只发送一次 key-down 和一次 key-up。
- 应用接线定向测试：57 项通过，1 项平台相关跳过。
- 完整测试：993 项通过，7 项安全或平台相关跳过，退出码 0。
- 公开边界扫描：225 个文件通过；`compileall`、`git diff --check` 通过。
- PyInstaller 构建和冻结入口检查通过：`--dry-run=0`、`--help=0`、无效 HID
  注入 PID 入口 `=4`。
- 真机门槛：短按一次并松开后讲话产生文字，再短按一次结束；自动测试不能
  代替该项。

## 实施范围

- 实现：`src/ovb_rc003/app.py`。
- 回归测试：`tests/test_app_wiring.py`。
- 不修改 BLE 候选选择、ATVV 解码、音频处理、按键映射或 Quicker 边界。

## `fix7` 候选

- 便携 ZIP：
  `RemoteMicRC003-0.1.0-candidate-20260820-fix7-portable.zip`。
- ZIP SHA-256：
  `07D7738AF91F87B6E572172752A8C5C1C7D038E1AC33D16445B7491E86F2A312`。
- ZIP 内 EXE SHA-256：
  `765329CA703080D0F9DDBA2E850D9A10CC3C97F0FB190129A4183A86AADA68E7`，
  已从 ZIP 条目重新计算并与候选目录一致。

## `fix7` 真机复测

多来源门闩能够识别并忽略首轮 legacy F5/HID 重复事件，部分会话达到
`result=signal`；但 `AudioStopped` 会在延迟的物理 down/up 回调之前到达，
旧实现过早释放门闩，后到 HID 又被当成下一次按下。同步低层 F5 回调还会
等待 PortAudio 初始化约 1.5 秒，伴随记事本偶发插入日期时间。两项后续
竞态独立记录为 `BUG-009`，不把 `fix7` 写成真机通过。
