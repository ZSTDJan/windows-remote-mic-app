# RC003 Windows 性能专项

状态：已建立基线，待分阶段优化

首次审计：2026-08-29

审计源码基线：`b7e0c21a227ad0de9f1c814c312f7f61b0967df1`

## 1. 文档职责

本文是 Windows RC003 主程序的性能权威入口，长期维护以下内容：

- 设置窗口、后台桥接、BLE/ATVV、按键、音频和应用动作的性能基线；
- 已复现的性能问题、具有明确触发路径的性能风险和处理优先级；
- 优化方案、验证指标、回退边界和不建议进行的过度优化；
- Python、PySide6/QML、WinRT BLE 和 PortAudio 同类实现的可复用经验。

本文不替代 `WINDOWS-ARCHITECTURE-LEDGER.md` 的架构边界，不记录逐次修复流水，
也不表示已经进入候选构建或发布。实施结果和真机验收仍分别写入
`MAINTENANCE.md`、`TESTING.md` 或对应故障记录。

`scripts/element_navigation_prototype.py` 是实验性元素导航，不属于当前 RC003
生产桥接热路径。它的 UI Automation 性能建议单独放在本文附录，不能与主程序
的 BLE、按键或音频结论混在一起。

## 2. 本轮结论

当前没有证据表明程序存在持续高 CPU、无界队列、明显内存泄漏或 Python 音频计算
能力不足。已确认的问题集中在“阻塞工作放错线程”和“启动阶段做得过早”，不是需要
更换语言或把全部代码重写成原生模块。

本轮没有 P1 性能故障。保留 4 个 P2 和 2 个 P3：

| ID | 等级 | 结论 | 当前影响 |
| --- | --- | --- | --- |
| PERF-001 | P2 | 阻塞式 PortAudio 写与 BLE 控制事件共用一个 worker | 音频写阻塞会延后 `AUDIO_STOP` 等控制事件 |
| PERF-002 | P2 | 设置控制器在 GUI 线程同步枚举音频端点和检查语音程序 | 首屏前稳定增加约 220 至 240 ms，同步状态检查约 143 ms |
| PERF-003 | P2 | 语音快捷键操作用嵌套 `QEventLoop` 等待后台线程 | 允许 QML 在调用尚未返回时重入，且每 10 ms 轮询一次 |
| PERF-004 | P2 | 设置窗口构造时立即执行全部系统诊断 | 本机约 649 ms 的后台系统检查，即使用户未主动重查也会启动 |
| PERF-005 | P3 | 三个 QML 页面在首屏一次性全部构造 | 离屏实测加载约 133 至 180 ms，共创建约 1273 个 QObject |
| PERF-006 | P3 | 应用动作每次重新扫描安装路径和开始菜单 | 每次动作额外约 20 至 109 ms，可用进程级缓存消除大部分重复扫描 |

## 3. 测量边界与当前基线

### 3.1 测量环境

- 核验日期：2026-08-29。
- 解释器：仓库现有 `apps/windows/rc003/.venv`。
- 源码入口：`apps/windows/rc003/src`。
- 设置窗口对象数和 QML 加载时间使用 `QT_QPA_PLATFORM=offscreen`，并用 Basic
  样式隔离真实桌面绘制差异；系统查询仍调用当前 Windows 主机的真实接口。
- 音频写耗时和尾部通知来自会话内此前已核实的真实运行 `app.log`，不是模拟值。
- 当前 PyInstaller 使用 one-dir `COLLECT`，不存在 one-file 每次启动解压的额外成本。
- 下表不是跨电脑统一成绩。硬件、Windows 音频栈、已安装应用和蓝牙状态变化后应重新取样。

### 3.2 设置窗口与启动

| 项目 | 当前结果 | 判断 |
| --- | ---: | --- |
| 导入 `ovb_rc003.qt_settings_app` | 约 207 至 250 ms | 可继续剖析，但不是单一主因 |
| 首次加载 Qt 类 | 约 75 至 84 ms | Qt 运行时固定成本 |
| 创建 `SettingsController` | 约 219 至 234 ms | 主要被首次 PortAudio 端点枚举占用 |
| 创建 `DiagnosticsController` | 约 1.5 ms | 构造很轻，但会立刻启动后台全量诊断 |
| 加载完整 `main.qml` | 约 133 至 180 ms | 三页全部构造，共约 1273 个 QObject |
| 首轮 `processEvents()` | 约 5 至 6 ms | 当前不是问题 |
| 一次全量系统诊断 | 约 649 ms | 后台执行，不冻结 GUI，但启动过早 |

首次音频端点枚举约 239 ms，进程内热调用约 0.1 ms。搜狗和微信语音程序状态检查
中位数分别约 144 ms 和 143 ms。两类调用都涉及系统或跨进程查询，不应直接放在
GUI 构造和普通 Qt Slot 的同步路径上。

### 3.3 桥接、按键与音频

| 项目 | 当前结果 | 判断 |
| --- | ---: | --- |
| 240 样本 ADPCM 解码、滤波和增益 | 中位约 0.246 ms | 不是瓶颈 |
| PCM 统计 | 中位约 0.053 ms | 不是瓶颈 |
| 48 kHz 重采样和双声道转换 | 中位约 0.077 ms | 不是瓶颈 |
| PortAudio 流打开 | 真实日志约 67 至 99 ms | 会话开始的一次性成本，可接受但应持续观察 |
| 阻塞式 `stream.write()` | 常见约 14 至 31 ms，历史最大约 57.09 ms | 已进入控制事件共享 worker 的延迟范围 |
| PortAudio underflow | 已核实日志为 0 | 当前音频连续性良好 |
| `AUDIO_STOP` 前补处理尾部通知 | 已出现 1 至 21 个 | 证明共享 worker 会积压，不是纯理论问题 |

### 3.4 轻量路径

| 项目 | 当前结果 | 判断 |
| --- | ---: | --- |
| 两份配置文件 `stat` | 中位约 0.123 ms | 保留现状 |
| 完整读取两份配置 | 中位约 0.402 ms | 保留现状 |
| 桥接 mutex 探测 | 中位约 0.0085 ms | 保留现状 |
| 缺失状态文件读取 | 约 0.0168 ms | 保留现状 |
| 应用动作路径解析 | 已安装约 20 至 78 ms；未找到约 89 至 109 ms | 适合进程级缓存 |

## 4. 已确认问题与改法

### PERF-001：拆开 BLE 顺序处理与阻塞音频输出

当前 `ble_transport_winrt.RC003BleSession` 的 worker 同时负责：

1. 合并控制队列与音频队列的全局顺序；
2. ATVV 解码；
3. 调用应用层控制回调；
4. 最终进入阻塞式 `OutputStream.write()`。

真实日志已经出现最高约 57 ms 的单次写阻塞和最多 21 个停止前尾部通知。现有
sequence 和 `_drain_audio_before_stop()` 保住了音频尾部不被 `AUDIO_STOP` 越过，
但代价是控制事件也必须等阻塞写返回。

建议单独立项，按以下结构修改：

- BLE worker 继续拥有通知排序、ATVV 解码和控制语义，不能把顺序责任分散到多个生产者；
- 新增有界音频播放队列，由独立播放 worker 或 PortAudio callback 消费；
- 每个 PCM 块携带会话和 sequence，`AUDIO_STOP` 以屏障标记进入播放队列；
- 只有播放端确认已处理屏障前的块后，控制层才完成本次音频停止和宿主收尾；
- 队列满时必须记录队列深度、最老块年龄和丢弃策略，不能无界增长；
- callback 内只取现成缓冲并填充输出，不能做解码、日志、文件、进程或其它可能阻塞的工作。

不能直接把 `AUDIO_STOP` 提到最高优先级，也不能简单改成另一个线程后立即释放宿主
快捷键；这两种做法都会重新引入已经修过的尾音丢失或停止越序。此项涉及真实设备
时序和音频合同，实施时按 L3 验证，必须覆盖长按、短说、快速连续两次、断开和停止边界。

### PERF-002：首屏只做显示所需的最少同步工作

`SettingsController.__init__()` 当前同步调用 `_refresh_endpoint_options()` 和
`_refresh_voice_program_status()`。前者首次约 239 ms，后者约 143 ms；语音程序
切换、启动结果等部分路径仍会再次同步检查。

建议：

- 先读取已保存端点并构造基础模型，让窗口尽快出现；
- 首帧之后在统一后台查询器中枚举端点，再通过 Qt signal 回写列表和选择状态；
- 所有语音程序状态检查统一走现有“后台执行、合并重复请求、丢弃过期结果”的模式；
- 同一个查询正在运行时只标记一次 pending，不为每次页面信号创建新线程；
- 保存端点前的 open/start/stop/close 预检保持同步业务合同，但继续放在明确的用户操作流程中。

### PERF-003：移除嵌套 `QEventLoop`

`_execute_voice_hotkey_call()` 虽然把文件和宿主 UI 操作放到了 Python 后台线程，
但 GUI Slot 会进入一个嵌套 `QEventLoop`，并用 10 ms `QTimer` 轮询完成状态。窗口
表面仍能响应，但 QML 绑定、动画和其它事件可在原调用尚未返回时继续执行，形成重入
边界；Qt 官方性能文档明确不建议用自建事件循环规避阻塞。

建议改成真正异步的任务入口：

- Slot 只做参数快照、设置 busy 状态并投递任务，随后立即返回；
- 任务完成后通过 signal 回到 GUI 线程执行成功、失败或回滚状态机；
- 可使用一个长期 worker object/QThread，或沿用受控 Python worker 加 Qt signal；
- 同一 provider 的读、写、回滚必须串行，不能为了异步而并发操作宿主设置；
- 关闭窗口时沿用有界停止和不向已销毁 Qt 对象回调的现有安全合同。

### PERF-004：把全量诊断移到首帧之后

`DiagnosticsController.__init__()` 会立刻调用 `refreshDiagnostics()`，一次运行包含
Windows 版本、Raw Input、BLE 候选、VB-CABLE、输出端点和人工听写状态。当前机器
一次约 649 ms，虽然在后台线程执行，但仍会启动 BLE 子进程和多项系统枚举，并扩大
设置窗口刚打开或立即关闭时的资源活动与清理范围。

设备页确实需要检查结果，因此不建议完全删除自动检查。建议先退出控制器和 QML 的
同步构造路径，再调度自动检查；如果目标是严格等首帧已经提交，应使用首帧信号或一次
短延时，不能把 `QTimer.singleShot(0, ...)` 直接等同于“首帧之后”。若以后改为按需
检查，必须同步调整“未检查、检查中、正常、需处理”的交互口径。仍保留现有的单
worker 合并、取消事件、子进程硬超时和关闭时有界清理。

### PERF-005：延迟构造隐藏 QML 页面

`main.qml` 的 `StackLayout` 直接声明 `DevicePage`、`ButtonsPage` 和 `VoicePage`，
所以按键页的多个 Repeater、Canvas、图片热点和语音页控件在设备页首屏前已经全部
构造。离屏实测完整 QML 加载约 133 至 180 ms、约 1273 个 QObject。

建议保留设备页同步加载，把按键页和语音页改为首次访问时创建、创建后保留的
`Loader`。不要每次切页卸载，否则会丢失编辑草稿、焦点和滚动位置。实施前用 QML
Profiler 确认对象创建和绑定耗时，实施后检查 objectName 自动测试、快捷键录入弹窗、
Canvas 连线、滚动位置和页面首次进入的状态刷新。

### PERF-006：缓存应用动作解析结果

`resolve_application_command()` 每次都会重新执行 PATH、常见安装目录、WindowsApps
和开始菜单扫描。应用动作本来就不在 Raw Input 原生回调中执行，因此当前不会堵塞
钩子；但每次触发仍额外增加约 20 至 109 ms。

建议使用进程级缓存，不写入用户配置：

- 按 `ActionKind` 缓存成功结果，启动前快速确认目标仍存在；
- 未找到结果使用短期负缓存，避免用户刚安装应用后长期失效；
- 启动失败时清除该项并允许下一次重新解析；
- 不持久化绝对路径，继续保持当前隐私和跨安装位置边界。

## 5. 当前不构成问题的路径

- 配置热加载只做两个 `stat`，变化后才读取 JSON，当前开销远低于 1 ms。
- 设置窗口的桥接状态轮询和状态文件读取很轻，当前 1 至 2 秒间隔无需改成复杂推送。
- 语音程序每 1.5 秒刷新已经在后台执行，并有 running/pending 合并；需要修的是仍然
  存在的同步入口，不是删除后台刷新。
- 单帧 ADPCM、滤波、增益、统计和重采样总量远低于一帧音频时长，当前没有理由用
  C/C++、Cython 或 Rust 重写。
- BLE 原生通知回调只做有界、非阻塞入队；控制队列和音频队列已经分开，音频满时
  丢最旧音频，控制队列满则失败重连。问题在下游 worker 同时承担阻塞播放。
- PCM 进度日志只在第 1、10 帧和每 200 帧记录，不是逐帧写日志。暂不引入
  `QueueHandler`；只有后续测到磁盘日志阻塞进入热路径时再改。
- PyInstaller 当前是 one-dir，不要为了“单文件看起来更整洁”改成 one-file；后者会
  增加启动解压和临时目录成本。
- 不应对所有 QML 绑定做手工微调。先用 QML Profiler 找到高频绑定，再处理真实热点。

## 6. 建议实施顺序

### 阶段 A：先建立可重复测量

- 设置启动记录：Python 入口、Qt 类加载、控制器构造、QML load、首帧完成；
- 音频记录：BLE 入队到处理延迟、PCM 队列深度和最老块年龄、播放写/callback 状态；
- 应用动作记录：首次解析、缓存命中、启动调用三个阶段；
- 使用 `cProfile` 看 Python 长路径，使用 `-X importtime` 看冷启动导入，使用 QML
  Profiler 看 QML 创建、绑定和绘制。三者用途不同，不能互相替代。

### 阶段 B：设置窗口响应

先处理 PERF-002、PERF-003、PERF-004，再根据 QML Profiler 结果处理 PERF-005。
完成标准是：首屏不等待端点枚举或语音程序检查；没有嵌套事件循环；自动诊断在首帧
后启动；页面首次访问后的功能和状态与当前合同一致。

### 阶段 C：音频与控制隔离

单独处理 PERF-001。先写队列、屏障、关闭和重连测试，再改真实播放结构，最后进行
RC003 真机短说、长说、快速连续会话、拔断和休眠恢复。自动测试通过但真机未完成时
只能写“检查点待实测”。

### 阶段 D：低风险小项

处理 PERF-006。日志异步化、Python 原生扩展、全局 asyncio/QThread 重构只有出现新
测量证据时才立项。

## 7. 建议性能门槛

以下是后续优化的目标，不是本轮已经通过的验收结论：

- GUI 线程普通 Slot：不执行可预期超过 16 ms 的系统、磁盘或跨进程查询；
- 设置首屏：分别记录源码和冻结程序的 P50/P95，不把离屏结果冒充真实首帧；
- 音频：持续保持 underflow 为 0，正常会话不丢控制事件，队列深度和最老块年龄有界；
- `AUDIO_STOP`：不能越过已接收的同代音频，宿主快捷键收尾不能早于播放屏障；
- 应用动作：缓存命中路径只做目标存在检查和启动调用，不重复扫描开始菜单；
- 长期运行：8 小时桥接的线程数、私有工作集和队列水位不持续单向增长。

## 8. 同类实现与官方依据

- [Qt Quick 性能建议](https://doc.qt.io/qt-6/qtquick-performance.html)：强调异步、
  worker thread、QML Profiler，并明确不建议用嵌套 `QEventLoop` 规避阻塞。
- [QML Loader](https://doc.qt.io/qt-6/qml-qtquick-loader.html)：支持按需实例化和跨帧
  异步创建，适合延迟构造隐藏页面。
- [Qt for Python QThread](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html)：
  官方 worker object + queued signal 模式可替代同步等待后台线程。
- [Python cProfile](https://docs.python.org/3/library/profile.html) 与
  [`-X importtime`](https://docs.python.org/3/using/cmdline.html)：分别用于运行路径和
  冷启动导入分析，不能拿 profiler 输出直接当微基准。
- [Python QueueHandler/QueueListener](https://docs.python.org/3/library/logging.handlers.html)：
  只有测到日志 I/O 阻塞热路径后，才考虑把 handler 工作移到独立线程。
- [python-sounddevice Stream API](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)
  与官方 [play_long_file.py](https://github.com/spatialaudio/python-sounddevice/blob/master/examples/play_long_file.py)：
  展示了 blocking write 与 callback 的差别，以及用有界队列预缓冲、callback 只取现成块的结构。
- [PortAudio callback 规则](https://portaudio.com/docs/v19-doxydocs/writing_a_callback.html)：
  callback 不能执行可能无界阻塞的分配、I/O、锁或系统调用。
- [Bleak Client](https://bleak.readthedocs.io/en/latest/api/client.html)：同类 Python BLE
  客户端采用 asyncio 生命周期和通知 callback，支持把通知接收与后续处理解耦；本项目
  当前原生 WinRT 回调快速入队的方向与此一致。
- [PyInstaller 运行模式](https://pyinstaller.org/en/stable/operating-mode.html)：本项目
  继续使用 one-dir，避免 one-file 启动时提取依赖。

## 9. 元素导航原型附录

实验性元素导航若以后进入产品，需要单独测量目标窗口枚举、UIA 树遍历、属性读取、
命中测试和覆盖层绘制，不能复用本次 RC003 主程序基线。

Microsoft 明确指出逐元素、逐属性读取会产生大量跨进程调用；应使用
[UI Automation 缓存请求](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-cachingforclients)
批量获取需要的属性和 Control Pattern，并根据 UI 变化事件刷新快照。UIA 调用还应
放在独立非 UI 线程，遵守
[UI Automation 线程规则](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)。
这只适用于元素导航原型，不是当前 BLE、按键或音频主链的优化依据。
