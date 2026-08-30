# Element Navigation

用方向键在 Windows 界面的可交互元素之间移动，并执行点击、双击、右键和滚动。
它不移动鼠标指针，而是通过 UI Automation、MSAA 和窗口几何识别可操作位置。

本目录是独立发布模板。导航算法的正式源码仍位于相邻的
`../rc003/scripts`，通过导出脚本生成一份可离开无线麦工程独立安装、运行和测试的
源码项目，避免两边长期维护两套算法。

## 当前边界

- 仅支持 Windows 10/11 和 Python 3.10 及以上版本。
- 独立版不依赖 `ovb_rc003`、遥控器、蓝牙、音频或无线麦配置。
- 无线麦继续把它作为独立伴随进程启动；两种用法共享同一份导航源码。
- 当前没有许可证文件，因此已经具备发布结构，但正式公开发布前仍需确定许可证。

## 导出独立项目

在本目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\export-source.ps1 `
  -Destination .\.build\ElementNavigation
```

导出目录包含独立的 `pyproject.toml`、依赖、源码、测试、归属说明和发布检查清单。
目标目录已存在时脚本会停止；只有显式增加 `-Force` 才会替换目标目录。

## 独立运行

进入导出目录后：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\element-navigation.exe
```

常用按键：

- `Ctrl+Alt+N`：进入或退出当前前台窗口的元素导航。
- 方向键：移动选区。
- `Enter`：点击；连续触发可执行双击。
- 菜单键：右键。
- 音量加减：在当前元素处滚动。
- `Esc`：返回上一层或退出导航。
- `Ctrl+Alt+Q`：退出程序。

只扫描一次当前窗口：

```powershell
.\.venv\Scripts\element-navigation.exe --scan-only
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

真实软件中的元素可达性、点击结果和长期运行仍需人工测试。
