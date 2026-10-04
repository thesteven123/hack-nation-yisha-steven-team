# AI Lab 开发版快速上手

这是独立源码开发版。窗口、数据和本地服务使用专用开发目录；启动入口不安装、升级或重启安装版 AgentsDock。当前能力适合查文献、比较研究方向和运行已经接入的有限研究动作，完整 v0.5 科研系统的验收尚未完成。

## 第一次使用

1. 在仓库根双击 **Start-AILab.cmd**，或用 PowerShell 7 执行 `./Start-AILab.ps1`。本机默认连接专用的 `http://127.0.0.1:17851`。重复启动会检查已有开发窗口和开发服务。
2. 打开侧栏 **Idea 组 / Idea group**。输入目标，可补充假设、限制和现有材料，再明确点击开始。**Literature Agent** 实际查询、下载和读取文献；**Idea Agent** 根据证据比较方向。审阅使用另一个任务上下文，可以要求补查、修订或提出需要你回答的问题；它不是第三个常驻成员。
3. 默认先看当前摘要、覆盖缺口和下一步。需要核实时展开证据、原文、先前结果或技术记录。网页链接、工作回执和模型建议不能代替已经读取的证据；“可以选择方向”也不表示结论已被证明。
4. 选定方向后进入 **Research Lab / 研究工作台**，核对输入、允许的方法与额度，明确保存选择再执行。模型规划、分析、审阅和本地动作有各自的执行入口；打开记录只读取保存状态。

## 分支、提问与证据

- 研究分支需要你**显式开启**，每项研究最多 **3 个**。它们共享研究的总额度；分支会显示自己的问题、输入、依赖和下一步。需要答案的分支会等待，依赖它的分支也会阻塞；没有依赖关系的其他分支仍可推进。根研究暂停会阻止所有新工作。
- 回答具体问题后，重新规划、确认和运行相应分支。换目标、输入或更正上游证据后，旧结果仍可检查，但不能直接成为新的当前结果；复制同一结果到多个分支不产生独立重复实验。
- 新下载保留原始响应文件与当次读取来源；模型实际收到的材料可能只有摘要、部分正文或裁剪后的文本。界面分别显示覆盖范围，不把保存整份原文件当作模型已读全文。
- 在证据内先读取当时的来源文本，再展开 **Content version / 内容版本** 保存当时下载的原文件。保存使用精确的生成轮次与原件描述身份。旧记录缺少身份或原件未保留时，只能查看已有文本；不会重新下载并冒充历史原件。保存文件也不等于整个研究服务的备份。

## 环境与源码构建

Windows 需要 **PowerShell 7、Node.js 22+、pnpm**。已核对当前 `electron/package.json`：包管理器为 `pnpm@11.9.0`，提供 `typecheck`（`tsc --noEmit`）和 `build`（Electron 编译及输出校验）脚本；依赖版本以 `pnpm-lock.yaml` 为准。启动入口优先找 PATH 中的 PowerShell 7，再找标准安装路径和现有 Codex bundled runtime；找不到会直接说明，不会自动安装。

在仓库根用 PowerShell 7 准备源码构建：

```powershell
Set-Location electron
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
Set-Location ..
./Start-AILab.ps1
```

已有依赖时也可以在 `electron` 中执行 `npm run typecheck`、`npm run build`；依赖安装仍推荐使用仓库的 pnpm 锁文件。启动脚本不会自动安装依赖、编译或生成安装包。更多源码说明见 [Electron README](../electron/README.md)。

服务需要 WSL 中可用的 Linux 环境及独立虚拟环境；已核对 [server/pyproject.toml](../server/pyproject.toml)，Python 要求为 **`>=3.10`**，还须安装其中声明的依赖。同一 WSL 用户须已有 `codex` CLI 和有效原生登录；启动会把登录凭据复制到独立 CLI home，研究会消耗该账号的模型额度。它不会复制原会话、项目或 MCP 配置。

本机入口默认使用 `Ubuntu` / `agentsdock`、Python `/home/agentsdock/.cache/agentsdock-ai-lab-dev/server-venv/bin/python`，开发状态位于 `/home/agentsdock/.cache/agentsdock-idea-lab-dev`。Windows 开发配置位于 `%LOCALAPPDATA%\Programs\AgentsDockIdeaLabData`。移动仓库后脚本从自己的位置寻找源码；新机器须先准备上述环境，或传入 `-WslDistribution`、`-WslUser`、`-ServerPython`、`-ServerRoot`。

只启动专用服务可运行 `./Start-AILab.ps1 -ServerOnly`。需要重读修改后的服务源码时，先关闭开发窗口，再执行 `./Start-AILab.ps1 -StopServer`，随后重新启动。停止入口只调用带独立标记的开发服务停止逻辑。服务启动失败时，查看开发配置目录下 `logs\server-start\server.stderr.log`；端口被其他进程占用不会自动清理它。

## 当前验证范围

自测覆盖客户端/原生 HTTP 请求形状、历史证据的精确身份、原件字节与响应校验、取消及服务器切换归属、分支共享额度/依赖/局部等待，以及类型检查和生产编译。合成输入、受控服务和组件测试只说明各自经过的边界；实际开发窗口到服务的完整流程需另行记录验收。

**Research model 调用中按需读取已保存完整证据的工具链（E4）尚未完成 resolver 接入与备份证明；跨任务族的泛化实验仍未完成。** 当前模型拿到的摘要或有限材料不代表它已按需读取全部保存证据。已接入的有限方法与演示不证明适用于所有科研任务，也不证明科学发现、独立复现或效率提升幅度。完整科研状态、来源与工程限制见 [Idea Lab](../server/docs/IDEA_LAB.md)、[分支设计与验收](../server/docs/RESEARCH_BRANCHES_DESIGN.md) 和 [应用开发验收要求](APP_DEV_OPERATIONS.md)。
