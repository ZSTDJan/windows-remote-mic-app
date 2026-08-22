# BUG-018：按住说缺少 AUDIO_STOP 时宿主快捷键不释放

状态：代码与自动回归通过，等待 RC003 真机复测。

## 现象

异机反馈在执行模拟快捷键后，Ctrl、Alt 或 Win 之类的键会保持按下，后续操作
像是一直带着修饰键。

2026-08-22 下午日志中有一条可以解释该现象的完整链路：

- `15:50:06.895` 收到实体麦克风 F5 按下；
- `15:50:06.897` 记录“physical mic trigger received before audio start”，HOLD
  状态进入 active；
- 此后没有对应的 `AudioStarted` 或 `AudioStopped`；
- `15:50:26.124` 已看到该实体 F5 抬起，但旧实现没有在这里释放宿主快捷键；
- `15:50:26.186` 的下一次按下被判定为“hold session already active”；
- 直到 `15:50:36.921` 程序退出清理，旧 HOLD 状态才获得释放机会。

同一份日志在 `15:43:04.261` 和 `15:43:04.507` 另有两次普通组合键
`SendInput delivered only 0/6 events`。这表示整批事件一个也没有进入 Windows，
不会形成“只按下没抬起”，与本缺陷不是同一原因。

## 根因

普通映射使用单次批量 `SendInput`，同一批内包含全部 key-down 和反向 key-up；
部分发送和异常路径也有逐键释放及待释放重试。

缺口只在语音 HOLD 状态机：

- 按下实体键时向宿主发送 KEY_DOWN；
- 实体键抬起只更新多来源手势集合；
- 只有 BLE 控制通道的 `AudioStopped` 才调用 KEY_UP。

因此，只要 Windows 已看到实体抬起、但本次 BLE 会话没有产生或丢失
`AudioStopped`，`VoiceController._holding` 就会保持为真，宿主修饰键也可能持续
按下。下一次按键还会被旧 active 状态拒绝。

## 修复

- `VoiceController` 新增 `on_mic_button_released()`：HOLD 在物理抬起时返回
  KEY_UP 并清除 holding。
- 实体麦克风键在最后一个 HID/F5 来源抬起时立即调用该释放路径。
- 非麦克风键映射为 HOLD 语音时，也在最后一个来源抬起时先释放宿主快捷键，
  再发送原有 `MIC_CLOSE`。
- 后到的 `AudioStopped` 继续作为没有物理抬起事件时的兜底；已释放时返回空，
  不会重复发送 KEY_UP。
- 若物理抬起时 KEY_UP 交付失败，恢复 holding 状态并请求重连，让既有 cleanup
  继续重试，不能把未确认的释放记成成功。

## 自动验证

- 语音状态机、应用接线与 Win32 输入定向测试：166 项通过，1 项按平台条件
  跳过。
- RC003 完整 unittest：1175 项通过，7 项按安全或平台条件跳过，退出码 0。
- 第一次完整测试遇到 Qt 离屏临时缓存文件被 Windows 短暂占用；对应单项复跑
  通过，随后完整 1175 项复跑通过。这不是按键状态机失败。
- 公开边界扫描：277 个文件通过。
- `compileall`、`pip check` 和 `git diff --check` 通过。
- PyInstaller 本地冻结构建通过；EXE 的 `--dry-run` 与 `--help` 均退出 0，
  检查后无残留进程。该 EXE 尚未经过 RC003 真机验证。

## 真机复测

当前只能标为“检查点待实测”。至少验证：

1. HOLD 按住讲话、抬起后立即结束，键盘输入不再表现为 Ctrl/Alt/Win 卡住；
2. 快速短按且没有形成有效音频流时，抬起后下一次按键仍可正常触发；
3. 正常收到 `AudioStopped` 的会话只发送一次 KEY_UP；
4. 断连或退出后仍无残留修饰键状态。
