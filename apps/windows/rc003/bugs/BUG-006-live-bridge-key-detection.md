# BUG-006 后台桥接运行时设置页无法检测按键

状态：自动验证通过，`fix5` 待本机真机验收

记录日期：2026-08-20

## 现象

用户从通知区域打开设置后，点击“检测真实按键”再按遥控器，界面不再高亮
对应按键；与此同时，普通映射在记事本中仍能正常执行。

## 根因

- 后台桥接已经持有 HID tap 的本机端口，并通过低层钩子吞掉原始键后执行
  映射。
- 托盘启动的设置窗口是第二个进程；它再次启动 HID tap 会发生端口冲突，
  Raw Input 也看不到已经被后台桥接截获的那次事件。
- 因而不是遥控器停止上报，而是设置进程试图与资源所有者重复监听。

## 修复设计

- 设置页通过既有 per-session named mutex 判断后台桥接是否正在运行。
- 桥接运行时，设置页不再启动第二套 Raw Input/HID tap，只在配置目录创建
  一个 UUID 一次性检测请求。
- 后台桥接捕获下一次逻辑按键后写回结构化结果，并吞掉该按键的一对
  down/up，不执行用户映射。
- QML 每 100 毫秒轮询结果；捕获后选中并高亮对应按钮，15 秒无结果则超时
  清理。30 秒以上的请求、claim 和结果文件会作为陈旧数据删除。
- IPC 只保存 schema、随机 token、时间和逻辑 `button_id`，不记录蓝牙地址、
  HID 设备路径、键盘内容或设备标识。
- 后台桥接未运行时，保留设置页原有的本地 Raw Input + HID tap 检测路径。

## 验证门槛

- 两个并发按键只能有一个原子 claim 成功。
- 捕获普通键和麦克风键时都不执行原映射或语音动作，匹配 release 后解除
  临时吞键状态。
- Qt 控制器在桥接运行时不创建本地监听器，能轮询结果、选中按钮并在超时
  后准确清理。
- 真机必须从托盘打开设置，点击“检测真实按键”，至少用返回键和音量键
  验证高亮且该次映射不执行。

## 实施与提交

- 主要实现：`src/ovb_rc003/key_detection_bridge.py`、
  `src/ovb_rc003/single_instance.py`、`src/ovb_rc003/app.py`、
  `src/ovb_rc003/qt_settings_app.py`、`src/ovb_rc003/qml/ButtonsPage.qml`。
- 回归测试：`tests/test_key_detection_bridge.py`、
  `tests/test_bridge_instance_status.py`、`tests/test_app_wiring.py`、
  `tests/test_qt_settings_app.py`。
- 完整自动验证：980 项通过、7 项跳过；公开边界扫描 223 个文件通过。
- 对应提交：`ca5b69885a3796901dc36f1578cb7a754d73f140`。
