# BUG-007 配对列表仅一个设备但 WinRT 返回两个候选

状态：`fix6` 异机真机连接验收通过

记录日期：2026-08-20

## 现象

异机曾连接过两个 RC003。用户已经在 Windows 设置中删除并重新配对，当前
界面只显示一个设备，但 2026-08-20 21:22:55 之后的新日志仍连续报告
`2 RC003 candidates found`。本次桥接因此在身份选择阶段失败关闭，没有进入
BLE/ATVV 连接；随后出现的日期时间输入是未被桥接截获的遥控器原生按键，
不能据此判断语音链路是否正常。

## 异机取证结果

- 该日志不是删除重连前的旧日志。
- `ble-probe1` 在 2026-08-20 22:08 后稳定记录
  `total=3 rc003_name_matches=2 unique_device_ids=3
  duplicate_device_id_entries=0`：两个 RC003 是不同设备 ID，不是同一 ID
  重复枚举。
- PnP 只读检查发现 `MI RC` 与 `小米蓝牙语音遥控器` 都是 `Present=True`、
  `Status=OK`，且属于两个不同的 `ContainerId` 分组；用户在普通蓝牙界面
  只能看到并确认当前使用的 `MI RC`。
- 精确删除中文名节点成功，但 `pnputil /scan-devices` 后 WinRT 仍再次枚举
  两个 RC003，22:24 后日志没有变化。这证明只删除 PnP 节点不能清除
  Windows 配对数据库中的记录，继续要求用户重复删除没有意义。
- 原始实例 ID 只出现在用户本机命令输出中，不写入项目文档、应用日志或
  候选产物。

## 诊断设计

- `discover_candidates()` 记录枚举总数、RC003 名称匹配数、唯一设备 ID 数、
  重复 ID 条目数和缺失 ID 数。
- 设备 ID 仅在进程内用于计数，采用 Windows 设备 ID 不区分大小写的比较；
  日志不写入蓝牙地址、原始设备 ID、哈希或设备路径。
- 本诊断构建不去重、不选择候选，也不改变原有两个不同候选时失败关闭的
  行为。

## 判定标准

- `total=2 ... unique_device_ids=1 duplicate_device_id_entries=1`：同一 WinRT
  设备记录被重复返回，下一步可在传输发现层按 ID 去重。
- `total=2 ... unique_device_ids=2 duplicate_device_id_entries=0`：系统仍保留
  两个不同的 BLE 设备记录，需要继续定位隐藏设备或增加明确的候选选择，
  不能自动猜测。

## 修复设计

- 单个名称匹配候选继续走原有快速路径，不增加连接延迟。
- 两个或更多候选时，对每个候选顺序执行最长 8 秒的 UNCACHED ATVV 语音
  服务探测；探测后立即关闭临时 GATT service 和 BluetoothLEDevice。
- 恰好一个候选可访问 ATVV 语音服务时选择它，再由正式 BLE session 按原
  流程连接、订阅和读取能力。
- 零个可用候选时报告无可达语音设备；两个可用候选时继续抛出歧义错误。
  任何情况下都不按枚举顺序、语言名称或持久化设备 ID 猜测。
- 探测日志只记录候选序号、总数、布尔结果和异常类型，不记录名称、地址、
  设备 ID、哈希或设备路径。

## 实施范围

- 实现：`src/ovb_rc003/ble_transport_winrt.py`。
- 测试：`tests/test_ble_transport_contract.py`、
  `tests/fakes/fake_winrt.py`。
- 真机操作只需退出旧桥接、运行诊断包约 20 秒并提交新日志；不删除设备，
  不重新配对。

## 自动验证与诊断产物

- BLE/身份/隐私定向测试：44 项通过。
- 完整测试：982 项通过，7 项平台或安全相关跳过。
- 公开边界扫描：224 个文件通过。
- PyInstaller 构建及冻结 `--dry-run`：通过。
- 诊断 ZIP：
  `RemoteMicRC003-0.1.0-candidate-20260820-ble-probe1-portable.zip`。
- ZIP SHA-256：
  `7E7AC18FFD39BAADCD011FC4613D3018CD7A1C266033154F46C47072AF94DDA1`。
- ZIP 内 EXE SHA-256：
  `8ADB9D9F62F40EAB90CB39A2D37E9CF7DBC20C930F81A81B38FB518C9F4373E4`；
  已从 ZIP 条目重新计算并与外部候选目录一致。
- 真机状态：诊断取证已完成，待 `fix6` 异机验收。

## `fix6` 验收目标

- 在异机保留当前 Windows 状态，不再删除或重新配对。
- 启动后应记录两个名称匹配候选的 ATVV 探测结果，并且恰好一个
  `reachable=True`。
- 随后应出现 `exactly one RC003 candidate resolved` 与
  `voice capabilities received`，再继续按住说话和切换模式测试。

`fix6` 自动验证：身份、BLE 合同与应用接线定向测试 40 项通过；完整测试
990 项通过、7 项跳过；公开边界扫描 224 个文件、`compileall` 与
`git diff --check` 通过。

异机真机结果：两个名称匹配候选分别得到一个 `reachable=True` 和一个
`reachable=False`，随后日志记录 `exactly one RC003 candidate resolved`、
`voice capabilities received`，HID tap 进入 `ready/hid_io_verified`。因此
多候选身份解析已通过；后续切换语音问题属于独立的 `BUG-008`。

`fix6` 便携 ZIP：
`RemoteMicRC003-0.1.0-candidate-20260820-fix6-portable.zip`，SHA-256
`60DE6082BEE84824073713B4235FB8B71A3356B0B5A955368D735AFCBF1820C3`；
ZIP 内 EXE SHA-256
`A168A28514D635725D2B08BA051986FDED4F52E725BFAD23AB77477B725C27DA`，
已与候选目录中的 EXE 复核一致。
