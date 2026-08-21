# BUG-011 检测麦克风键时误开启语音会话

状态：自动验证通过，`fix11` 待本机真机复测

记录日期：2026-08-21

代码修复提交：`4d6a0fe`

## 现象

用户在 `fix10` 设置页逐个执行“检测真实按键”，13 个按钮都能被识别并高亮，
普通按键的 down/up 与原始键抑制也正常。但检测麦克风键后，即使用户没有
执行正式语音测试，桥接仍进入 TOGGLE 活跃状态，并每 60 秒重新打开一次设备
麦克风；每轮 PCM 汇总均为 `frames=0`、`result=empty`。

## 本机日志证据

2026-08-21 14:13:56 的同一次麦克风检测按下中，事件顺序为：

1. `voice audio started`；
2. `voice audio start used as microphone trigger`；
3. 约 82 毫秒后才出现 legacy F5；
4. `key detection captured button=mic; mapped action suppressed`；
5. `AudioStopped` 后记录空 PCM，并因 TOGGLE 仍活跃而续发 `MIC_OPEN`。

随后直到桥接进程被精确停止，日志每 60 秒重复一次空音频 stop/open。这证明
问题不在按键识别结果，而在多来源事件进入按键检测和语音状态机的先后顺序。
记录不包含设备地址、设备路径、身份 ID 或语音内容。

## 根因

后台一次性检测请求原本只在 `_on_button_event()` 中由 HID/Raw Input 或
legacy F5 发布结果。RC003 的 `AudioStarted` 或 `MicButtonPressed` 可以先于
这些物理边沿到达；它们看不到检测状态，会先执行正常语音路径、打开播放端点
并改变 HOLD/TOGGLE 状态。等 F5/HID 到达时，界面虽然得到 `mic`，但语音副
作用已经发生。

检测请求在首个来源发布结果后立即被认领并移除，因此后到来源也不能只靠
“是否还有请求文件”判断自己属于检测手势。

## 修复设计

- 增加进程内麦克风检测手势门闩，统一接收 `AudioStarted`、
  `MicButtonPressed`、legacy F5 和 HID/Raw Input 物理边沿。
- 任一来源先认领检测请求后，只发布一次 `mic`；同一实体手势的后续来源全部
  合并并吞掉，不发送宿主快捷键，不打开播放端点，不改变 VoiceController，
  也不发送 `MIC_OPEN` / `MIC_CLOSE`。
- 同时追踪音频开始/停止和各物理来源 down/up；完整释放后保留 1 秒乱序宽限，
  并设置 10 秒硬超时，兼顾延迟事件和 BLE-only 缺失边沿，不形成永久抑制。
- legacy F5 的右 Alt 变换检查与检测请求认领使用同一把短锁，消除“请求刚被
  AudioStarted 认领、F5 尚未看到内存门闩”的注入窗口。
- 连接清理时重置检测门闩，不把上一连接代的检测状态带到重连后。

## 自动验证

- `AudioStarted` 先到，随后 F5、HID、ATVV 到达：只发布检测结果，语音状态
  不变。
- HID 先到，随后 ATVV、音频和 F5 到达：不发送宿主快捷键或设备命令。
- `MicButtonPressed` 先到且释放事件乱序：门闩到期后下一次正常麦克风按下仍
  能触发语音。
- HOLD 右 Alt 特殊路径在请求仍待处理、以及请求已由音频来源认领两种阶段都
  不会提前变换 F5。
- 应用接线测试：81 项通过，1 项平台相关跳过。
- 完整测试：1093 项通过，7 项安全或平台相关跳过，退出码 0。
- 公开边界扫描：229 个文件通过；`compileall`、`pip check` 和
  `git diff --check` 通过。

## `fix11` 候选

- 构建时 Git HEAD：`c135991accc081e35d1e5c53624aca7259ff1c86`。
- 候选目录：
  `RemoteMicRC003-0.1.0-candidate-20260821-fix11/`。
- EXE SHA-256：
  `4639AC9942C3D66525ED8EC7457F1EA7562E1B9DBD14ED5E5FD05BB2CB44ED7D`。
- 便携 ZIP：
  `RemoteMicRC003-0.1.0-candidate-20260821-fix11-portable.zip`。
- ZIP SHA-256：
  `DF9F50C4B9F11D703FA65BFE200A52FBD953B7591670FA3AEF89B6C96985127B`。
- ZIP 内 EXE 已重新计算并与候选目录一致；冻结入口
  `--dry-run=0`、`--help=0`、无效 HID 注入 PID `=4`。

## 真机复测门槛

1. 运行 `fix11` 后，从托盘打开设置并检测麦克风键一次；界面应高亮麦克风，
   宿主语音窗口不得被唤起，日志不得进入持续的空 PCM 重开循环。
2. 检测结束后等待约 1 秒，再执行一次正常麦克风按键；必须仍能触发语音，
   证明检测门闩没有残留。
3. 分别继续 HOLD 和 TOGGLE 真机测试；按键检测通过不能替代这两种语音生命
   周期和非零 PCM 的独立验收。
