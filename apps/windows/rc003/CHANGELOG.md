# Changelog — Remote Mic RC003 (Windows)

本项目按“候选发布”打标签。内部构建版本号固定在
`installer/RemoteMicRC003Setup.iss` 的 `AppVersion`（当前 `0.1.0-candidate`），
仓库级 tag 只作为发布编号，两者对应关系以每条发布说明为准。

标签格式：`v<内部版本>-windows-rc003-candidate.<序号>`。

## [Unreleased] — 2026-08-21

状态：自动检查通过，部分真机路径通过，最新实现待复测。`fix8` 异机测试确认全部按键可在
映射页识别、语音存在有效 PCM 和可见识别文字，且记事本未再插入日期时间；
同时暴露设置页保存语音模式/快捷键后，运行中桥仍沿用旧状态机。`fix9` 已增加
语音设置热加载；`fix10` 在完整代码审查后加固资源清理、队列、配置保存、
HID tap 身份校验、进程控制和日志隐私。自动检查与 `fix10` 候选构建已通过，
本机已确认 13 个按键全部可识别；同轮日志发现检测麦克风键会抢先进入语音
状态，`fix11` 已隔离检测手势与正式语音路径。后续本机日志确认切换模式的
续开命令早于实体 F5 松键，导致第一次短按后空流；`fix12` 已改为实体释放后
续开，待本机复测。随后单独完成语音设置界面整合：连接页不再重复展示语音
配置，按键页集中管理两套语音快捷键；当前 `fix15` 源码进一步取消全局语音
模式选择，把开关型/按住型语音改为任一实体键可选的主映射动作，并允许麦克风
键改为普通动作。该界面与动作模型批次不改变 `fix12` / `fix14` 的语音时序
实现；非麦克风键能否持续取得 ATVV 音频仍待新候选真机复测。
本节不得扩大为完整真机通过声明。
维护证据与验收入口见
`MAINTENANCE.md`、
`bugs/BUG-001-audio-endpoint.md`、`bugs/BUG-002-hid-tap-readiness.md`
、`bugs/BUG-003-duplicate-bridge-dialog.md`、
`bugs/BUG-004-background-bridge-tray.md`、
`bugs/BUG-005-toggle-continuous-voice.md`、
`bugs/BUG-006-live-bridge-key-detection.md`、
`bugs/BUG-007-duplicate-winrt-ble-candidates.md`、
`bugs/BUG-008-toggle-multi-source-gesture.md`、
`bugs/BUG-009-f5-hook-release-race.md`、
`bugs/BUG-010-live-voice-settings-reload.md`、
`bugs/BUG-011-mic-key-detection-voice-race.md`、
`bugs/BUG-012-toggle-reopen-before-physical-release.md`、
`bugs/BUG-013-key-detection-claim-race.md`、
`bugs/BUG-014-toggle-reopen-f5-echo.md`、
`bugs/BUG-015-sogou-codex-text-commit.md` 和 `TESTING.md`。

### 修复

- **语音触发但无声**：过滤当前 PortAudio 阻塞播放不支持的
  `Windows WDM-KS` 端点；同名视图优先 `Windows WASAPI`，缺失时使用
  `Windows DirectSound`；设置保存和诊断页自动选择都会先真实打开、启动
  并关闭输出流，不能再把只能枚举、无法播放的端点写入配置。已有 WDM-KS
  配置且检测到标准 CABLE Input 时，设置页预选其 WASAPI 视图，等待用户
  保存后再持久化。
- **部分按键在设置页无法识别**：设置页的真实按键检测同时接收 Raw Input
  与 Frida HID tap，tap 只补齐 Windows 普通键盘链路丢失的返回和音量
  usages，任一来源捕获到首个按下事件后统一停止且不执行映射动作。
- **HID tap 假就绪与日志缺失**：注入改为隐藏的独立子进程，保留 PID、
  WUDFHost 和 Gadget 哈希校验；新增等待宿主、注入、等待连接、等待 IO、
  ready、不健康和失败状态。后台线程创建只记录 starting，收到有效 HID IO
  后才记录 ready，状态进入 `app.log` 而不是窗口 EXE 不可见的 stdout。
- **重复启动提示与状态矛盾**：设置页启动桥接时附加隐藏来源标记；已有
  桥接实例时，子进程不再弹出阻塞轮询的系统模态框，而是立即返回既有的
  确定退出码，设置页可准确显示“已经在运行”。手工 `--bridge` 启动仍保留
  可见的单实例保护提示。
- **后台桥接缺少退出入口**：桥接进程新增 Windows 通知区域图标；双击可
  打开设置，右键可选择“打开设置”或“退出桥接”。托盘退出会投递到 asyncio
  主线程并执行既有 BLE、HID、语音热键、音频与互斥锁清理，不依赖任务
  管理器强杀。关闭设置窗口仍只关闭设置，不会意外中断桥接。
- **切换模式短按后没有持续语音**：切换状态现在只由两次独立按下改变；
  第一次松开产生的 `AudioStopped` 不再关闭宿主，而会续发 `MIC_OPEN`；
  第二次按下发送宿主关闭快捷键和线程安全 `MIC_CLOSE`，竞态音频事件不会
  重新开麦。按住说话模式保持原行为。
- **从托盘打开设置后真实按键检测失效**：设置页检测到后台桥接已持有 HID
  资源时，通过一次性本地文件 IPC 请求后台捕获下一键；后台回传逻辑按键并
  吞掉该次 down/up，不执行映射。后台未运行时仍使用本地 Raw Input 与
  HID tap。
- **配对列表一个设备但桥接报告两个候选**：先用隐私安全日志确认 Windows
  配对数据库保留了两个不同 RC003 记录；精确删除隐藏 PnP 节点后系统扫描
  仍会重建。桥接现在只在多候选时逐个执行 UNCACHED ATVV 语音服务探测，
  恰好一个可用才连接；零个或多个可用仍失败关闭，不按名称或顺序猜测。
- **切换模式仍像按住说话**：同一次实体麦克风按下会分别产生
  `AudioStarted`、legacy F5、HID 和 ATVV mic 事件，旧局部门闩会把后到
  来源当成第二次点击并立即 `MIC_CLOSE`。现在首个来源认领整轮手势，其他
  来源只合并不再切换；物理/音频释放后下一次按下才执行关闭。HOLD 模式的
  一次 key-down/key-up 行为保持不变。
- **切换模式时灵时不灵且偶发输入日期时间**：`AudioStopped` 会早于排队中
  的 F5/HID 物理释放，门闩不能只靠音频停止释放；低层 F5 钩子也不能同步
  等待 PortAudio 初始化。现在手势需同时越过音频停止和全部物理 up，F5
  应用处理投递到事件循环，重复音频 start/stop 只处理一次；关闭切换时不再
  重开播放端点。
- **设置语音模式后运行中行为没有变化**：设置窗口会保存
  `voice_trigger_mode` 与 `voice_hotkey`，但桥接只热加载按键映射，导致界面
  选择 HOLD 后日志仍执行 TOGGLE。桥接现在同时监控两个设置文件；空闲时在
  下一次输入事件前立即应用语音模式和快捷键，活跃会话则在完整收尾后切换。
  启动和热应用日志会写明实际生效模式与快捷键。
- **检测麦克风键后后台持续空开麦**：`AudioStarted` 或 ATVV mic 事件可能
  比 F5/HID 的检测回调先到，旧逻辑会先改变语音状态，再把后到物理边沿作为
  检测结果吞掉。现在一次检测手势由首个来源统一认领，音频、ATVV、F5 和
  HID 后续事件全部只用于收尾，不发送宿主快捷键、播放端点或设备开关麦命令；
  释放宽限和硬超时确保乱序事件被吸收且下一次正常按键仍可用。
- **切换模式第一次短按后无声、必须先长按**：真机日志确认
  `AudioStopped` 到达时实体 F5 仍处于 down；旧逻辑立即续发 `MIC_OPEN`，
  设备虽回复 `AudioStarted` 却不再发送 PCM。现在续开先等待全部实体来源
  up；BLE-only 路径使用 200 毫秒有界宽限吸收迟到边沿。第二次点击、清理和
  重连都会取消待续开，旧回调不能串入新连接。
- **真实按键检测偶发没有结果**：多个 HID/F5/音频来源会同时读取一次性请求
  后再竞争认领锁；Windows 文件共享时序可能让锁胜者移动失败、败者也返回
  失败。现在先独占 claim lock 再读取和移动请求，结果写入失败会尽量恢复
  请求供后续来源重试。
- **切换续开后约 60 毫秒又被关闭**：`fix13` 真机日志确认程序在实体松键后
  续发 `MIC_OPEN`，紧接着出现一组新的 legacy F5 down/up，被误判成第二次
  点击并产生空会话。现在续开后设置 250 毫秒有界回声保护，同源 down/up
  成对吞掉；保护期后的真实第二次点击仍正常关闭。
- **PortAudio 清理失败时丢失重试句柄**：播放流只有在 `close()` 确认成功后
  才清除内部引用；关闭失败会保留句柄供后续清理重试。
- **隐私字段大小写绕过**：禁止持久化的设备标识字段改为大小写不敏感检查，
  不能通过改变 JSON 键名大小写绕过。
- **开始菜单环境缺失时误扫工作目录**：`APPDATA` 或 `PROGRAMDATA` 不存在时
  不再把相对路径解析为当前工作目录，避免意外启动同名快捷方式。

### 变更

- 语音生命周期（按一下切换 / 按住到松开）与宿主语音快捷键解耦。切换
  生命周期或录制快捷键不再擅自改写另一个字段，支持宿主把
  `ralt+space` 定义为按住说话等真实配置。
- `fix13` 把语音设置从连接页迁移到按键映射页，并为“按一下切换”和“按住
  说话”分别保存宿主快捷键；当时麦克风映射卡仍保持只读。
- `fix15` 顶部语音区只保存开关型和按住型两套快捷键，不再提供全局模式选择；
  13 个实体键的主映射均可选择两种语音动作或普通动作，麦克风键同样可编辑。
  保存时明确拒绝多个语音主映射及次级语音动作；语音主映射行的双击/长按控件
  同步不可用，但原有普通次级设置会保留，切回普通主映射后恢复。旧 generic
  `VOICE` 配置按原 `voice_trigger_mode` 迁移。
- 普通麦克风映射与非麦克风语音映射都按事件来源合并重叠和迟到边沿，避免
  HID tap、Raw Input 与 legacy F5 把一次实体操作执行两次；热加载到损坏配置
  时会立即关闭旧 F5 语音转换快照，修复配置前保持失败关闭。
- 设置页保存提示区分热应用与重启边界：按键映射、显式语音动作和两套快捷键在
  下一次输入事件时应用；连接设备和音频输出端点仍需重启桥接。

自动验证与候选：

- 完整测试：1006 项通过，7 项安全或平台相关跳过，退出码 0。
- 公开边界扫描：227 个文件通过；`compileall`、`pip check` 与
  `git diff --check` 通过。
- PyInstaller 构建和冻结入口检查通过：`--dry-run=0`、`--help=0`、无效
  HID 注入 PID 入口 `=4`。
- `fix9` 候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260820-fix9/`。
- `fix9` 候选 EXE SHA-256：
  `DB3F49E23F2B6EEB6C51B9B0B45C5A2E1B29EEEA2E8FA97FFB999A49A3E91C28`。
- `fix9` 便携 ZIP SHA-256：
  `F24AFA07020385276297537E6B3F3F49880A305BC019460D3849778835CC778C`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- `fix10` 代码修复检查点：`eafd203`；构建时 Git HEAD：`041c125`。
- `fix10` 完整测试：1090 项通过，7 项安全或平台相关跳过；公开边界扫描
  228 个文件通过；PyInstaller 构建通过。
- `fix10` 冻结入口：`--dry-run=0`、`--help=0`、无效 HID 注入 PID `=4`。
- `fix10` 候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260821-fix10/`。
- `fix10` 候选 EXE SHA-256：
  `B499B7C665115B128FFAF49F80A9A425B82969157A4F0C6EA2ABCFA784BA15B2`。
- `fix10` 便携 ZIP SHA-256：
  `69B3F76BB2590C736CFEE6359AADE5EB8848DB2937D7FDC8CDAF404F70708799`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- `fix11` 麦克风检测隔离代码提交：`4d6a0fe`。
- `fix11` 构建前完整测试：1093 项通过，7 项安全或平台相关跳过；公开边界
  扫描 229 个文件通过；`compileall`、`pip check` 和 `git diff --check` 通过。
- `fix11` 构建时 Git HEAD：`c135991`；PyInstaller 构建通过，冻结入口
  `--dry-run=0`、`--help=0`、无效 HID 注入 PID `=4`。
- `fix11` 候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260821-fix11/`。
- `fix11` 候选 EXE SHA-256：
  `4639AC9942C3D66525ED8EC7457F1EA7562E1B9DBD14ED5E5FD05BB2CB44ED7D`。
- `fix11` 便携 ZIP SHA-256：
  `DF9F50C4B9F11D703FA65BFE200A52FBD953B7591670FA3AEF89B6C96985127B`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- `fix12` 实体释放后续开代码提交：`f6ae3c3`；并发检测认领提交：
  `cfbca35`。应用接线测试 83 项通过、1 项平台相关跳过；检测与应用扩展
  定向测试 90 项通过、1 项跳过；完整测试 1096 项通过、7 项跳过；公开边界
  扫描 231 个文件通过。
- `fix12` 构建时 Git HEAD：`62b1032`；PyInstaller 构建通过，冻结入口
  `--dry-run=0`、`--help=0`、无效 HID 注入 PID `=4`。
- `fix12` 候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260821-fix12/`。
- `fix12` 候选 EXE SHA-256：
  `7B99267D5E1385408CDBAAE36BA422FAACC43B50F16E47BF3E5EEC8176BB0926`。
- `fix12` 便携 ZIP SHA-256：
  `D6B9168B728E12FD0D892BD472547007F0A8B5E835F34D35230677E76BF5CBAA`；
  ZIP 内 EXE 已重新计算并与候选目录一致。
- `fix13` 界面代码提交：`459a0bd`。配置、设置控制器与 QML 定向测试
  174 项通过；完整测试 1102 项通过、7 项安全或平台相关跳过；公开边界扫描
  231 个文件通过；`compileall`、`pip check` 和 `git diff --check` 通过。
- `fix13` 构建时 Git HEAD：`63b038d`；PyInstaller 构建通过，冻结入口
  `--dry-run=0`、`--help=0`、无效 HID 注入 PID `=4`。
- `fix13` 候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260821-fix13/`。
- `fix13` 候选 EXE SHA-256：
  `DB7C357419DACDC04491C50CF3BD5AFF4353467B30BE3B5E73E5C3012EC34CA6`。
- `fix13` 便携 ZIP SHA-256：
  `B496F8DEED84DC7D8A2B28E711931EDEC11BC169A93771F56732A6339849D4FE`；
  ZIP 内 EXE 已重新计算并与候选目录一致。人工界面与真机语音仍待复测。
- `fix14` F5 续开回声代码提交：`bfd514d`。应用接线测试 84 项通过、1 项
  平台相关跳过；完整测试 1103 项通过、7 项跳过；公开边界扫描 231 个文件
  通过；`compileall`、`pip check` 和 `git diff --check` 通过。
- `fix14` 构建时 Git HEAD：`f5c86b9`；构建门禁公开边界扫描 233 个文件，
  PyInstaller 和冻结入口检查通过。
- `fix14` 候选目录：源码仓库同级的
  `RemoteMicRC003-0.1.0-candidate-20260821-fix14/`；EXE SHA-256 为
  `E0E3A7A36D6F07A1E1893E6B2A2750240EE0C78056EE5DC6DCE331227C98D24D`。
- `fix14` 便携 ZIP SHA-256：
  `59DA357C40BDA873AF7FF06F4CFFFC68D1E5EB56FEC4BE37B3CE3BAA453FCDB5`；
  ZIP 内 EXE 已重新计算并与候选目录一致。真机复测待完成。

## [0.1.0-candidate] — 2026-07-31

标签：`v0.1.0-windows-rc003-candidate.1`（基于 `6c33fcc`）

首个 Windows RC003 候选发布。本版本已在真实 RC003 遥控器上完成逐键、
语音链路验收（详见 README“真机验收”部分）。CI 与自动构建仍然不能
替代真机验证。

### 新增

- **Frida HID tap 旁路**：对 Windows 普通输入链路拿不到的返回、音量+、
  音量- 缺失 usages（`0xF1`、`0x80`、`0x81`），复用上游
  `remote-bridge-hub` 的 Frida Gadget WUDFHost tap 读取；扩展为上报
  遥控器全部键盘 usage，作为所有普通按键的输入旁路。Gadget 是可选的
  第三方二进制，需显式获取（`build/fetch-frida-gadget.ps1`）且验证
  固定 SHA-256 后才会启用。
- **豆包语音触发（DoubaoPhysicalizer）**：注入的右 Alt 合成事件此前被
  豆包输入法 `ImeService` 的低层键盘钩子以 `LLKHF_INJECTED` 标志忽略；
  现在附加到 `ImeService.exe` 的低层回调，只对该标记事件清除 injected /
  lower-integrity 标志并清空 `dwExtraInfo`，使豆包看到的按键形状与
  实体右 Alt 一致。默认按住模式 `ralt`、切换模式 `ralt+space`。
- **设置页独立入口**：`RemoteMicRC003Settings.exe` 与桥接 EXE 分离
  （后合并为单个 EXE，见下）。
- **按键采集/回放工具**：`src/rc003_key_test.py`、`rc003_key_probe.py`
  等诊断工具，被动记录真实物理签名，不执行映射动作。

### 修复

- **普通按键双触发**：方向键、OK 等按键按下时一次动作被触发两次。
  根因是低层键盘钩子阻塞了 `WM_INPUT` 派发，导致“先 arm 后吞键”的
  等待式方案永远慢半拍。改为由 Frida GATT tap 的独立 socket 线程在
  `NtDeviceIoControlFile` 报告到达时 arm，低层钩子零等待匹配并吞掉
  原生按键，只注入一次映射动作。方向/OK/Home/Menu/TV/Power/返回/
  音量键全部实测通过。
- **F5 语音键重复替换刷屏**：按住麦克风键期间键盘 auto-repeat 会让
  “替换为右 Alt”逻辑反复触发；为 transform 增加已按下/已发送守卫，
  只在真实按下/释放边沿各发送一次。
- **BLE GATT 特征找不到**：修复后反复出现
  `ATVV characteristic not found`；改用 `BluetoothCacheMode.UNCACHED`
  读取服务与特征，避免 Windows 缓存旧枚举结果。
- **设置保存失败且映射不生效**：配置文件改为临时文件 + fsync +
  `os.replace` 原子写入；Qt 设置保存捕获一切持久化/回读异常并在界面
  显示错误；桥接进程在按键前按 mtime 热加载新的按键映射，磁盘数据
  损坏时保留最后一份有效映射。
- **启动闪黑色命令行窗口**：桥接启动子进程与打包运行时的控制台子进程
  均使用 `CREATE_NO_WINDOW` 隐藏。
- **语音识别无声/不稳定**：语音输出改为按端点能力输出立体声并复制
  声道；解码后增加 20 Hz 一阶高通 DC 阻挡；默认增益提高到 +10 dB；
  16 kHz → 48 kHz 改有状态连续插值（对齐上游）。实机验收：豆包输入法
  能识别遥控器语音。

### 变更

- **单 EXE 行为**：合并为同一个 `RemoteMicRC003.exe`。双击（无参数）或
  `--settings` 打开设置窗口；`--bridge` 显式启动桥接进程。安装器/便携版
  的启动快捷方式统一使用 `--bridge`。
- **设置保存原子化**：`save_config` / `save_key_bindings` 走原子写入，
  不暴露半写的 JSON。
- **返回键默认映射**：保持 `delete_backward`（退格）语义；新增可选的
  “浏览器后退”动作供用户在设置页手动绑定。
- 普通按键仍通过 `SendInput` 注入映射动作；语音快捷键通过物理化的
  右 Alt 事件；两者互不混用。

### 已知限制

- 未签名，首次运行会触发 SmartScreen 提示，属预期行为。
- Frida Gadget 与 VB-CABLE 均为可选第三方组件；未显式获取/安装时，
  缺失 usages 不会被猜测伪造，语音默认没有虚拟麦克风路由。
- 遥控器没有独立物理静音键；“系统静音”只是可选手动绑定。
- 安装器与便携版运行期配置都写入 `%LOCALAPPDATA%\RemoteMic\RC003`，
  卸载不会自动删除。
