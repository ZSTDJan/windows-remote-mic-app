# Windows RC003 测试

当前状态：`0.2.0-candidate.2` 发布准备。

## 自动检查

在 `apps/windows/rc003` 中运行：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v
.\build\check-public-boundary.ps1
.\.venv\Scripts\python.exe build\check-third-party-notices.py
.\.venv\Scripts\python.exe build\check-release-readiness.py
```

正式 tag 必须把最后一条命令改为 `--enforce`，并通过完整候选构建。

## 真机验收

2026-09-01，用户确认现有功能已经完成真机使用验证。后续只修改文档、Git 历史、
授权记录或版本号时可以沿用该结果；修改运行代码、依赖或构建内容时，应重新验证
受影响范围。

正式候选至少确认：

- RC003 配对、断开重连、13 键识别和已保存映射；
- 按住话筒键传声，松开后正常结束且没有卡键或重复输入；
- VB-CABLE 通道，以及实际使用的搜狗、微信或 Windows 语音输入文字上屏；
- 休眠唤醒、通知区域恢复、完全退出和登录启动；
- 常用按键、元素导航和长时间运行没有明显异常。

未签名程序的 SmartScreen 或杀毒软件提示应在发布说明中如实注明。
