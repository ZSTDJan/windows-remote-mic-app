# BUG-018：按住说缺少 AUDIO_STOP 时宿主快捷键不释放

状态：第三轮迟到 F5 与发送所有权缺口已修正，自动回归与冻结构建通过，等待
RC003 真机复测。

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

### 2026-08-22 真机复测发现的第二层缺口

第一轮修复仍把“物理抬起”定义成所有 HID/F5 来源都已抬起。新日志证明这个
前提在 RC003 上不成立：

- `20:51:30.109` 已收到直接 HID 话筒 usage up，这是遥控器按键真实松开的
  最早设备级证据；
- BLE 音频在 `20:51:30.303` 停止；
- Windows 的 legacy F5 却继续产生自动重复 down，直到 `20:51:31.959` 才出现
  up，比真实松开晚约 1.85 秒；
- 第一轮代码因为来源集合中仍留有 `legacy_f5`，没有在直接 HID up 时释放宿主
  快捷键，仍会让搜狗看到明显晚于键盘操作的抬起时序。

这也解释了“键盘操作正常、遥控器松开后界面仍停留”的差异：键盘抬起会立即
到达宿主；RC003 的直接 HID 已抬起，但旧代码错误等待了延迟的 legacy F5 up。

### 2026-08-24 一般自检发现的第三层缺口

直接 HID 松开后，legacy F5 还可能继续晚到并跨进下一轮。进一步强制线程顺序
复现出：`_transform_legacy_voice_key()` 已返回替换目标、
`_emit_legacy_voice_key()` 尚未发送时，direct HID down 先走普通配置快捷键，
随后 emit 再发右侧 Alt。同一次实体按下因此会出现两个 down、一个 up。

这不是随机猜测，测试已稳定得到重复调用序列。直接原因是旧代码只把已经完成
emit 的 transform session 当作“宿主动作已处理”，没有给“已决定、未发送”的
在途 F5 与物理 HID 分配唯一发送所有权。

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
- 第二轮修正把直接 HID tap 的话筒 usage up 视为权威物理抬起，不再等待延迟的
  legacy F5 up。晚到的 F5 up 和 `AudioStopped` 仍只清理来源状态，不重复发送
  KEY_UP。
- 第三轮用同一状态锁串行化 F5 transform/emit、设置应用和 `AudioStopped`。
  物理 HID 先认领时隔离尚未 emit 的 F5，并发送当前配置快捷键；F5 先 emit 时，
  HID 识别 transform session 后不重发。两种顺序都只有一个发送者，且不等待
  低层键盘钩子。

## 自动验证

- 语音状态机、应用接线、能力探针与 Win32 输入定向测试：189 项通过，1 项按
  平台条件跳过。
- RC003 完整 unittest：1179 项通过，7 项按安全或平台条件跳过，退出码 0。
- 第一次完整测试遇到 Qt 离屏临时缓存文件被 Windows 短暂占用；对应单项复跑
  通过，随后完整 1175 项复跑通过。这不是按键状态机失败。
- 公开边界扫描：277 个文件通过。
- `compileall`、`pip check` 和 `git diff --check` 通过。
- PyInstaller 本地冻结构建通过；EXE 的 `--dry-run` 退出 0。该 EXE 尚未经过
  RC003 真机验证。
- 2026-08-24 最新完整 unittest 1205 项通过、7 项跳过；应用接线 113 项通过、
  1 项跳过且无资源泄漏记录。公开边界扫描 302 个文件，`compileall`、
  `pip check`、PowerShell 全脚本解析、`git diff --check`、PyInstaller、冻结
  `--help` / `--dry-run` 和 11 个 QML 一致性均通过。

## 真机复测

当前只能标为“检查点待实测”。至少验证：

1. HOLD 按住讲话、抬起后立即结束，键盘输入不再表现为 Ctrl/Alt/Win 卡住；
2. 快速短按且没有形成有效音频流时，抬起后下一次按键仍可正常触发；
3. 正常收到 `AudioStopped` 的会话只发送一次 KEY_UP；
4. 断连或退出后仍无残留修饰键状态。
