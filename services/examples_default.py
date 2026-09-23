"""示例块**框架**（C1/C2）—— 只保留 HEADER 与加载契约，示例语料不在仓库里。

🔴 2026-09-23 脱敏：仓库是 public，**示例语料属于真实群聊数据**（示例块源自真实群语句），
因此整批移到**仓库外**的 `data_out/examples.json`，部署时 `scp` 到
`<plugin_data>/example_dialogs.json`（`examples.json` 亦可，加载器两个名字都认）。

所以本模块的 `ENTRIES` **恒为空列表** —— 它不是"默认文案"，而是"没有内置语料"这一事实。
加载链在 `services/examples.py`：

    <data_dir>/example_dialogs.json 可解析且有条目   → 用它（部署形态）
    <data_dir>/examples.json        （同一个 loader 的别名）
    都没有 / 坏了 / 空                              → ENTRIES（空）→ **空示例块**

⚠️ 空示例块 = `ExamplesState.source == "none"`，**必须留痕**（本项目第一病根）：
启动日志 `[examples] ⚠️ …` 与 pipeline trace `examples_degraded` 都会报。

⚠️ 历史口径（用户 2026-09-20）不变：**任何情况下不回退到旧示例句**。
旧句（含 `口癖丙` 那一代）在这份代码里一个字都不存在，现在**整批语料**都不在。
"""
from __future__ import annotations

#: 头部（草案 §A：删掉了旧的两条「规则A/规则B」—— 规则B 已证伪，规则A 是错误示例）。
#: 这是**框架文本**（只说"这里是一段示例"，不含任何具体信息），随代码保留。
HEADER = '示例对话（就是这种语感，照着说）：'

#: 内置示例语料 —— **框架态恒空**。真实 20 条在仓库外 `data_out/examples.json`。
#: 保留这个名字是为了让调用点（`services/examples.py` 的回落分支）不必感知仓库形态；
#: 判空即可得知"没加载到语料"。
ENTRIES: list[dict] = []
