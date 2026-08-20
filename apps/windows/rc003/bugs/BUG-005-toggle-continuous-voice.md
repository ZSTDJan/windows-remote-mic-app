# BUG-005 切换模式没有持续语音

状态：`fix5` 完成持续传音设计，但异机复测发现多来源重复触发；后续见
`BUG-008-toggle-multi-source-gesture.md`

记录日期：2026-08-20

## 现象

“按住说话”模式已经在本机产生可见识别文字，日志也记录到非零 PCM；但
“按一下切换”模式只能触发宿主开始和结束，第一次短按后讲话没有识别结果。

## 证据与根因

- 成功的按住会话记录到约 2.4 秒的非零 PCM，汇总结果为 `signal`。
- 失败的短按会话只有 60 至 195 毫秒，汇总结果为 `too_short`。
- 原 `VoiceController` 在第一次按下时发送宿主快捷键，在遥控器第一次松开
  产生 `AudioStopped` 时立刻再次发送快捷键并清除会话。
- 因而所谓切换模式实际上仍由同一次物理按下和松开完成开关，没有让用户在
  两次独立短按之间持续讲话。

## 修复设计

- 第一次有效按下：发送宿主切换快捷键，逻辑会话进入 active，并发送或续发
  `MIC_OPEN`。
- 第一次松开：`AudioStopped` 不再关闭宿主；只要逻辑会话仍 active，就再次
  发送 `MIC_OPEN`，保持 RC003 持续传音。
- 第二次有效按下：再次发送宿主切换快捷键，逻辑会话进入 inactive，并发送
  `MIC_CLOSE`。
- 关麦期间使用 close-pending 门闩忽略竞态到达的 `MicButtonPressed` 和
  `AudioStarted`，收到 `AudioStopped` 后完成收口。
- `MIC_CLOSE` 与既有 `MIC_OPEN` 使用同一线程安全调度、连接 generation、
  closing gate、任务跟踪和错误回报规则。
- `HOLD` 模式仍保持按下、讲话、松开结束的既有行为。

## 验证门槛

- 纯状态机验证第一次按下 active、第二次按下 inactive，`AudioStopped` 不
  改变切换状态。
- 应用接线验证第一次短按停止后会续发 `MIC_OPEN`；第二次按下发送
  `MIC_CLOSE`；关麦竞态事件不会重新开麦。
- BLE 契约验证线程安全 `MIC_CLOSE` 的正常写入、失败回报和 generation gate。
- 真机必须完成：短按一次、讲话得到文字、再短按一次结束；不能用自动测试
  代替这一项。

## 实施与提交

- 主要实现：`src/ovb_rc003/voice_controller.py`、
  `src/ovb_rc003/app.py`、`src/ovb_rc003/ble_transport_winrt.py`。
- 回归测试：`tests/test_voice_controller.py`、
  `tests/test_app_wiring.py`、`tests/test_ble_transport_contract.py`。
- 完整自动验证：980 项通过、7 项跳过；公开边界扫描 223 个文件通过。
- 对应提交：`ca5b69885a3796901dc36f1578cb7a754d73f140`。

## 后续复测

`fix6` 已证明第一次释放后能够获得约 3.6 秒 `result=signal` 的音频，但同一
实体按下的 `AudioStarted`、legacy F5、HID 和 ATVV mic 事件仍会被重复计数，
导致部分短按会话立即发送 `MIC_CLOSE`。该独立接线缺陷记录为 `BUG-008`，
不改写本记录对持续传音生命周期的历史结论。
