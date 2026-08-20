# BUG-002 部分遥控器按键无法检测

状态：检查点待实测

记录日期：2026-08-20

## 现象

设置页“检测真实按键”能识别方向键、确定键、电源键等 Raw Input 按键，
但部分实体键没有任何识别结果。

## 证据与根因

- 该设置入口只创建 `RawInputButtonListener`，没有启动 Frida HID tap。
- Windows 普通键盘链路不会暴露 RC003 的返回、音量加和音量减 usage；
  这些键必须从 WUDFHost 内已完成的 HID-over-GATT 读取中取得。
- `RC003HidReportTap.start()` 在线程启动后立即返回成功；调用方随后记录
  “enabled”，但此时尚未证明目标进程注入成功、Gadget 已连接、心跳正常
  或收到真实 HID IO。
- tap 的关键状态与异常使用 `print`；窗口子系统 EXE 没有可靠可见的
  stdout，因此现场日志无法解释失败发生在哪一步。
- 2026-08-20 的 `fix3` 管理员真机启动显示隐藏注入器持续返回退出码 3。
  外置管理员诊断进一步确认：在启用 `SeDebugPrivilege` 前，使用
  `PROCESS_QUERY_LIMITED_INFORMATION` 查询已锁定的 WUDFHost 会返回
  `WinError 5`；启用该权限后，对同一 PID 的进程名查询立即成功。
- `inject_current_process()` 当时先调用 `_target_process_name()`，最后才调用
  `enable_debug_privilege()`，所以注入尚未开始就被错误的权限顺序挡住。
- 注入失败没有记住对应宿主 PID，状态会每两秒在 `injecting` 与 `failed`
  之间切换，对同一系统进程反复尝试并持续刷日志。

结论：一部分是设置页功能覆盖不完整，另一部分是运行状态被过早宣布且
诊断信息写入了错误的输出通道。

## 修复设计

- 为 tap 建立明确状态：等待宿主、注入中、等待连接、已连接等待 IO、
  就绪、不健康、失败和已停止。
- 所有状态和错误通过注入的日志回调进入 `app.log`；线程启动只表示
  “正在启动”，收到有效 HID 读取后才能表示“就绪”。
- 通过独立子进程执行窄范围注入，参数使用数组传递，不经过 shell；
  保留 PID 重查、WUDFHost 进程名、固定 Gadget 哈希、超时和退出码检查。
- 设置页同时启动 Raw Input 与 HID tap；tap 只补齐普通输入链路缺失的
  usage，任一来源捕获到首个按下事件后统一停止，不执行已配置动作。
- 注入器先启用 `SeDebugPrivilege`，再校验 WUDFHost 进程名、Gadget 哈希并
  执行注入；权限、校验和未知失败使用稳定且可操作的脱敏状态名。
- 同一 WUDFHost PID 注入失败后保持稳定 `failed`，只在宿主 PID 改变后
  重新尝试，避免重复注入和状态刷屏。

## 验证门槛

自动检查：

- 线程创建不会被报告为 ready。
- 注入失败、连接等待、心跳过期和首个有效 IO 都产生确定状态与日志。
- 设置页可以从 HID tap 捕获缺失 usage，且停止时同时释放两个监听器。

人工检查：见 `../TESTING.md` 的 `TEST-KEY-001` 与 `TEST-KEY-002`。
必须逐键观察一对 down/up、无重复触发后，才能写“真机按键已通过”。

## 实施与提交

- 主要实现：`src/ovb_rc003/__main__.py`、`src/ovb_rc003/app.py`、
  `src/ovb_rc003/frida_compat.py`、
  `src/ovb_rc003/frida_hid_tap_injector.py`、
  `src/ovb_rc003/qt_settings_app.py`、
  `src/ovb_rc003/qml/ButtonsPage.qml`。
- 回归测试：`tests/test_app_wiring.py`、`tests/test_frida_compat.py`、
  `tests/test_main_entrypoint.py`、`tests/test_qt_settings_app.py`。
- 自动验证：HID、入口、Qt 与应用定向测试 170 项通过、1 项跳过；最终
  完整测试 962 项通过、7 项跳过；公开边界扫描 215 文件通过；冻结隐藏
  入口无效 PID 检查返回退出码 4。
- `fix4` 管理员冻结桥接已从 `injecting` 进入
  `waiting_for_gadget_connection` 和 `attached_waiting_for_hid_io`，不再出现
  退出码 3；尚未收到实体键输入，因此不能把该检查写成 `ready` 或逐键通过。
- 初始实现提交：`88ea7a90144ff078b7abc62de9dedc4290043fe2`。
- 权限顺序与稳定失败修复提交：
  `9daf49fc8fae1ecc5824fca4360ca0a6f968c55b`。
