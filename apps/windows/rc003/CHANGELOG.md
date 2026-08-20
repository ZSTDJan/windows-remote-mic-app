# Changelog — Remote Mic RC003 (Windows)

本项目按“候选发布”打标签。内部构建版本号固定在
`installer/RemoteMicRC003Setup.iss` 的 `AppVersion`（当前 `0.1.0-candidate`），
仓库级 tag 只作为发布编号，两者对应关系以每条发布说明为准。

标签格式：`v<内部版本>-windows-rc003-candidate.<序号>`。

## [Unreleased] — 2026-08-20

状态：自动检查通过；按住说话已在当前机器产生非零 PCM 和可见识别文字，
但 `fix5` 的切换语音与桥接运行时按键检测仍待复测。本节不得扩大为完整
真机通过声明。维护证据与验收入口见 `MAINTENANCE.md`、
`bugs/BUG-001-audio-endpoint.md`、`bugs/BUG-002-hid-tap-readiness.md`
、`bugs/BUG-003-duplicate-bridge-dialog.md`、
`bugs/BUG-004-background-bridge-tray.md`、
`bugs/BUG-005-toggle-continuous-voice.md`、
`bugs/BUG-006-live-bridge-key-detection.md` 和 `TESTING.md`。

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

### 变更

- 语音生命周期（按一下切换 / 按住到松开）与宿主语音快捷键解耦。切换
  生命周期或录制快捷键不再擅自改写另一个字段，支持宿主把
  `ralt+space` 定义为按住说话等真实配置。

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
