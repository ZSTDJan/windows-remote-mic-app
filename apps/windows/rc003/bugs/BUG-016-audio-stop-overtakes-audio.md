# BUG-016：AUDIO_STOP 越过已收到的音频通知

状态：代码与自动回归通过，等待 RC003 真机短语音复测。

## 现象

部分很短的按住语音会话能看到 `AudioStarted` 和 `AudioStopped`，但 PCM
汇总为 `frames=0 samples=0`。这类空会话既可能来自设备根本没有发音频，
也可能来自 Windows 客户端已经收到音频、却在内部处理顺序上先关闭了解码器。

## 排除项

本机用 VB-CABLE 做过独立合成信号实验：

- 端点枚举约 219 至 250 毫秒；
- 输出流打开约 62 至 78 毫秒；
- 写入后约 69 至 99 毫秒能在 `CABLE Output` 观察到信号；
- 60、150 和 645 毫秒三种合成信号都能到达；
- 没有 PortAudio underflow。

因此，不能把所有短会话空 PCM 都归因于 VB-CABLE 或“语音太短来不及到达”。

## 确定性复现

`RC003BleSession` 原本为 CONTROL 和 AUDIO 使用两个有界队列，worker 永远
优先取 CONTROL。使用生产 `RC003BleSession` 与 `ATVVSession` 注入以下顺序：

1. `AUDIO_START`；
2. 10 个 AUDIO 通知；
3. `AUDIO_STOP`。

旧实现先处理开始、再越过 10 个音频处理停止。`ATVVSession` 收到停止后立即
关闭 decoder，随后才出队的音频都按晚到数据拒绝，结果是开始/停止事件完整，
PCM 回调为零。

这与用户日志中的短会话空 PCM 形状一致，但不把每一次 `frames=0` 都强行归为
同一原因；设备未发音频的会话仍由 `BUG-017` 单独记录。

## 根因

“控制优先”对能力协商、错误和一般控制事件是正确的，但 `AUDIO_STOP` 具有
特殊协议含义：它不能越过在它之前已经进入客户端的同一代音频通知。两个队列
没有共同顺序标记，worker 无法判断哪些 AUDIO 属于停止之前。

WinRT 还可能在不同线程调用两个特征的通知回调。如果序号分配与实际入队分开，
较早的 AUDIO 线程在两步之间暂停时，较晚的 STOP 仍可能先进入控制队列。

## 修复

- CONTROL 与 AUDIO 共用单调递增通知序号。
- 序号分配和对应队列插入位于同一个 producer lock 内，避免并发回调在线程
  切换时形成“早序号、晚入队”。
- worker 仍保持一般 CONTROL 优先；仅在处理 `AUDIO_STOP` 前，先处理同一
  generation 且序号更小的 AUDIO。
- 遇到序号不小于当前 STOP 的音频时只暂存一条，等待后续控制事件先建立新的
  会话，不能把下一会话首帧错误归给上一会话。
- cleanup 清空两个队列和暂存音频；日志只记录排空通知数量，不记录设备标识
  或语音内容。

## 自动验证

- 新增确定性回归：开始 + 10 个音频 + 停止，10 批 PCM 必须全部先于停止处理。
- 新增跨会话回归：上一会话停止不能吞入下一会话的首帧。
- 新增并发 producer 回归：阻塞 AUDIO 入队时，后来的 STOP 不能先插入。
- `NotificationProcessingTests`：9 项通过。
- RC003 全量 unittest：1154 项通过，7 项按安全或平台条件跳过，退出码 0。

## 尚未证明的边界

当前序号证明的是“进入应用回调的顺序”。Microsoft 的
`GattValueChangedEventArgs` 提供通知时间戳，但 WinRT 文档没有承诺两个不同
GATT 特征之间的回调交付顺序。若系统在应用收到回调之前已经把真实早到 AUDIO
排到 STOP 之后，本修复无法从 payload 单独恢复设备原始顺序。

真机复测需要连续做短按与约 1 至 3 秒按住会话，确认非零 PCM、尾音和停止后
无残留；如果仍出现控制停止早于音频回调，应另行记录 WinRT 通知时间戳再决定
是否需要跨特征重排，不能用更长固定等待掩盖。

macOS 的
`Bugs/2026-08-09-rc001-short-voice-stream-tail-dropped.md` 只有“短流尾音丢失”
症状相似；其根因是播放队列清空，不是本 Windows 客户端的 CONTROL/AUDIO
队列越序。
