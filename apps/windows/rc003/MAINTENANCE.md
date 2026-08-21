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
3. 桥接已经运行时再次点击“保存并启动桥接”，重复子进程弹出系统模态框，
   同时设置页误显示该重复子进程“目前仍在运行”。
4. 关闭设置窗口后桥接仍在后台运行，但没有通知区域图标或菜单，用户无法
   直观看到桥接仍在运行，便携版只能通过任务管理器退出。
5. “按住说话”已能产生识别文字，但“按一下切换”仍在第一次短按松开时
   立即结束 RC003 音频，无法在两次短按之间持续讲话。
6. 后台桥接运行时，从通知区域打开的第二个设置进程无法使用“检测真实
   按键”；普通映射在记事本中仍能执行。
7. 异机删除并重新配对后，Windows 设置只显示一个 RC003，但 WinRT 仍返回
   两个名称匹配候选，桥接在连接前失败关闭。
8. `fix6` 已能连接并在长按时识别语音，但切换模式同一次实体按下被
   `AudioStarted`、legacy F5、HID 和 ATVV mic 多次报告，刚开启就被误判为
   第二次点击并关闭。
9. `fix7` 首轮多来源合并生效，但 `AudioStopped` 早于排队中的 F5/HID
   释放；同步低层 F5 回调还会等待 PortAudio 初始化约 1.5 秒，切换模式因而
   时灵时不灵，记事本偶发收到原生 F5 并插入日期时间。
10. `fix8` 已能稳定得到识别文字且未再泄漏日期时间，但用户在设置页切换
    HOLD/TOGGLE 和快捷键后，运行中桥的行为没有变化；日志仍显示旧 TOGGLE
    状态机。
11. `fix11` 中切换模式第一次短按后仍无声，必须先长按才能得到识别；日志
    显示续发 `MIC_OPEN` 时实体 F5 尚未 up。

已确认原因：

- `BUG-001`：配置保存了 `Windows WDM-KS` 播放端点；当前 PortAudio
  阻塞式输出流不支持该 host API，流在收到 PCM 前即打开失败。
- `BUG-002`：设置页只启动 Raw Input；Frida HID tap 的启动函数只证明
  后台线程已创建，注入、连接、心跳和真实 HID IO 状态没有进入应用日志。
- `BUG-003`：重复桥接子进程先显示模态提示再退出；设置页只轮询 1.5 秒，
  因提示框仍在等待用户点击而把该子进程误判为成功启动。
- `BUG-004`：设置进程与桥接进程有意分离，但后台桥接没有 Windows 通知
  区域生命周期入口；关闭设置窗口不是停止桥接操作。
- `BUG-005`：旧切换状态机把第一次物理松开产生的 `AudioStopped` 当成宿主
  第二次切换，同时关闭逻辑会话；短按 PCM 只有 60 至 195 毫秒。
- `BUG-006`：后台桥接已经独占 HID tap 端口并吞掉原始键，托盘打开的设置
  进程再次启动 Raw Input/HID tap，必然与资源所有者冲突或看不到事件。
- `BUG-007`：异机计数确认两个 RC003 使用不同设备 ID；PnP 检查确认可见的
  `MI RC` 与隐藏中文名节点属于不同容器。精确删除隐藏节点后，Windows 扫描
  会从配对数据库重建它。修复改为多候选时探测 UNCACHED ATVV 服务，只
  选择唯一可用候选；两个都可用时仍失败关闭。
- `BUG-008`：同一实体麦克风按下通过四条异步路径到达；旧的 Raw Input 和
  AudioStarted 局部门闩在第一个重复来源后即被清除，后续来源将切换状态
  再改变一次并发送 `MIC_CLOSE`。
- `BUG-009`：音频停止不是单独可靠的物理手势结束边界；低层键盘钩子线程
  也不能同步进入可能打开 PortAudio 的应用状态机。重复音频通知进一步放大
  了开关竞态。
- `BUG-010`：设置窗口正确写入 `config.json`，运行中桥却只按 mtime 热加载
  `key_bindings.json`；语音生命周期和快捷键只在进程启动时读取。
- `BUG-011`：按键检测请求可能先被 `AudioStarted` 或 ATVV mic 抢跑，后到
  F5/HID 虽能让界面显示 `mic`，正式语音状态却已经被改变。
- `BUG-012`：TOGGLE 在 `AudioStopped` 时立即续开，但真机实体 F5 约 108
  毫秒后才 up；设备回复续流开始却不发送 PCM。
- `BUG-013`：一次性检测的并发来源先读取请求再争锁；Windows 共享冲突可让
  锁胜者和败者同时失败，设置页得不到结果。

本批次范围：

- 只修复 RC003 的音频路由、语音触发配置独立性、HID tap 状态与设置页
  按键检测链路。
- 保留现有 BLE、ATVV、Raw Input、SendInput、Frida 和 VB-CABLE 架构。
- Quicker 仅保留为未来可选动作出口，本批次不做深度集成。
- 不把 DJI Mic 2 入口扩展为新的开发主线。

关联记录：

- `bugs/BUG-001-audio-endpoint.md`
- `bugs/BUG-002-hid-tap-readiness.md`
- `bugs/BUG-003-duplicate-bridge-dialog.md`
- `bugs/BUG-004-background-bridge-tray.md`
- `bugs/BUG-005-toggle-continuous-voice.md`
- `bugs/BUG-006-live-bridge-key-detection.md`
- `bugs/BUG-007-duplicate-winrt-ble-candidates.md`
- `bugs/BUG-008-toggle-multi-source-gesture.md`
- `bugs/BUG-009-f5-hook-release-race.md`
- `bugs/BUG-010-live-voice-settings-reload.md`
- `bugs/BUG-011-mic-key-detection-voice-race.md`
- `bugs/BUG-012-toggle-reopen-before-physical-release.md`
- `bugs/BUG-013-key-detection-claim-race.md`
- `bugs/BUG-014-toggle-reopen-f5-echo.md`
- `bugs/BUG-015-sogou-codex-text-commit.md`
- `TESTING.md`

实施结果：

- 音频端点现在明确拒绝 `Windows WDM-KS`，同名端点优先使用
  `Windows WASAPI`，其次使用 `Windows DirectSound`，保存前执行真实
  `open/start/stop/close` 预检。
- 语音生命周期与宿主快捷键已分开保存，“按住说话/按一下切换”不再覆盖
  用户录制的快捷键。
- HID tap 改为隐藏独立子进程，保留目标 PID、WUDFHost 名称和固定 Gadget
  哈希校验，并把启动到有效 HID IO 的状态写入 `app.log`。
- 管理员现场诊断确认 WUDFHost 进程名查询必须发生在
  `SeDebugPrivilege` 启用之后；注入器现已按此顺序执行。同一宿主 PID
  注入失败后保持稳定失败状态，只在 PID 改变后重试。
- 设置页的按键检测同时监听 Raw Input 与 HID tap；任一路径捕获后都会统一
  停止监听，检测期间不会执行已配置动作。
- 设置页发起的桥接子进程使用隐藏来源标记；重复实例立即以退出码 3 返回，
  不再被模态提示框阻塞并误判为启动成功。
- 已保存 WDM-KS 配置且检测到标准 CABLE Input 时，设置页预选 WASAPI
  视图，等待用户保存并通过真实预检后才持久化。
- 后台桥接新增原生 Windows 通知区域图标；双击打开设置，右键可打开设置
  或正常退出。退出请求取消 asyncio 运行任务，并继续使用原有 BLE、HID、
  语音热键、音频和互斥锁清理路径。
- 切换语音改为真正的第一次按下开启、第二次按下关闭；第一次物理松开后
  续发 `MIC_OPEN`，第二次按下发送线程安全 `MIC_CLOSE`，并用 close-pending
  门闩隔离关麦竞态。按住模式保持不变。
- 设置页在后台桥接运行时改走一次性本地文件 IPC；后台捕获下一键、回传
  逻辑 `button_id` 并吞掉该次 down/up，不执行映射。没有后台桥接时仍走
  原本的本地 Raw Input + HID tap。
- 麦克风触发增加跨来源手势门闩；首个 HID、legacy F5、ATVV mic 或
  `AudioStarted` 判定一次切换，后到来源只并入同一手势，`AudioStopped`
  后下一轮按键才可再次改变状态。
- 手势门闩增加音频停止与全部物理 up 的双边界；重复音频 start/stop 去重；
  低层 F5 回调改为事件循环异步投递，并用连接 generation 丢弃清理前排队
  的旧事件。切换关闭不再重新打开播放端点。
- 桥接同时监控 `config.json` 和 `key_bindings.json`。新语音生命周期与快捷键
  在空闲时立即热应用，活跃会话则暂存到完整收尾后应用；BLE-only
  `AudioStarted` 路径也会刷新设置。日志记录实际生效的模式与快捷键。
- 同轮完整代码审查修复了三个确定性问题：PortAudio `close()` 失败时保留
  资源句柄以便重试；隐私禁用字段按大小写不敏感校验；开始菜单环境变量缺失
  时不再误扫当前工作目录。
- 麦克风按键检测改用独立跨来源门闩，检测期间不会改变正式语音状态。
- TOGGLE 续开改为待全部实体来源 up 后执行；BLE-only 使用 200 毫秒有界
  宽限，第二次关闭、清理和重连均取消待续开。
- 一次性按键检测先独占 claim lock 再读取和移动请求；写结果失败时恢复请求，
  避免并发来源同时认领失败。

自动验证证据：

- 音频定向测试：284 项通过，2 项平台相关跳过。
- HID、入口、Qt 与应用定向测试：164 项通过，1 项平台相关跳过。
- 托盘、启动器、应用与入口定向测试：90 项通过，1 项平台相关跳过；原生
  托盘线程 start/stop smoke test 通过且无 `ResourceWarning`。
- `fix6` 最终完整测试：990 项通过，7 项安全或平台相关跳过，退出码 0。
- `fix7` 手势合并完整测试：993 项通过，7 项安全或平台相关跳过，退出码 0。
- `fix8` 释放竞态与低层钩子完整测试：998 项通过，7 项安全或平台相关跳过，
  退出码 0。
- `fix8` 公开边界扫描：通过，扫描 226 个文件；`compileall` 与
  `git diff --check` 通过。
- `fix9` 语音设置热加载与同轮审查完整测试：1006 项通过，7 项安全或平台
  相关跳过，退出码 0；公开边界扫描 227 个文件通过；`compileall`、
  `pip check` 和 `git diff --check` 通过。
- `fix9` PyInstaller 构建成功；冻结程序 `--dry-run` / `--help` 均返回 0，
  无效 HID 注入 PID 入口返回 4。
- PyInstaller 候选构建：成功；冻结程序 `--dry-run` 和 `--help` 退出码均为
  0，隐藏注入器对无效 PID 稳定返回退出码 4。
- 冻结重复启动实测：已有桥接运行时，设置来源子进程在 256 ms 内返回
  退出码 3，没有显示阻塞设置页轮询的模态提示框。
- 最新候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260820-fix8/`。
- `fix8` 候选 EXE SHA-256：
  `E9FF471EF55CB8A7713DC934155D30D207D69278FA9550C116987E21B0EB10AB`。
- `fix8` 便携 ZIP SHA-256：
  `2148F4F25C56E2117FEA9FC80A9A460BA0905CD3B09442CAD6CCB5C8260089FA`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- 最新候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260820-fix9/`。
- `fix9` 候选 EXE SHA-256：
  `DB3F49E23F2B6EEB6C51B9B0B45C5A2E1B29EEEA2E8FA97FFB999A49A3E91C28`。
- `fix9` 便携 ZIP SHA-256：
  `F24AFA07020385276297537E6B3F3F49880A305BC019460D3849778835CC778C`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- 2026-08-21 完整代码审查修复提交：`eafd203`；检查范围与结论见
  `reviews/2026-08-21-full-code-audit.md`，项目构成见
  `WINDOWS-ARCHITECTURE-LEDGER.md`。
- `fix10` 构建时 Git HEAD：`041c125`；完整测试 1090 项通过、7 项安全或
  平台条件跳过，公开边界扫描 228 个文件通过，PyInstaller 构建成功。
- `fix10` 冻结程序 `--dry-run` / `--help` 均返回 0，无效 HID 注入 PID
  入口返回 4。
- 最新候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260821-fix10/`。
- `fix10` 候选 EXE SHA-256：
  `B499B7C665115B128FFAF49F80A9A425B82969157A4F0C6EA2ABCFA784BA15B2`。
- `fix10` 便携 ZIP SHA-256：
  `69B3F76BB2590C736CFEE6359AADE5EB8848DB2937D7FDC8CDAF404F70708799`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- `fix10` 本机逐键检测确认 13 个按钮全部可识别；日志同时证明麦克风检测的
  `AudioStarted` 会早于 F5/HID 回调并误开持续空语音会话。故障、证据和修复
  边界见 `bugs/BUG-011-mic-key-detection-voice-race.md`。
- `fix11` 麦克风检测隔离代码提交：`4d6a0fe`。应用接线测试 81 项通过、1 项
  平台相关跳过；完整测试 1093 项通过、7 项跳过；公开边界扫描 229 个文件
  通过；`compileall`、`pip check` 和 `git diff --check` 通过。
- `fix11` 构建时 Git HEAD：`c135991`；PyInstaller 构建与冻结入口检查通过，
  `--dry-run=0`、`--help=0`、无效 HID 注入 PID `=4`。
- 最新候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260821-fix11/`。
- `fix11` 候选 EXE SHA-256：
  `4639AC9942C3D66525ED8EC7457F1EA7562E1B9DBD14ED5E5FD05BB2CB44ED7D`。
- `fix11` 便携 ZIP SHA-256：
  `DF9F50C4B9F11D703FA65BFE200A52FBD953B7591670FA3AEF89B6C96985127B`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- `fix12` 代码修复提交：`f6ae3c3`、`cfbca35`；应用接线测试 83 项通过、
  1 项平台相关跳过；检测与应用扩展定向测试 90 项通过、1 项跳过；原并发
  失败场景压力运行 100 次无失败；完整测试 1096 项通过、7 项跳过；公开
  边界扫描 231 个文件通过。
- `fix12` 构建时 Git HEAD：`62b1032`；PyInstaller 和冻结入口检查通过。
  候选目录为源码仓库同级
  `RemoteMicRC003-0.1.0-candidate-20260821-fix12/`；EXE SHA-256 为
  `7B99267D5E1385408CDBAAE36BA422FAACC43B50F16E47BF3E5EEC8176BB0926`，
  便携 ZIP SHA-256 为
  `D6B9168B728E12FD0D892BD472547007F0A8B5E835F34D35230677E76BF5CBAA`。
  本机短按持续语音复测尚未完成。
- 冻结桥接实测：进程日志记录通知区域图标 ready 和 started，随后完成
  RC003 BLE/ATVV 能力连接。用户从托盘打开设置后再选择“退出桥接”，桥接
  进程消失而设置窗口继续保留；日志记录 HID tap stopped、BLE/HID/音频
  清理和 graceful bridge exit，通知区域生命周期验收通过。
- `fix4` 管理员冻结桥接实测：HID tap 从 `injecting` 进入
  `waiting_for_gadget_connection` 和 `attached_waiting_for_hid_io`，确认已
  越过此前的权限失败；当前 CABLE Input / Windows WASAPI 配置再次通过
  真实音频端点预检。两项结果仍不能替代实体逐键和非零 PCM 语音验收。
- `fix4` 后续真机结果：HID tap 已进入 `ready/hid_io_verified`；普通映射在
  记事本中有效；按住说话记录到约 2.4 秒非零 PCM 并产生可见识别文字。
  同次复测也确认旧切换模式仍只有极短音频，且从托盘打开设置后的按键检测
  失效。这两项分别由 `BUG-005`、`BUG-006` 的 `fix5` 修复，尚待用户复测。
- `fix6` 异机结果：多候选探测得到唯一可用设备并完成 ATVV 能力连接；所有
  按键均能在映射页识别；长按会话达到约 3.6 秒非零 PCM、
  `result=signal` 并产生识别文字。切换模式的短按仍因 `BUG-008` 在约
  180 至 360 毫秒内误关，不能据此宣称切换模式通过。
- `fix7` 异机结果：首轮同手势来源能够被忽略，部分会话达到
  `result=signal`，但音频停止后延迟 HID 会重新关闭；低层 F5 回调等待语音
  锁约 1.5 秒，用户同时报告记事本偶发插入日期时间。`fix7` 不通过真机
  稳定性门槛，后续由 `BUG-009` / `fix8` 处理。
- `fix8` 异机结果：全部普通按键可在映射页识别；按住语音得到约 2.2 秒
  `result=signal` 的 PCM 并产生可见识别文字；记事本未再插入日期时间，
  证明 `BUG-009` 的 F5 防漏路径生效。两种模式仍表现相同，日志持续记录
  `voice toggle remains active`，后续由 `BUG-010` / `fix9` 处理。

基础功能实现提交：`88ea7a90144ff078b7abc62de9dedc4290043fe2`。

重复启动与旧端点迁移提交：
`b0e18b48ede974b14bc5765a9a1b82cb23b5a7e0`。

后台桥接通知区域控制提交：
`dad113ce992ea34af7dc3f74ba797543c8adc77c`。

HID 注入权限顺序与稳定失败提交：
`9daf49fc8fae1ecc5824fca4360ca0a6f968c55b`。

持续语音与桥接运行时按键检测提交：
`ca5b69885a3796901dc36f1578cb7a754d73f140`。

仍未完成：最新候选的 HOLD/TOGGLE 区分、13 键和 F5 防漏回归、断连重连、
休眠恢复、长期运行与杀软兼容验收。历史候选上的多候选连接、设置页逐键检测、
按住说话基础语音链路和 F5 防漏结果不能自动继承给后续候选。

### 2026-08-21 语音设置界面整合（fix13）

来源与边界：

- 起点为已冻结的 `fix12` 代码与记录；语音时序修复、检测 IPC 和真机结论不在
  本批次重写，也不把界面变化混入 `fix12` 候选身份。
- 用户反馈连接页和按键映射页同时存在语音相关设置，难以判断两处各自作用；
  本批次只整理设置模型、界面归属和保存体验，不扩展 Quicker 或 DJI 功能。

实现：

- 代码提交 `459a0bd` 把语音设置集中到 `ButtonsPage.qml`，连接页只保留设备、
  桥接和音频端点。
- “按一下切换”和“按住说话”各有独立的宿主快捷键与录制入口；
  `voice_hotkeys` 保存两套值，现有 `voice_hotkey` 继续作为桥接运行时当前值，
  因而不需要同时改动已验证的桥接读取接口。
- 旧配置加载时，当前模式的原 `voice_hotkey` 仍为权威值；缺少的另一套快捷键
  使用内置默认值补齐。保存、恢复默认和模式切换均有自动测试覆盖。
- 麦克风映射卡保持只读语音语义，显示当前方式和当前快捷键，避免第三个重复
  编辑入口。

自动验证：

- 配置、设置帮助器、Qt 控制器和 QML 定向测试 174 项通过。
- 完整测试 1102 项通过，7 项安全或平台相关跳过，退出码 0。
- 公开边界扫描 231 个文件通过；`compileall`、`pip check` 和
  `git diff --check` 通过。
- 构建时 Git HEAD 为 `63b038d`；PyInstaller 构建与冻结入口检查通过：
  `--dry-run=0`、`--help=0`、无效 HID 注入 PID `=4`。
- 候选目录为源码仓库同级
  `RemoteMicRC003-0.1.0-candidate-20260821-fix13/`；EXE SHA-256 为
  `DB7C357419DACDC04491C50CF3BD5AFF4353467B30BE3B5E73E5C3012EC34CA6`，
  便携 ZIP SHA-256 为
  `B496F8DEED84DC7D8A2B28E711931EDEC11BC169A93771F56732A6339849D4FE`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- 状态：源码与冻结检查点通过；保存后重开和 RC003 语音真机回归待完成。

### 2026-08-21 切换续开 F5 回声（fix14）

- `fix13` 本机日志确认实体释放后已经成功续发 `MIC_OPEN`，但约 60 毫秒后又
  出现 legacy F5 down/up；旧状态机把它当作第二次点击，立即关闭并产生
  `frames=0 samples=0 result=empty`。
- 代码提交 `bfd514d` 在 TOGGLE 主动续开后增加 250 毫秒有界回声保护，按
  来源成对吞掉保护期内的物理 down/up；保护期后的真实第二次点击仍正常关闭。
- 应用接线测试 84 项通过、1 项平台相关跳过；完整测试 1103 项通过、7 项
  跳过；公开边界扫描 231 个文件通过；`compileall`、`pip check` 和
  `git diff --check` 通过。
- 同轮“记事本可上屏、Codex 不上屏”不能由本程序日志直接归因；识别文字由
  搜狗持有，需用键盘手动触发同一快捷键区分宿主控件兼容与本程序焦点时序。
- 构建时 Git HEAD 为 `f5c86b9`；构建门禁完整测试 1103 项通过、7 项跳过，
  公开边界扫描 233 个文件通过；PyInstaller 与冻结入口检查通过。
- 候选目录为源码仓库同级
  `RemoteMicRC003-0.1.0-candidate-20260821-fix14/`；EXE SHA-256 为
  `E0E3A7A36D6F07A1E1893E6B2A2750240EE0C78056EE5DC6DCE331227C98D24D`，
  便携 ZIP SHA-256 为
  `59DA357C40BDA873AF7FF06F4CFFFC68D1E5EB56FEC4BE37B3CE3BAA453FCDB5`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- 状态：代码、自动检查与冻结候选通过，本机真机复测待完成。

### 2026-08-21 统一语音主映射（fix15）

来源与边界：

- 起点为 `fix14` 记录提交 `e59438b`。用户决定顶部只维护开关型和按住型两套
  宿主快捷键，实体键仍在统一映射列表中自由选择普通动作或语音动作；麦克风键
  不再锁死。Quicker、DJI 和完整语音协议重写不在本批次。
- 检查点提交为 `979ed49`，最终代码提交为 `eaabde8`，界面/架构说明提交为
  `8624ceb`。保留检查点而不改写历史，便于追溯中途接管与最终审查修复。

实现与审查修复：

- 新增显式 `VOICE_TOGGLE` / `VOICE_HOLD` 主动作。13 个实体键均可选择普通、
  开关语音或按住语音，但保存层只允许 0 或 1 个语音主映射；双击/长按拒绝
  语音动作。旧 generic `VOICE` 按原 `voice_trigger_mode` 迁移。
- 语音主映射期间，已有普通双击/长按设置保留、隐藏并暂停执行；切回普通主
  映射后恢复。零语音映射允许两套语音快捷键为空，并跳过无意义的音频端点预检。
- 运行时按事件来源合并非麦克风语音边沿，并延迟到映射键完整释放后执行切换
  续开；HID tap 失效时强制释放其遗留来源。麦克风键改作普通动作后，同样合并
  重叠和迟到的 HID/legacy F5 边沿，避免一次实体操作执行两次。
- 活跃语音或普通麦克风手势期间的配置热更新延迟到完整收尾；清理失败时保留
  pending 配置。损坏的热加载文件会立即关闭旧 F5 语音转换快照，修复并成功
  加载前保持失败关闭。PCM 只在已授权语音会话中写入选定端点。
- QML 顶部只显示两套快捷键；照片热点颜色跟随当前语音主映射；录制窗口明确
  区分输入法语音快捷键与普通键盘映射。1024x720 离屏截图保存在本地研究目录，
  不进入公开仓库。

自动与冻结验证：

- 应用接线测试 108 项通过、1 项平台相关跳过；设置帮助器与 Qt 设置定向测试
  149 项通过；完整测试 1143 项通过、7 项安全或平台相关跳过，退出码 0。
- 1024x720 离屏 `main.qml` 加载无 QML warning，真实输入并直接保存普通组合键
  的探针通过；公开边界扫描 266 个文件通过；`compileall`、`pip check` 和
  `git diff --check` 通过。
- 构建时 Git HEAD 为 `8624ceb`；Frida 17.15.3 与 VB-CABLE Pack45 固定资产
  校验通过，PyInstaller 构建成功。复制后的候选入口 `--dry-run=0`、`--help=0`、
  无效 HID 注入 PID `=4`。
- 候选目录为源码仓库同级
  `RemoteMicRC003-0.1.0-candidate-20260821-fix15/`；EXE 大小 5,048,557 字节，
  SHA-256 为
  `FE9B15C484DCF5E7DCBC2472DA3181587344AB4ABA3F942FC2C972B20475D61D`。
- 便携 ZIP 为
  `RemoteMicRC003-0.1.0-candidate-20260821-fix15-portable.zip`，SHA-256 为
  `6A24FA1023F5E4C5AC4FFCE68C43B2334286851A69CB7C87E00F1C1E60EF7DF`；
  ZIP 只有一个版本顶层目录，内部 EXE 已重新计算并与候选目录一致。同批次
  `RemoteMicRC003-0.1.0-candidate-20260821-fix15-SHA256SUMS.txt` 覆盖 EXE 与 ZIP。
- 状态：代码、自动检查与冻结候选通过；统一界面、麦克风键普通映射、两种语音
  生命周期和非麦克风键持续 ATVV PCM 仍须按 `TESTING.md` 真机复测，不能据此
  声称已完成真实硬件验收。

## 维护纪律

- 每次修改都应写明：上游起点、故障现象、根因、改动文件、自动检查、
  人工验收和对应提交。
- 自动测试通过但真实 RC003 尚未复测时，状态只能写“检查点待实测”。
- 不记录真实蓝牙地址、个人绝对路径、凭据或用户语音内容。
- 发布或分发派生构建时继续遵守 GPL-3.0-only，并保留
  `ATTRIBUTION.md`、`COPYRIGHT.md` 和第三方通知。
