# BUG-007 配对列表仅一个设备但 WinRT 返回两个候选

状态：诊断构建待异机取证，尚未修改设备选择规则

记录日期：2026-08-20

## 现象

异机曾连接过两个 RC003。用户已经在 Windows 设置中删除并重新配对，当前
界面只显示一个设备，但 2026-08-20 21:22:55 之后的新日志仍连续报告
`2 RC003 candidates found`。本次桥接因此在身份选择阶段失败关闭，没有进入
BLE/ATVV 连接；随后出现的日期时间输入是未被桥接截获的遥控器原生按键，
不能据此判断语音链路是否正常。

## 当前判断

- 该日志不是删除重连前的旧日志。
- Windows 可见配对列表与 WinRT 配对 BLE 枚举结果不一致。
- 现有日志只能证明名称匹配候选为两个，无法区分同一
  `DeviceInformation.id` 被重复枚举，还是存在两个不同的隐藏/陈旧记录。
- 在没有这一区分前，不应静默选择其中一个，也不应要求用户继续重复删除
  和重新配对。

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
- 真机状态：待异机回传 `paired BLE discovery` 新日志。
