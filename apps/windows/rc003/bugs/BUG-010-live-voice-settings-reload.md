# BUG-010 运行中桥接未应用语音模式与快捷键设置

状态：自动验证通过，`fix9` 待异机真机验收

记录日期：2026-08-20

## 现象

用户在设置页分别选择“按住说话”和“按一下切换”，并保存不同的宿主快捷键，
但两种设置的实际行为仍然相同：长按遥控器麦克风键时有语音，松开后宿主
语音窗口仍保持开启，需要再按一次才能关闭。

实体键盘复核也确认，`ralt+space` 在当前宿主中本身不支持持续按住语义，
而 `ctrl+l` 可以。软件不能把宿主快捷键的行为硬编码成固定的搜狗预设，
必须按用户保存的生命周期和组合键分别执行。

## `fix8` 现场证据

- 全部普通按键可以在映射页识别，普通映射在记事本中有效。
- 按住麦克风键产生了约 2.2 秒的非零 PCM，汇总为 `result=signal`，宿主
  产生可见识别文字。
- 记事本没有再插入当前日期时间，说明低层 F5 防漏修复生效。
- 用户保存新设置后，日志只出现 `settings mappings reloaded from disk`，没有
  语音模式或快捷键应用记录；后续仍持续出现
  `voice toggle remains active after physical release`。

这些证据说明 BLE、ATVV、播放端点和 F5 抑制已经工作，剩余问题位于运行中
配置应用，而不是音频链路或设备配对。

## 根因

设置窗口会把 `voice_trigger_mode` 和 `voice_hotkey` 正确写入 `config.json`，
但桥接进程只在启动时读取该文件。运行中按键前的热加载只检查
`key_bindings.json`，因此界面提示保存成功后，后台仍一直使用启动时创建的
旧 `VoiceController` 和旧快捷键。

## 修复设计

- 同时监控 `config.json` 与 `key_bindings.json` 的修改时间。
- 在下一次 Raw Input、HID/F5、普通映射或 BLE-only `AudioStarted` 事件前，
  解析并验证新的语音生命周期和宿主快捷键。
- 当前没有语音会话时立即替换 `VoiceController` 和快捷键；会话仍活跃时先
  暂存，等现有手势、音频流和快捷键释放完毕后再切换，避免半途更换状态机。
- 配置损坏时保留最后一份有效运行值，不把半写或非法数据带入桥接。
- 启动和热应用时记录实际生效的 `trigger_mode` 与 `hotkey`，便于异机日志
  直接证明设置是否进入后台进程。
- 设置页提示改为：按键映射和语音触发在下一次按键时应用；连接设备和音频
  输出端点仍需重启桥接。

## 自动验证

- 空闲状态保存 `hold` + `ctrl+l` 后立即热应用。
- 只有 BLE `AudioStarted`、没有可用 Raw Input/F5 边沿时，也会先刷新设置再
  发送新的宿主快捷键。
- 切换会话仍活跃时保存新设置，旧会话完整关闭后才应用新状态机。
- 非法快捷键配置不会替换最后一份有效运行值。
- 完整测试：1006 项通过，7 项安全或平台相关跳过，退出码 0。
- 公开边界扫描：227 个文件通过；`compileall`、`pip check` 和
  `git diff --check` 通过。
- PyInstaller 构建与冻结入口检查通过：`--dry-run=0`、`--help=0`、无效
  HID 注入 PID 入口 `=4`。

## `fix9` 候选

- 便携 ZIP：
  `RemoteMicRC003-0.1.0-candidate-20260820-fix9-portable.zip`。
- ZIP SHA-256：
  `F24AFA07020385276297537E6B3F3F49880A305BC019460D3849778835CC778C`。
- ZIP 内 EXE SHA-256：
  `DB3F49E23F2B6EEB6C51B9B0B45C5A2E1B29EEEA2E8FA97FFB999A49A3E91C28`，
  已从 ZIP 条目重新计算并与候选目录一致。

## 真机门槛

1. 选择“按住说话”，保存实体键盘已确认支持按住语义的宿主快捷键；日志
   出现 `settings voice configuration applied: trigger_mode=hold`。按住时讲话，
   松开后宿主应按该快捷键自身的语义结束。
2. 选择“按一下切换”，保存宿主真正的点按开关快捷键；日志出现
   `settings voice configuration applied: trigger_mode=toggle`。第一次短按松开后
   仍能继续讲话，第二次短按才结束。
3. 两种模式各连续测试至少 5 轮；不能出现日期时间插入、快捷键卡住或模式
   与日志不一致。
