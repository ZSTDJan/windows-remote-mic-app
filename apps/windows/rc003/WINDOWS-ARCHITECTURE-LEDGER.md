# RC003 Windows 项目认知与构成原理总台账

当前认知基线：2026-08-22，完整源码检查点 `eafd203`，统一按键/语音映射
检查点 `eaabde8`，全局设置框架检查点 `addc2b0`，BLE 音频停止排序检查点
`a189a1c`，On-request 专项探针检查点 `dc82e1a`。
其中 On-request 检查点只用于追溯已完成的硬件边界调查，专项程序当前已经撤下。

## 文档定位

本文是 Windows RC003 派生客户端的项目认知主入口。它依据最近一次完整源码
检查、现有测试、构建安装链和已经取得的真机反馈，沉淀以后接手项目时仍然
需要知道的背景与原理，主要回答：

1. 项目为什么存在，当前真正要解决什么用户问题；
2. 软件由哪些进程、线程、模块和第三方组件组成；
3. 实体按键动作与语音怎样从遥控器到达 Windows 应用；
4. 配置、日志、构建产物和运行资源分别由谁管理；
5. 哪些能力已有证据，哪些仍需要当前构建的真实硬件验证。

本文不承担逐次修改流水、单个故障排查过程或发布包哈希记录。具体故障经过见
`bugs/`，维护批次见 `MAINTENANCE.md`，发布变化见 `CHANGELOG.md`，单次完整
检查证据见 `reviews/`。这些材料解释“发生过什么”，本文只保留仍影响当前
项目理解的结论。

发生冲突时，源码和配置格式决定实现事实，Git 决定代码基线，当前构建的哈希
决定产物身份，当前实机记录决定硬件结果；本文应随这些权威来源更新，而不能
反过来覆盖它们。自动测试结果和历史构建的真机结果也不能自动继承给新构建。

## 1. 项目背景与当前目标

RC003 原本是面向电视/机顶盒的蓝牙语音遥控器。Windows 可以把它的一部分
按键当作 HID 键盘输入，但不会自然把它的 ATVV 蓝牙语音流暴露成普通麦克风；
部分 HID usage 还会在 Windows 键盘栈中丢失。因此，这个项目不是简单的
“快捷键映射器”，而是一座同时连接 BLE 语音、HID 按键、Windows 输入注入和
音频端点的本地桥。

当前目标用户是希望把小米蓝牙遥控器 2 Pro / RC003 当作 Windows 无线遥控器
和语音入口的人。项目当前优先解决四个结果：

- 13 个实体按钮可以稳定识别、检测并映射为 Windows 动作；
- 实体话筒键不仅能唤起宿主语音界面，还能把实际语音送到宿主选择的输入链；
- “按住说话”是 RC003 唯一正式语音生命周期；最后一条 On-request 真机探针
  未收到启动信号后，开关型运行路径和专项程序已经撤下；
- 后台桥接可见、可退出、可重启，并能在失败时留下不泄露设备身份的诊断证据。

当前阶段是“完整源码检查和本机候选构建完成、最新候选待真机复测”。基础按键与
语音可靠性是当前主线。Quicker 只被视为未来可选的动作出口，不做深度互融，
也不属于本阶段的完成条件。

## 2. 用户工作流

1. 用户先在 Windows 中配对 RC003，并安装或选择需要的音频路由；
2. 在设置窗口中选择 RC003 和语音输出端点，维护一套按住说话快捷键；实体
   话筒键可选择按住说话或普通动作，其他按键只选择普通动作或组合键；
3. 保存并启动后台桥接，之后设置窗口可以关闭，桥接继续在通知区域运行；
4. 按钮经 HID/Raw Input 识别后执行主映射；普通动作进入 Windows 动作分发，
   实体话筒键的按住说话动作同时驱动对应快捷键和 BLE/ATVV 音频会话；
5. 用户从通知区域重新打开设置或正常退出桥接；异常断连由监督器清理后重连。

典型语音路由为：RC003 -> BLE/ATVV -> Remote Mic 解码与播放 -> `CABLE Input`
-> VB-CABLE -> `CABLE Output` -> 输入法或语音应用。VB-CABLE 是可选外部驱动，
不是程序自动安装或静默配置的内置麦克风。

## 3. 当前范围与非目标

当前范围：

- Windows 10/11 64 位上的 RC003 配对、连接、13 键识别与动作映射；
- ATVV 语音接收、ADPCM 解码、PortAudio 输出和可选 VB-CABLE 路由；
- 实体话筒键 HOLD 生命周期、一套宿主快捷键和可选松手收尾动作；
- 设置、诊断、托盘控制、便携包、安装器和 Windows 自动检查。

当前非目标：

- 不推倒重写已经存在的 BLE、ATVV、Raw Input、Frida、SendInput 和音频链；
- 不把 Quicker 变成核心依赖，也不做双向深度集成；
- 不把 DJI Mic 2 的只读设置入口扩展成另一条硬件开发主线；
- 不自动改变系统默认音频设备，不静默提权、安装驱动或绕过安全软件；
- 不把未签名候选、自动测试或旧候选的结果描述成全面发布验收通过。

## 4. 来源、演进基线与许可

- Remote Mic 主项目调研快照：`HD838A/remote-mic-app` 的 `main` 在
  2026-08-20 核验为 `4d526175817ad2c4c5fbe1650d528c946a3cdbf3`，主要维护
  macOS，不是本 Windows 客户端的直接代码基线。
- Windows 派生仓库：`miaomiaozii/windows-remote-mic-app`。
- Windows 派生起点：`271ed7947eec19c4c691ed3ba97f338461be8051`；内部实现
  基于 GPL-3.0-only 项目 `nijez/open-voice-bridge` 的 Windows RC003 客户端。
- 最近完整源码检查点：`eafd203`；该提交证明代码修改与自动检查基线，不代表
  最新冻结包已经完成真机验收。
- 当前维护主线：小米蓝牙遥控器 2 Pro / RC003 的 Windows 客户端。
- 协议与 HID tap 参考：GPL-3.0-only 项目
  `xxb26553663-star/remote-bridge-hub` 的提交
  `8a93f321ac71a602300c6cd77f7256fa4b63068e`；具体归属见
  `THIRD_PARTY_NOTICES.md`。
- 本仓库许可：GPL-3.0-only。PySide6、Frida、PortAudio、NumPy、WinRT 投影、
  VB-CABLE 等仍按各自许可和通知分开处理。
- Quicker 不是核心运行依赖。未来只需要新增一个动作出口，不应反向侵入
  BLE、HID 或语音状态机。
- DJI Mic 2 只是设置页中的独立只读设备入口，不复用 RC003 桥接链路。

## 5. 统一术语

| 术语 | 本文含义 |
| --- | --- |
| RC003 | 小米蓝牙遥控器 2 Pro；本项目当前唯一桥接主设备 |
| 设置进程 | 打开 Qt Quick 设置窗口的 `RemoteMicRC003.exe` 角色 |
| 桥接进程 | 后台持有 BLE、按键、音频和托盘资源的 `--bridge` 角色 |
| HID/按键链 | 从 Windows HID、Raw Input 或 HID tap 到逻辑按钮和映射动作的链路 |
| ATVV/语音链 | 从 BLE GATT 控制与音频特征到 PCM 输出端点的链路 |
| 宿主 | 最终接收快捷键和虚拟麦克风输入的输入法或语音应用 |
| HOLD | 实体话筒键按下时发送宿主 key-down，物理松手时发送 key-up；`AUDIO_STOP` 是缺少松手边沿时的释放兜底 |
| 历史 TOGGLE | 已撤下的开关型实验路径；仅保留配置迁移和 `BUG-017` 调查证据，不再是可选能力 |
| 松手收尾 | 默认关闭；完成 HOLD key-up 后，再有界补发一次同一宿主快捷键，用于关闭仍停留的宿主窗口，不延长遥控器传声 |
| 候选构建 | 未签名、需要绑定源码提交和哈希、尚可能等待真机验收的包 |

内部 Python 包名 `ovb_rc003` 是为了减少与上游同步时的差异，不是另一个产品。

## 6. 可执行程序角色

冻结包只有一个 `RemoteMicRC003.exe`，参数决定角色：

| 入口 | 角色 | 是否持有硬件资源 |
| --- | --- | --- |
| 无参数、`--settings` | Qt Quick 设置窗口 | 不长期持有；本地按键检测和音频预检时短暂持有，BLE 诊断委托子进程 |
| `--bridge` | 后台桥接进程 | 持有 BLE、Raw Input、HID tap、音频和托盘 |
| `--dry-run` | 模块导入检查 | 否 |
| `--help` | 帮助文本 | 否 |
| `--diagnose-ble-candidates <result>` | 隐藏的有界 BLE 诊断子进程 | 短暂持有 WinRT BLE 枚举资源 |
| `--rc003-hid-injector --pid ...` | 隐藏的受限注入子进程 | 短暂持有目标进程句柄 |

源码运行使用 `python -m ovb_rc003`。PyInstaller 不直接分析包内
`__main__.py`，而从顶层 `src/launcher.py` 做绝对导入，避免冻结入口失去
包上下文。

后台桥接由 per-session Windows named mutex 保证单实例。设置窗口可以多次
打开，但第二个桥接不能越过单实例保护。桥接存活时通知区域图标提供“打开
设置”和“退出桥接”；关闭设置窗口本身不会结束后台桥接。

`--bridge-from-settings` 只是设置窗口启动 `--bridge` 时附带的隐藏来源标记，
用于让重复实例静默返回确定退出码，不是独立运行角色。BLE 诊断另起子进程，
是为了在 WinRT 调用卡住时仍能由父进程确认终止，不把不可取消的原生调用留在
设置进程中。

## 7. 运行拓扑

```text
RC003 遥控器
  |-- BLE GATT / ATVV --------------------------+
  |                                             |
  |-- Windows HID 键盘栈 --> Raw Input --------+--> RC003App
  |                                             |      |
  +-- HidOverGatt read --> WUDFHost + Gadget ---+      |-- 按键动作 --> SendInput / 应用启动
                                                        |
                                                        +-- ADPCM --> PCM --> PortAudio
                                                                            |
                                                                            +--> CABLE Input
                                                                                  |
                                                                                  +--> 宿主语音输入

设置进程 -- config.json / key_bindings.json -- 后台桥接热加载
设置进程 -- 一次性 key-detection 文件 IPC ---- 后台桥接捕获下一键
后台桥接 -- app.log --------------------------- 设置页/用户诊断
```

这里有两条相互独立、在主映射解析为语音动作时汇合的数据链：

- HID/按键链负责判断按下了哪个实体按钮，并执行 Windows 动作；
- BLE/ATVV 链负责打开遥控器麦克风、接收压缩音频、解码并播放到选定端点。

实体麦克风键可能同时从 ATVV、Windows F5、Raw Input 和 HID tap 到达。因此
`app.py` 有跨来源手势门闩，必须把同一次实体按下合并成一次动作；该动作可以
是语音，也可以是用户为麦克风键配置的普通映射。来源边沿可能重叠，也可能在
首个来源已经释放后迟到；两种情况都必须在有界保护期内折叠，不能把迟到来源
误判为下一次点击。

## 8. 按键与动作映射链路

### 8.1 输入来源

`raw_input_windows.py` 注册并读取 Windows Raw Input，解析标准键盘事件并
匹配 RC003 设备。Windows 键盘栈会丢掉返回、音量加、音量减等部分 usage，
所以 `frida_compat.py` 提供可选 HID-over-GATT tap 补齐缺失报告。

HID tap 的 loopback TCP 客户端是运行在已核验目标 `WUDFHost.exe` 内的
Frida Gadget。服务端通过 `GetExtendedTcpTable` 核对 TCP 客户端进程 PID；
无法确认或 PID 不等于刚核验过的 WUDFHost 时，连接会在读取任何消息前关闭。
日志只写固定状态，不持久化端点、PID、设备路径或地址。

### 8.2 去重与原生按键抑制

同一个按键可能同时被 HID tap 和 Windows 原生键盘路径报告。如果两条路径都
继续传播，用户会同时得到原生字符/功能和映射动作。

- `legacy_key_suppressor_windows.py` 安装低层键盘钩子；
- HID tap 或 Raw Input 根据已知 usage 提前 arm 一个待吞掉边沿；
- 低层钩子本身看不到设备身份，只在短时间窗内吞掉与 arm 条目键值、扫描码、
  扩展位和按下/释放状态都匹配的非注入边沿；
- 应用随后只执行一次映射动作。

麦克风的原生 F5 是特殊路径：部分 RC003/Windows 组合只把麦克风键暴露成
无法关联设备来源的 legacy F5。桥接运行时，专用钩子会吞掉非注入 F5，避免
它泄漏给记事本或输入框并触发“插入日期时间”。麦克风键映射为语音时，符合
当前手势条件的边沿进入对应语音状态机；映射为普通动作时，经跨来源去重后进入
普通手势分发。代价是桥接钩子启用期间，用户键盘上任何非注入的真实 F5 同样
会被吞掉；这是当前已知架构边界，不能描述成钩子直接识别了 RC003 设备来源。

### 8.3 物理签名、手势与动作

`raw_input_windows.physical_signature()` 把可移植的键盘/HID字段组成签名；
签名不包含设备接口路径。`physical_bindings` 可把未知签名学习为逻辑按钮。

`button_gesture.py` 在单击、双击、长按和可重复按住之间做纯状态机判定。
连接清理会递增 generation 并取消定时器，旧连接的延迟回调不能在重连后产生
动作。

`key_mapping.py` 保存语义动作，而不是界面文字。`app.py` 再把语义动作交给：

- `win32_input.py`：批量 `SendInput` 普通键、组合键和媒体键；
- `action_executor.py`：不经过 shell，以参数数组启动已解析应用；
- 专用语音状态机：只接受实体话筒键的 `VOICE_HOLD` 主动作，即时处理按下与
  释放边沿，不等待双击或长按判定。

麦克风键配置为普通动作时仍可使用单击、双击和长按。只有方向、退格和音量
语义动作允许按住重复；自定义组合键无论放在方向键还是其他键上，都只执行
一次完整点按，避免定时重放修饰键组合。

配置迁移先执行产品边界归一化。旧开关型、非话筒键语音或次级手势语音会把
对应整键标记为停用，连同该键的单击、双击和长按一起停止执行，直到用户在
界面中重新选择并保存；不会悄悄转换成按住说话。

如果 `SendInput` 部分成功且补偿 key-up 也失败，应用会保留“仍欠释放”的
按键集合。下一次动作前必须先完成安全释放；完成前不会继续注入新动作。

## 9. 语音链路

### 9.1 BLE 连接与 ATVV 协议

`ble_transport_winrt.py` 使用 WinRT 枚举已配对 BLE 设备，并通过 UNCACHED
GATT 查询连接 ATVV voice service。若 Windows 返回多个同名候选，只在恰好
一个候选通过实际 ATVV service 探测时连接；零个或多个可用都失败关闭，不按
显示名称或枚举顺序猜测。

连接后主要特征为：

- TX：主机发送 capabilities、`MIC_OPEN`、`MIC_CLOSE`；
- CONTROL：设备发送 capabilities、麦克风按钮、音频开始/停止；
- AUDIO：设备发送 ADPCM 帧。

控制与音频分别进入有界队列。控制队列溢出是协议状态不可证明的硬错误，会
停止 worker 并请求重连；音频队列满时只丢最旧音频并计数，不能让音频洪峰
挤掉关麦等控制事件。两个队列的通知共享单调序号，序号分配与入队由同一个
producer lock 保护。一般控制仍优先，但 `AUDIO_STOP` 必须先排空同一连接代、
序号更早的音频；否则 decoder 会先关闭并把已经收到的尾包误判为晚到数据。

### 9.2 解码、统计与输出

`atvv_protocol.py` 实现 capabilities、命令与 IMA/DVI ADPCM 解码；
`atvv_session.py` 管理 session id、decoder reset、sync、晚到音频保护和
隐私安全 PCM 统计。统计只包含帧数、样本数、峰值、能量、过零等数值，不
保存或转写语音内容。

`audio_playback.py` 使用 `sounddevice`/PortAudio 打开用户明确选择的输出
端点：

- WDM-KS 被拒绝；
- 同名端点优先 WASAPI，其次 DirectSound；
- 先真实 open/start/stop/close 预检，再允许设置保存；
- 16 kHz 单声道 PCM 按端点能力转换采样率和声道；
- 流关闭失败时保留对象引用，后续清理继续重试。

通常输出端点选择 `CABLE Input`，VB-CABLE 再把它暴露为录音端点供输入法或
语音应用使用。软件不会静默安装驱动；设置页只能在用户明确确认后启动随包、
哈希校验通过的厂商安装程序。

### 9.3 宿主语音动作

`voice_controller.py` 只决定状态，不直接操作 Windows：实体话筒键按下产生
key-down，物理松手产生 key-up；如果 Windows 没有提供松手边沿，设备的
`AUDIO_STOP` 负责同一个 key-up 兜底。重复松手或重复 `AUDIO_STOP` 不会发送
第二个 key-up。

开关型实验的最后一条 On-request 真机诊断已经完成，协商成功后仍未收到实体
`START_SEARCH`。因此产品运行路径、设置项和专项程序均已撤下；协议常量与
`BUG-017-toggle-firmware-audio-boundary.md` 继续保留为历史调查证据，不代表
当前软件提供开关型语音。

`app.py` 强制顺序为：

1. 验证 BLE session 存在；
2. 释放任何历史遗留热键；
3. 开始语音时先验证并打开播放端点；
4. 先向宿主交付热键；
5. 只有宿主 key-down 成功后才允许向设备发送 `MIC_OPEN`。

默认关闭的松手收尾选项只在本次 key-down 成功后取得资格。物理松手先完成
key-up；若随后收到 `AUDIO_STOP`，约 120 毫秒后补发一次本次会话快捷键；若
没有收到，约 800 毫秒后最多兜底一次。新话筒按下、设置变化、断线或清理都会
取消旧计时器；计时器使用本次会话快捷键快照，不会误用刚修改的新设置。

### 9.4 豆包兼容层

`doubao_rpc.py` 的可选 `DoubaoPhysicalizer` 只对固定名称、固定安装目录且
SHA-256 匹配的 `ImeService.exe` 安装 Frida callback hook。hook 仅处理本
桥接为右侧 Alt 按住配置发送的 injected 标记，不改变其他按键。右侧 Alt 仍是
新配置默认；曾评估的 `Ctrl + Alt + F8` 走通用三键注入路径，但真机证明 UU
无法稳定保留它的按住时长，因此没有作为默认值发包。自动键序测试不能代替
UU 远程与目标输入法真机验收。

Frida script/session 的引用只有在卸载或 session detach 成功后才清除。
session detach 成功即证明其脚本不再被会话持有；单独 script unload 报错不再
把兼容层永久误判为“仍需清理”。

### 9.5 搜狗本机内部 IPC 调查（未实施）

2026-08-24 只读核验当前机器安装的搜狗输入法 `16.6.0.4777` 及语音组件
`1.0.1.3272`。证据来自
`Components\ai_voice_input\1.0.1.3272\bin\resources\app.asar` 内的
`out/main/chunks/NativeIPCService.win32-D7_cisol.js`、`out/main/index.js`，以及
当日搜狗运行日志。当前版本会创建标题为 `SogouVoiceInputWnd` 的 0 尺寸隐藏
窗口；该字符串是窗口标题，不是固定窗口类名。窗口监听 `WM_COPYDATA`，其中
`dwData=0` 表示命令行消息，正文按 UTF-8 解码后解析参数。

发送 `--from=hotkey` 会进入搜狗自己的 toggle 路径：空闲时切换为开始，录音时
切换为结束。当前代码中没有找到供外部调用的独立“开始”“停止”或录音状态查询
命令，也没有能证明录音已经真正开始或结束的结果回执。因此按下和松开各发送
一次只能在两次消息均未丢失、未重复且搜狗没有自行改变状态时模拟按住说话；任一
条件不成立都可能反相，例如松手时反而开始录音。

`WM_COPYDATA` 是发给本机窗口句柄的进程间消息，不能从控制电脑直接穿过 UU
到达另一台被控电脑。只有 Remote Mic 与搜狗位于同一台 Windows 电脑和交互
会话时，这条路径才可能直接使用；跨电脑需要被控端另有接收程序。它也只绕过
快捷键触发，不解决音频端点无声、输入框兼容或文字上屏问题。

当前决定是保留调查事实、暂不进入产品实现。本次没有向搜狗发送测试消息，尚未
验证真实时延、漏发/重复、搜狗卡死或重启、不同权限、升级兼容和 UU 两端部署。
后续若重新评估，应先做隔离探针并使用有超时的发送方式，再分别验证正常切换、
漏一次、重复一次、中途自停和版本变化；在取得用户明确确认前不得替换现有宿主
快捷键逻辑。

## 10. 设置、配置与热加载

运行期目录固定为 `%LOCALAPPDATA%\RemoteMic\RC003`：

| 路径 | 内容 |
| --- | --- |
| `config.json` | 设备选择、增益、重试、按住说话快捷键、输出端点 |
| `key_bindings.json` | 主/次手势动作、可移植物理签名映射 |
| `logs\app.log` | 轮转运行日志 |
| `key-detection\` | 最多 30 秒有效的一次性按键检测 IPC |
| `captures\` | 用户显式运行诊断工具时生成的隐私安全 JSONL |

两份 JSON 都逐文件使用临时文件、flush、`fsync`、`os.replace` 原子替换。
设置页保存两份文件时走 `config.save_settings_pair()`：先验证两份结构和隐私
字段，第二份保存失败就把第一份按原始字节原子恢复。这样普通的文件占用、
权限、序列化或 replace 失败不会留下混合版本。

边界说明：普通 NTFS 文件 API 不提供两文件共同提交的断电原子性；极端断电
仍不等同于数据库事务。当前设计解决可观察的运行期写入失败，并通过回读确认
最终内容；未来若需要断电事务，应引入显式 journal/schema revision，而不是
继续堆叠临时文件技巧。

桥接按 mtime 一起热加载按键动作和按住说话设置。活跃语音会话不会中途切换
状态机，新设置会等本次手势完整收尾后应用。设备 profile 与音频端点仍要求
重启桥接。若新文件无法解析，运行时保留最后一份有效配置，但会立即关闭依赖
配置的低层 F5 语音转换快照；修复并成功加载前不继续沿用旧转换行为。

配置 schema 为 3。当前语音设置只写 `voice_hotkeys.hold`、`voice_hotkey` 和
固定值 `voice_trigger_mode=hold`。旧 schema-1 开关快捷键不会被解释成按住
快捷键。新安装、新建配置和恢复默认使用 `ralt`；加载旧文件时先归一化文件中
真实存在的顶层和嵌套语音字段，再合并默认，因此已保存的 `ralt`、自定义值以及
仅写在 `voice_hotkeys.hold` 中的旧值不会被覆盖。旧开关配置没有独立 HOLD 值、
旧 Ctrl+Win 内置值或错误记录的 `lalt` 时，兼容迁移仍回退到历史 `ralt`。
schema-2 的
`voice_release_finish_tap_enabled` 已撤下，加载或保存时都会移除。运行时迁移
标记不会写回用户文件，只有用户明确重新选择并保存后才生成新配置。

配置隐私守卫递归拒绝地址、设备 ID、设备路径、接口 ID 和 token 等字段，
且大小写不敏感。诊断捕获也不提供“包含设备路径”开关。

## 11. 设置界面与诊断

`qt_settings_app.py` 把 Python model/controller 暴露给 QML。主要页面：

- `ConnectionPage.qml`：设备、桥接、输出端点，不重复承载语音动作设置；
- `ButtonsPage.qml`：13 键整宽矩阵，按单击/双击/长按统一展示和编辑主/次动作，
  通过共享 selected state 承接真实按键检测定位，并保存按住说话快捷键；旧照片
  hotspot roles 只留作 model 兼容，不再作为当前界面；
- `PermissionsPage.qml`：权限和系统入口；
- `DiagnosticsPage.qml`：BLE、音频、驱动和日志诊断；
- `main.qml` / `Tokens.qml`：窗口、导航和设计 token。

设置窗口采用一套固定全局框架：顶部保留产品标识与四页导航，中间页面独立滚动，
底部只显示一份 `SettingsController` 普通状态或错误。普通状态使用中性色，不把
“进程已启动”着色或措辞成“RC003 已连接”；错误优先于普通状态，后续成功操作
会清除旧错误。连接页仍保留“仅保存”和“保存并启动”两个独立命令，前者不启动
进程，后者必须保存成功后才调用桥接启动器。

权限页不维护统一的真假授权状态，因为 Windows 没有覆盖这些入口的单一可靠 API。
RC003 模式显示蓝牙配对、可选 HID tap/VB-CABLE 与宿主快捷键边界；DJI Mic 2
模式隐藏 RC003 专属内容，只说明目标应用的麦克风访问和系统输入选择。VB-CABLE
的检测、确认和 UAC 安装继续只有 `DiagnosticsPage.qml` 一套流程，权限页只导航，
不复制系统变更操作。

耗时 WinRT、PortAudio 和系统诊断在后台线程执行。窗口退出会按顺序停止热键
录制、真实按键检测、诊断 worker 和遗留音频预检流；某一步失败不跳过后续
清理。

后台桥接存在时，“检测真实按键”不再争抢 Raw Input/HID tap，而是写入一次性
本地文件请求，由桥接吞掉下一次按下/释放、返回逻辑 button id 且不执行映射。
无法安全确认桥接 mutex 状态时，设置页停止检测，不猜测“没有运行”。

13 个实体按键的主映射统一可编辑；只有实体话筒键额外提供“按住说话”，其他
按键只提供普通动作或组合键。顶部语音面板只维护一套快捷键和默认关闭的松手
收尾开关。保存层拒绝把语音动作放入非话筒键或次级手势；麦克风键默认映射为
按住说话，但可以改为普通动作。

多个输入来源可能同时尝试认领该请求。`key_detection_bridge.py` 必须先用
`O_CREAT | O_EXCL` 独占 claim lock，再读取和移动 request JSON；结果写入失败
时尽量恢复 request。先读文件再争锁会在 Windows 共享时序下产生双失败。

## 12. 线程、队列与资源所有权

| 所有者 | 资源 | 停止/失败原则 |
| --- | --- | --- |
| asyncio 主线程 | supervisor、BLE 生命周期、托盘退出协调 | cleanup 失败则停止重连循环 |
| BLE worker | CONTROL/AUDIO 队列和解码回调 | 异常通知 supervisor，不静默死亡 |
| Raw Input 线程 | 隐藏窗口、设备通知、按键状态 | join 超时保留 listener 引用 |
| 低层钩子线程 | F5/原生键抑制 | 消息队列 ready 后才报告启动成功 |
| HID tap 线程/注入子进程 | loopback server、目标进程句柄、Gadget 消息 | 验证客户端 PID，心跳/大小有界 |
| 托盘线程 | Win32 window、图标、菜单 | 退出回投 asyncio，不直接碰 BLE |
| Qt 诊断线程 | 一次诊断任务 | 窗口退出发 stop 并有界等待 |
| PortAudio sink | native output stream | close 未确认时保留 owner 重试 |

最重要的总原则是“引用代表所有权”。线程仍活、handle 未关闭、stream 未关闭
或 key-up 未确认时，对象引用不能清成 `None`。清理会尝试所有独立步骤，最后
聚合失败；不能因某一步报错跳过其他资源，也不能在旧资源存活时开始新一代
连接。

## 13. 日志与隐私

日志位置为 `%LOCALAPPDATA%\RemoteMic\RC003\logs\app.log`，5 MiB 轮转，
保留 3 个备份。日志允许记录：

- 固定状态名、异常类型；
- 逻辑按钮名、语音模式；
- PCM 帧数、时长、峰值等统计；
- 候选数量、布尔匹配和退出码。

日志禁止记录：

- 蓝牙地址；
- WinRT device id、HID/设备接口路径；
- 设备 token、原始 handle/address；
- 用户语音内容或识别文本；
- 原生异常中可能夹带的机器本地路径。

持久日志 handler 会移除 traceback，并把作为日志参数传入的异常替换为异常
类型。这只是第二道防线，调用点仍必须使用固定消息和隐私安全字段。

## 14. 构建、安装与第三方资产

`build/RemoteMicRC003.spec` 生成 one-dir、windowed、unsigned PyInstaller
候选。构建明确包含 QML、设备 profiles、遥控器图片和已验证的可选资产。

`build/build-candidate.ps1` 的门禁顺序是：

1. 准备虚拟环境和固定依赖；
2. 公开边界扫描；
3. 下载并校验固定 VB-CABLE 包；
4. 运行带 `ResourceWarning` 门禁的完整测试；
5. PyInstaller 构建；
6. 冻结 EXE `--dry-run`。

Frida Gadget 与 VB-CABLE 的构建策略不同：VB-CABLE 是候选构建的必经下载门禁，
而 Frida 仍由 `fetch-frida-gadget.ps1` 显式获取，`build-candidate.ps1` 不会自动
下载它。需要保留返回键/音量键 HID tap 补齐能力的完整 RC003 候选，必须在构建
前完成 Frida 固定哈希校验，并在成品目录和 ZIP 中再次确认资产存在；缺失该文件的
包只能作为不含 HID tap 的降级构建，不能沿用完整候选的验收结论。

`installer/RemoteMicRC003Setup.iss` 是 per-user 安装器，不自动启动、不自动
安装驱动。升级和卸载前调用 `stop-app.ps1`，只停止安装目录下、文件名精确为
`RemoteMicRC003.exe` 且 PID/CreationDate 仍匹配的进程。无法确认退出时，
安装或卸载会停止，不覆盖仍在使用的文件。

Frida Gadget 与 VB-CABLE 包都使用固定 URL/version/SHA-256。运行时仍再次
校验，不把“构建时下载成功”当成永久可信。

## 15. 验证层级与当前事实

| 层级 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| unittest | 状态机、异常路径、资源所有权、隐私/构建契约 | 真实蓝牙、驱动、输入法行为 |
| Windows API smoke | ctypes ABI、Raw Input/SendInput/托盘生命周期 | RC003 每个 usage 和长期稳定性 |
| QML 离屏加载 | QML 可创建、布局关键契约、窗口生命周期 | 用户机器显卡/缩放/主题全部组合 |
| 冻结 EXE smoke | PyInstaller 依赖完整、入口可运行 | BLE/音频/权限真机成功 |
| 真实硬件验收 | 当前候选在指定机器上的按键和语音事实 | 其他机器、休眠、长期、杀软兼容 |

源码检查点 `eafd203` 的完整审查基线已通过；当前 HOLD-only 修改在
2026-08-22 完成 1151 项 unittest，7 项安全或平台条件跳过。核心定向测试
481 项通过、1 项跳过；`compileall`、`pip check`、PowerShell parser、公开边界
扫描 278 个文件及 `git diff --check` 均通过，QML 离屏加载和 Windows 输入调用
契约包含在完整测试中。详细历史审查命令与证据见
`reviews/2026-08-21-full-code-audit.md`。这些结果证明当前源码的自动化基线，
冻结候选的 `--help`、`--dry-run`、资源与 QML 完整性也已通过；这些结果仍不
证明冻结包已经通过 RC003、VB-CABLE 或目标输入法的端到端实机验收。

截至 2026-08-22，历史异机结果已证明：多候选中可选出唯一可用 RC003、全部
普通按键可在映射页识别、记事本映射有效、按住语音得到非零 PCM 和识别文字、
F5 不再向输入框泄漏日期时间。On-request 真机探针最终未收到 `START_SEARCH`，
开关型已正式撤下。最新代码检查点还需要复测 HOLD 短流尾音、标准松手结束、
旧配置停用提示、自定义组合键不连发、断连重连、休眠恢复、长期运行、权限/
杀软兼容和安装器升级/卸载。

## 16. 模块索引

| 领域 | 主要文件 |
| --- | --- |
| 入口与总装 | `launcher.py`、`__main__.py`、`app.py` |
| 连接监督 | `connection_supervisor.py`、`single_instance.py` |
| BLE/ATVV | `ble_transport_winrt.py`、`atvv_protocol.py`、`atvv_session.py` |
| 音频 | `audio_output.py`、`audio_playback.py` |
| 普通按键 | `raw_input_windows.py`、`hid_identity.py`、`button_gesture.py` |
| 动作输出 | `key_mapping.py`、`win32_input.py`、`action_executor.py` |
| F5/去重 | `legacy_key_suppressor_windows.py`、`hotkey_capture_windows.py` |
| HID tap | `frida_compat.py`、`frida_hid_tap_runtime.py`、`frida_hid_tap_injector.py` |
| 豆包兼容 | `doubao_rpc.py` |
| 配置/IPC | `config.py`、`key_detection_bridge.py`、`key_testing.py` |
| 设置界面 | `qt_settings_app.py`、`settings_ui.py`、`qml/*.qml` |
| 诊断/日志 | `windows_diagnostics.py`、`logging_setup.py`、顶层诊断脚本；历史 On-request 探针已撤下 |
| 资源/设备目录 | `resources.py`、`device_catalog.py`、`device-profiles/` |
| 驱动帮助 | `vb_cable_bundle.py` |
| 构建安装 | `build/`、`installer/`、Windows CI workflow |

## 17. 文档入口

| 想了解什么 | 权威入口 |
| --- | --- |
| 项目背景、构成原理、当前边界 | 本文 |
| 安装、配置、运行、测试和构建命令 | `README.md` |
| 上游归属、许可证和第三方边界 | `ATTRIBUTION.md`、仓库根目录许可文件 |
| 当前测试方法与人工验收项目 | `TESTING.md` |
| 单个已知故障的现象、原因和修复 | `bugs/BUG-*.md` |
| 维护批次、源码基线和验证证据 | `MAINTENANCE.md` |
| 用户可见版本变化 | `CHANGELOG.md` |
| 一次完整代码检查的范围和结论 | `reviews/` |
| 尚未实施的功能推演与过程讨论 | `discussions/` |

建议第一次接手时依次阅读本文、`README.md`、`TESTING.md`，再按当前任务读取
对应的 `bugs/`、维护记录或检查报告；不需要先通读所有历史流水。

## 18. 台账维护规则

- 新改动必须先确定资源所有者和失败后的重试边界。
- 输入路径不能在回调/钩子线程执行可能阻塞的 PortAudio、磁盘或进程操作。
- 控制事件不能与可丢弃音频共用一个会被音频填满的队列。
- `AUDIO_STOP` 不能越过应用已经收到的同代音频；跨队列处理必须保留顺序证据。
- 宿主快捷键与设备 `MIC_OPEN/MIC_CLOSE` 必须按既定先后顺序交付。
- 自动检查、冻结 smoke、真机验收必须分开写，不互相冒充。
- 候选的源码提交、目录 EXE 哈希、ZIP 哈希和 ZIP 内 EXE 复核写入维护/发布
  记录；本文只说明这一产物身份规则，不重复维护每个包的具体数值。
- 不提交二进制、设备身份、个人路径、凭据或语音内容。
- 对外分发继续满足 GPL-3.0-only 源码提供与第三方通知义务。
- 只有项目目标、用户工作流、模块职责、关键数据链、正式边界或长期有效结论
  发生变化时才更新本文；单次修复细节先进入故障、维护或检查记录。
