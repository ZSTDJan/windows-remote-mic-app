# BUG-009 音频停止早于物理释放并阻塞低层 F5 钩子

状态：自动验证通过，`fix8` 待异机真机验收

记录日期：2026-08-20

## 现象

`fix7` 能偶尔完成切换语音并得到 `result=signal`，但表现时灵时不灵；部分
按键在记事本中插入当前日期时间。Windows 记事本的 F5 默认动作正是插入
当前日期时间，因此该现象也说明某些 RC003 原生 F5 没有被稳定吞掉。

## `fix7` 现场证据

- 多来源合并本身已经生效：日志先记录
  `voice physical trigger ignored: same mic gesture source=legacy_f5/hid`，没有
  在首个重复来源处立即关闭。
- 一次典型按下中，低层 F5 于 23:45:10.578 进入应用回调，但对应的物理
  手势处理到 23:45:12.072 才完成，间隔约 1.5 秒；同一时段应用正在同步
  打开 PortAudio 播放端点并持有语音状态锁。
- `AudioStopped` 于 23:45:12.073 到达，旧逻辑立即释放整轮手势；延迟的 HID
  down 于 23:45:12.119 随后到达，被误认成下一次按下，最终再次发送
  `MIC_CLOSE`。
- 日志还出现连续重复的 `AudioStarted` / `AudioStopped`，同一个 stop 会重复
  续发 `MIC_OPEN`，放大了开关竞态。

## 根因

1. `fix7` 把 `AudioStopped` 单独当成可靠的手势结束边界，但真机上音频控制
   通知会早于已经进入其他线程队列的 F5/HID 物理边沿。仅凭音频停止释放
   门闩仍然过早。
2. `WH_KEYBOARD_LL` 的回调同步调用 `_on_button_event()`；当另一线程持有
   `_voice_trigger_lock` 打开 PortAudio 时，低层钩子线程也会等待约 1.5 秒。
   低层键盘钩子不能承担这种阻塞工作，否则 Windows 可能停用超时钩子，
   原生 F5 随后就会到达记事本。
3. 音频 start/stop 没有边沿去重，重复通知会重复清空统计、重开设备并改变
   后续事件的时序。

## 修复设计

- 手势结束改为双边界：收到 `AudioStopped` 后先标记音频已停；如果仍有
  legacy F5/HID 来源处于 down，则继续保留手势，直到所有物理来源都 up。
- `MIC_OPEN` 产生的续流 `AudioStarted` 若发生在旧手势尚待物理释放期间，
  只作为延续流处理，不重新认领一次按键。
- 连续重复 `AudioStarted` 只接受第一条；同一流的重复 `AudioStopped` 只
  处理第一条，避免多次续发 `MIC_OPEN`。
- 低层 F5 回调只完成 auto-repeat 合并和事件投递，应用处理通过所属 asyncio
  事件循环异步执行；钩子不再等待语音锁或 PortAudio 打开。
- 清理/重连使用 generation 使旧连接排队中的 F5 事件失效，不能污染下一
  连接。
- 切换模式第二次按下只做关闭，不重新打开或复核播放端点，减少关麦延迟。

## 自动验证

- 复现 `AudioStopped -> 续流 AudioStarted -> 延迟 HID down -> 物理 up`：
  同一手势不关闭，全部物理来源释放后下一次按下才关闭。
- 重复 `AudioStarted` 不再次切换；重复 `AudioStopped` 只续发一次
  `MIC_OPEN`。
- 在另一个线程持有语音状态锁时调用低层 F5 回调，回调仍在 0.2 秒内返回；
  排队事件在锁释放后正常处理。
- 切换关闭不调用播放端点打开/预检。
- 应用接线定向测试：62 项通过，1 项平台相关跳过。
- 完整测试：998 项通过，7 项安全或平台相关跳过，退出码 0。
- 公开边界扫描：226 个文件通过；`compileall`、`git diff --check` 通过。
- PyInstaller 构建和冻结入口检查通过：`--dry-run=0`、`--help=0`、无效 HID
  注入 PID 入口 `=4`。

## 真机门槛

- 记事本中连续短按测试不能再插入日期时间。
- 切换模式第一次短按并松开后讲话产生文字，第二次短按稳定结束；至少连续
  测试 5 轮。
- 回传日志应出现 F5、HID 同手势忽略和 continuation/duplicate 音频诊断，
  不能在物理释放前出现错误的第二次 `voice toggle closing`。

## `fix8` 候选

- 便携 ZIP：
  `RemoteMicRC003-0.1.0-candidate-20260820-fix8-portable.zip`。
- ZIP SHA-256：
  `2148F4F25C56E2117FEA9FC80A9A460BA0905CD3B09442CAD6CCB5C8260089FA`。
- ZIP 内 EXE SHA-256：
  `E9FF471EF55CB8A7713DC934155D30D207D69278FA9550C116987E21B0EB10AB`，
  已从 ZIP 条目重新计算并与候选目录一致。
