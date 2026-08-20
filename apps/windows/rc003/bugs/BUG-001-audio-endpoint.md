# BUG-001 语音已触发但无音频

状态：检查点待实测

记录日期：2026-08-20

## 现象

RC003 麦克风键能够唤起和结束宿主语音输入，但说话没有识别结果。

## 证据与根因

- BLE 连接、ATVV 能力协商、语音开始和停止事件均已到达应用。
- 每次会话的 PCM 汇总均为 `frames=0 samples=0`。
- 已保存端点使用 `Windows WDM-KS`，PortAudio 报告阻塞 API 不受支持，
  因而输出流在接收 PCM 前打开失败。
- 同机以 `CABLE Input / Windows WASAPI` 和
  `CABLE Input / Windows DirectSound` 打开输出流成功。
- 本机合成回环检查已从 CABLE Input 写入并由 CABLE Output 捕获到非零
  音频，证明 VB-CABLE 驱动链路本身可工作。

结论：问题位于应用的端点枚举、选择和保存校验，不是 RC003 BLE/ATVV
传输失败，也不是 VB-CABLE 安装失败。

## 修复设计

- 当前阻塞播放实现明确拒绝 `Windows WDM-KS`。
- 同名多 host API 视图按 `Windows WASAPI`、
  `Windows DirectSound` 的顺序选择；同一优先级仍不唯一时继续失败关闭。
- 设置保存和“选择检测到的 CABLE Input”在落盘前实际打开、启动并关闭
  所选输出流，不能只验证端点文字存在。
- 语音的“按住/切换”生命周期与具体快捷键分开保存，选择生命周期不再
  擅自覆盖用户录制的宿主快捷键。

## 验证门槛

自动检查：

- WDM-KS 端点不会出现在可选播放端点中，手工构造时也会被拒绝。
- 多个 CABLE Input 视图优先选 WASAPI，缺失时选 DirectSound。
- 端点预检真实 open/start/close 失败时不保存。

人工检查：见 `../TESTING.md` 的 `TEST-VOICE-001` 至
`TEST-VOICE-003`。在这些检查完成前不得写“真机语音已通过”。

## 实施与提交

- 主要实现：`src/ovb_rc003/audio_output.py`、
  `src/ovb_rc003/audio_playback.py`、`src/ovb_rc003/config.py`、
  `src/ovb_rc003/qt_settings_app.py`、
  `src/ovb_rc003/qml/ConnectionPage.qml`。
- 回归测试：`tests/test_audio_output.py`、
  `tests/test_audio_playback.py`、`tests/test_config.py`、
  `tests/test_qt_settings_app.py`。
- 自动验证：音频定向测试 284 项通过、2 项跳过；完整测试 947 项通过、
  7 项跳过；冻结候选构建成功。
- 对应提交：`88ea7a90144ff078b7abc62de9dedc4290043fe2`。
