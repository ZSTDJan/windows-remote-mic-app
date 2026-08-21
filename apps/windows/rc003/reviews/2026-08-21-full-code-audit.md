# 2026-08-21 RC003 Windows 完整代码审查

## 审查目标

在 `fix9` 真机反馈之后，对 Windows RC003 应用、测试、构建、安装和诊断脚本
做一次完整工程审查；确定性缺陷直接修复，需要改变产品语义的事项先核实。

审查基线：

- 分支：`fix/rc003-audio-hid-baseline`
- 起始提交：`aaf7d07`
- 代码修复提交：`eafd203`
- 范围：`apps/windows/rc003` 及其仓库级构建输入
- 不包含：Quicker 集成、完整产品重写、DJI Mic 2 新功能

## 审查方法

- 逐模块检查入口、状态机、线程、队列、Win32/WinRT/PortAudio/Frida 边界；
- 从完整 diff 反查每项行为变化和异常路径；
- 搜索进程启动、线程创建、持久化、宽异常捕获和隐私日志；
- 检查安装升级/卸载、构建脚本、PyInstaller spec 和诊断 CLI；
- 为发现的问题先补失败契约，再执行定向和完整测试；
- 执行 Python、PowerShell、QML、公开边界和冻结产物验证。

## 已修复的确定性问题

### 资源所有权与清理

- PortAudio open/close 失败时保留 stream owner，预检遗留流可重试清理；
- BLE worker、GATT service、device 关闭未确认时保留 session owner；
- Raw Input、热键录制、低层键盘钩子、HID tap 启停失败时不伪装为已清理；
- Doubao Frida script/session 按真实所有权清理，session detach 成功后不再因
  单独 unload 报错永久阻塞重启；
- 进程级 `KeyboardInterrupt` / `SystemExit` 在清理后继续传播；
- 远程 `LoadLibraryW` 未完成时不释放仍可能被读取的远程路径缓冲区。

### 输入与语音

- `SendInput` 部分交付且补偿 key-up 未确认时，保留普通按键或语音热键的待
  释放状态；释放成功前拒绝新动作；
- 旧连接 generation 的 gesture timer 不能在 reset 后触发宿主动作；
- CONTROL 与 AUDIO 拆成独立有界队列，控制溢出触发重连，音频只丢最旧帧；
- BLE worker callback 异常不能让线程静默死亡；
- TOGGLE 关闭 TAP 成功后，后续 `MIC_CLOSE` 失败只重连，不重复 TAP；
- 多项 WinRT writer、async task、回调和 event-loop 关闭竞态补齐失败处理。

### 安全与隐私

- HID tap loopback 连接通过 Windows TCP owner table 验证客户端进程必须是
  已核验目标 WUDFHost；身份未知或不匹配时读取前关闭；
- 日志改为轮转文件，持久 handler 移除 traceback 并把异常参数降为类型；
- 配置递归拒绝更多设备身份字段，且大小写不敏感；
- 按键捕获和广域 Raw Input 探针不再保存设备接口路径，异常只保存类型；
- bridge/settings 子进程启动错误不再保存完整系统错误文本；
- Gadget 消息缓冲有上限，非对象 JSON 不进入消息分发。

### 设置、进程与安装

- `config.json` 非对象根、`key_bindings.json` 非对象根明确拒绝；
- 设置双文件保存先验证两份内容，第二份失败时原子回滚第一份；
- bridge 子进程已创建但 `poll()` 失败时报告“状态未知”，不误称创建失败并
  诱导重复启动；
- 设置页无法证明 mutex 状态或清理 probe handle 时停止按键检测；
- 真实按键检测停止失败时保留资源 owner，不能继续开启第二套检测；
- 设置窗口退出时各清理步骤互相独立，不因前一步异常跳过诊断/音频清理；
- 安装升级和卸载在旧进程无法确认退出时停止；停止脚本只针对安装目录下
  精确 EXE，并用 PID + CreationDate 防止 PID 复用误杀；
- 公开边界 PowerShell 扫描与 Python 重放器的时间戳构建目录规则对齐。

### 诊断与可维护性

- key-detection 请求按真实修改时间选择最旧项，临时文件始终清理；
- 诊断子进程 `poll()` 失败时先确认子进程退出，再决定是否读取结果；
- `rc003_key_test.py` 在 listener stop 失败时保存诊断记录但拒绝保存映射；
- 新增长期维护的 `WINDOWS-ARCHITECTURE-LEDGER.md`。

## 自动验证

代码提交 `eafd203` 前完成：

- 完整 unittest：1090 项通过，7 项安全或平台条件跳过，退出码 0；
- 定向复核：Frida PID 身份、双文件回滚、TOGGLE 关闭策略、Qt 设置保存通过；
- 本机真实 loopback socket smoke：TCP owner lookup 返回当前客户端进程；
- `compileall`：通过；
- `pip check`：`No broken requirements found`；
- `check-public-boundary.ps1`：226 个文件通过；
- 全部项目 PowerShell 文件 parser：通过；
- `git diff --check`：通过；
- QML：完整测试中的真实 offscreen engine/load/render 契约通过；额外
  `qmllint` 返回 0，存在已有动态 singleton import/unqualified 警告，无错误。

最终 fix10 冻结构建、EXE/ZIP 哈希和 ZIP 内 EXE 复核在本文后续与
`MAINTENANCE.md`/`CHANGELOG.md` 中补记。

## 仍需真实设备验证

- HOLD 与 TOGGLE 在当前 fix10 上能否按界面选择表现不同；
- 长按、短按、快速重复按和长时间语音的首尾是否完整；
- 13 键映射、真实按键检测和 F5 防漏是否保持稳定；
- 断连重连、蓝牙关闭再开、系统休眠恢复；
- 30 分钟以上连续运行和多轮开关麦；
- VB-CABLE 安装后不同 host API/默认设备组合；
- 普通权限与管理员权限边界、SmartScreen/杀软兼容；
- 安装器原位升级、卸载、便携版跨机器展开。

以上未完成项不能写成“已通过”。历史 fix8/fix9 的真机证据只证明当时构建的
对应路径，不自动覆盖 `eafd203`。

## 已知设计边界

- HID tap 仍使用 loopback TCP，但已从“信任任意本机客户端”收紧为验证 TCP
  客户端进程必须等于目标 WUDFHost；不需要持久密钥。
- 双文件保存对普通失败可回滚，但普通文件系统不提供断电级两文件共同提交；
  需要该等级时应设计 journal 和 schema revision。
- Frida/WUDFHost 注入依赖管理员权限并可能被安全软件阻止；失败时缺失按键
  退化为不可用，不猜测伪造。
- 软件未签名，不自动安装驱动，不自动开机启动。
