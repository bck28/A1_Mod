# A1_Mod：不问凡尘提示词替换 Mod

从游戏资源包提取提示词到 `prompts/` 文本文件，再按你编辑的文件内容替换自定义模型请求中的系统提示词。游戏原文件只读，不修改程序或资源包。仅需 Python 3.10 或更新版本，无第三方依赖。

当前附带的原始提示词适用于 2026 年 9 月 30 日更新的《不问凡尘》版本，包含 22 组模板、3 种语言，共 66 个文本文件。仓库中的 `templates.original.json` 与 `prompts/` 均保留该版本游戏原文，下载后可直接编辑。使用其他版本或游戏更新后，请重新提取当前安装版本的提示词。

仓库提供 `config.example.json` 作为默认配置。如果本地缺少 `config.json`，先在项目目录运行以下命令，再编辑 `config.json`，填写实际模型服务的 `upstream_url`：

```powershell
Copy-Item .\config.example.json .\config.json
```

`config.json` 已加入 `.gitignore`，用于保留本地服务地址与游戏路径，不纳入提交。`config.example.json` 仅包含通用默认值，不含密钥或个人路径。API Key 在游戏内填写，或使用 `api_key_env` 指定的环境变量。

## 提取模板

1. 双击 `提取模板.cmd`，在弹出的文件夹选择窗口中选择游戏安装文件夹，也就是包含 `WorldApart_Data` 的目录。每次运行都会打开选择窗口。
2. 程序读取游戏的 `DefaultPackage.version`、对应 manifest 和资源包，提取当前版本提示词。成功后将选择的目录记入 `config.json` 的 `game_dir`，作为下次窗口的初始位置；取消选择不会提取或修改文件。
3. 当前游戏包含 22 组模板、3 种语言，生成 66 个 `.txt` 文件。游戏更新后可再次运行。

也可以在项目文件夹打开 PowerShell，用 `--game-dir` 指定游戏目录；此方式跳过选择窗口：

```powershell
python .\extract_prompts.py --game-dir "C:\Games\A1"
```

输出默认放在提取程序所在的项目文件夹。指定其他输出目录：

```powershell
python .\extract_prompts.py --game-dir "C:\Games\A1" --output "C:\Projects\A1_Mod"
```

提取结果包含：

- `prompts/<语言>/<模板名>.txt`：直接编辑的替换文件。
- `templates.original.json`：本次游戏原文匹配基准，请勿手动修改。
- `extraction_report.json`：来源版本、资源包 SHA-256、生成和保留文件统计等。

默认保留你已经修改的 `.txt`，补齐缺失文件；游戏更新时，原文未修改的文件自动刷新。若已修改文件对应的游戏原文改变，报告中的 `source_changed_for_edits` 会列出需要你检查的文件。程序保留你的编辑，但旧版文字或占位符可能需要按新版基准调整。

需要重新生成全部原文时，显式使用 `--overwrite`；覆盖前会在 `backups/` 中保存受影响文件的独立备份：

```powershell
python .\extract_prompts.py --game-dir "C:\Games\A1" --overwrite
```

此操作恢复模板原文，不清空 `extra_system_instructions.txt`。

## 恢复默认提示词

双击 `恢复默认提示词.cmd`，程序直接使用项目中的 `templates.original.json` 恢复所有语言的提示词，无需选择游戏目录。缺失的 `.txt` 会重新生成，编码损坏的文件也会恢复为 UTF-8 原文；`extra_system_instructions.txt` 会清空为一个空文件，关闭附加规则，包括旧配置中可能保留的附加规则。

覆盖前，已修改的模板和附加规则会自动备份到 `backups/restore_时间_编号/`，无需确认弹窗。恢复结果记录在 `restore_report.json`，其中包含恢复文件统计和备份位置。保存后，正在运行的 Mod 会在下一次请求读取默认内容。

恢复使用的是最近一次成功提取的游戏原文。游戏更新后，先重新运行 `提取模板.cmd`，再恢复默认，即可使用新版原文。不要手动修改或删除 `templates.original.json`；缺少该文件时需先从游戏提取。

也可以在 PowerShell 中运行：

```powershell
python .\extract_prompts.py --restore-defaults
```

`--restore-defaults` 可以与 `--output` 一起指定要恢复的项目目录，不与 `--game-dir` 或 `--overwrite` 同时使用。

## 编辑与运行

1. 编辑 `config.json` 的 `upstream_url`，填写你实际使用的模型服务地址，例如 `https://example.com/v1` 或完整的 `https://example.com/v1/chat/completions`。
2. 用记事本或其他文本编辑器修改 `prompts/zh-Hans/` 下的文件并保存为 UTF-8。繁体中文与英文分别使用 `zh-Hant`、`en-US` 文件夹。
3. 双击 `启动Mod.cmd`，保持窗口打开。
4. 游戏启用 AI NPC 自定义模型，API URL 填 `http://127.0.0.1:18666/v1`；若要求完整接口，填 `http://127.0.0.1:18666/v1/chat/completions`。API Key 和模型名填写原模型服务的配置。
5. 与 NPC 对话，查看窗口中的 `matched` 数量和 `logs/` 中的修改后请求，确认实际替换内容。

更新程序后重启 Mod 一次。此后保存 `.txt` 文件，下一次请求自动读取，不需要重启。可在项目目录运行 `python .\mod.py check` 检查配置、占位符和共享片段；`python .\mod.py` 默认启动服务。

常用简体中文文件：

- `prompts/zh-Hans/task.chat.txt`：闲聊规则。
- `prompts/zh-Hans/general_requirements.txt`：通用要求。
- `prompts/zh-Hans/rule.worldview.txt`：世界观。
- `prompts/zh-Hans/task.persuade.txt`：说服规则。
- `prompts/zh-Hans/task.topic.txt`：话题规则。
- `prompts/zh-Hans/format.chat.txt`：闲聊输出格式。
- `extra_system_instructions.txt`：附加系统规则，默认留空，同样自动读取。

每个模板文件写完整替换文本，无需 JSON、引号或额外标记。保留文件名、语言目录以及 `{HIDDEN_TRIGGER}`、`{DISPLAY_ACTIONS}` 等占位符和顺序。建议保留游戏要求的 JSON 输出格式及字段。

Mod 匹配原模板占位符之间的静态片段，保留游戏填入的动态内容。原本为空的占位符间隙不能加入文字；共享的原片段必须使用相同替换文本。缺失文件回退到原文；无占位符的模板保存为空可删除对应片段。有问题的编辑会在检查或请求时提示错误。

只改写请求中的 `system`、`developer` 和 `system_prompt` 文本。玩家与助手历史不被改写。官方模型模式不经过本地 Mod；提示词也不能直接改变游戏逻辑计算的进度、奖励或关系状态。

## HTTP 测试程序

`http_test.py` 是一个只抓包不改写的测试服务：把游戏发来的请求行、请求头、请求体原样打印到窗口，不替换提示词、不转发，然后返回一段固定回复让游戏可以正常继续。

`HTTP测试.cmd` 与命令行的效果相同：

```powershell
python .\http_test.py --port 18666 --pretty
```

常用参数：

- `--host`、`--port`：监听地址与端口，默认 `127.0.0.1:18666`。
- `--reply`：返回给游戏的固定回复文本。
- `--pretty`：请求体是 JSON 时缩进显示；不加则完全按原样输出。
- `--once`：处理一次请求后自动退出。

把游戏的自定义模型 API URL 指到 `http://127.0.0.1:18666/v1`。这与 Mod 使用同一个地址，两者不能同时监听同一端口：抓包前先关闭 Mod 窗口，或给测试程序换一个端口并同步修改游戏 URL。请求头会连同 `Authorization` 一起原样打印，其中包含 API Key，分享截图前先遮挡。

## 日志与停用

运行窗口会实时打印每次请求的对话消息和模型回复：收到的 `messages` 按角色逐条列出，系统与开发者提示词完整输出，模型返回的文本（流式 SSE 或完整 JSON）按顺序输出；提示词确实发生替换时，会再打印一份替换后实际转发给模型的内容。将 `print_dialogue` 设为 `false` 可关闭终端打印；将 `print_system_messages` 设为 `false` 可把系统提示词折叠为字符数摘要。打印只写入窗口，不改变转发内容和日志文件。

默认每次请求在 `logs/时间_随机编号/` 保存原始请求、修改后请求和替换统计。日志不记录请求头、API Key 或模型回复；正文包含对话与角色状态。将 `log_requests` 改为 `false` 可停止记录。

`unmatched_templates` 表示本次请求未匹配的已编辑模板；说服、话题和其他语言模板只在对应场景出现。浏览器打开 `http://127.0.0.1:18666/health` 可检查服务状态。修改监听端口后需重启 Mod 并更改游戏 URL。

停用时，将游戏 API URL 改回原模型服务地址或切回官方模式，再关闭 Mod 窗口。没有游戏文件需要还原。

开发验证：在项目目录运行 `python -m unittest discover -s tests -v`。本地模拟服务测试不消耗 API 额度；真实游戏内替换需通过实际对话与请求日志确认。
