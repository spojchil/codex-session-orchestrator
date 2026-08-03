---
name: codex-session-orchestrator
description: >
  调用本机 Codex CLI 作为外部持久执行代理。以 codexctl 为核心：后台派发并秒级返回
  session id、实时结构化状态文件（阶段/token/上下文占用/改动/性能）、运行中注入新提示词
  （steer）、可靠取消（进程树级）、主动上下文压缩、健康守卫，以及跨会话检索本地 JSONL 记录。
---

# Codex Session Orchestrator

把 Codex CLI 当作可派发、可观测、可控制的后台执行代理。三个入口：

- `scripts/codexctl.py` —— 派发与控制（主力）
- `scripts/codex-trace.py` —— 会话记录考古与检索
- `scripts/dispatch_codex_agent.py` —— 阻塞式单次派发脚本（无监督与状态文件，适合最简单的等待场景）

## 前置条件

- Codex CLI 已安装且已登录（`codex --version` 能跑）。
- Python 3，只用标准库。
- 会话记录默认读 `~/.codex/`；装在别处就设 `CODEX_HOME`。
- 运行注册表默认在 `~/.codex-orchestrator/runs/`；可用 `CODEX_ORCH_HOME` 改。

安装二选一：作为 plugin 安装（`/plugin marketplace add` + `/plugin install`，见仓库
README），或手动把整个目录放到 `~/.claude/skills/codex-session-orchestrator/`
（Windows 为 `%USERPROFILE%\.claude\skills\`），`SKILL.md` 与 `scripts/` 同级。

下文命令里的 `<技能目录>` 指本 SKILL.md 所在目录（plugin 安装时以加载器给出的实际
路径为准；手动安装即上述 skills 路径）。

## 派发：dispatch

```bash
python <技能目录>/scripts/codexctl.py dispatch \
  -C /path/to/project --sandbox workspace-write "任务描述"
```

立即返回（不阻塞、不等任务跑完）：

```text
run_id: r-20250101-120000-ab12
session_id: 01900000-0000-7000-8000-000000000000
state: ~/.codex-orchestrator/runs/r-20250101-120000-ab12/state.json
watch: python .../codexctl.py watch r-20250101-120000-ab12
```

常用选项：`--model`、`--reasoning-effort low..ultra`、`--profile`、
`--sandbox read-only|workspace-write|danger-full-access`、`--add-dir <目录>`（可重复）、
`--session-id <id>`（续接）、`--fork-from <id>`（复制父会话再执行，父会话不动）。
`--wait` 为阻塞式用法（打印最终回复后才返回）。

**长任务书用 `--prompt-file <路径>`**（dispatch / resume / steer 都支持）：内容含反引号、
引号、多行 Markdown 时不再经受任何 shell 转义，先写文件再派发。

健康守卫选项（超阈值写告警；`--on-alert cancel` 则自动软取消）：

```text
--max-silence 600          连续 N 秒无 Codex 事件（默认 600，0 关）
--max-commands-no-diff N   连续 N 条命令没有产生文件改动
--max-items N              条目总数上限
--max-runtime N            总时长上限（秒）
--max-repeat-command N     同一命令重复 N 次（仅计"完全相同且期间无文件改动"的重复；
                           修一次跑一次的正常修复循环不会触发）
--allowed-path DIR         文件改动越出这些根目录即告警（可重复；相对路径以 -C 为锚，
                           规范化结果写在 state.json 的 allowed_paths 里）
--context-alert-pct 90     上下文占用百分比告警线
--on-alert warn|cancel     告警后动作（默认 warn）
```

## 观测：status / watch / events / list

```bash
python .../codexctl.py status            # 最近一个 run；也可给 run_id 或唯一前缀
python .../codexctl.py watch <run>       # 刷屏跟踪直到结束，结束时打印最终回复
python .../codexctl.py events <run> -n 30
python .../codexctl.py list              # 全部 run，并实况核对：LIVE = 进程真活着
python .../codexctl.py list -C           # 只看当前目录工作区的（-C <目录> 指定别处）
```

所有吃 run 的子命令（status/watch/events/cancel/steer/ccr）同时接受位置参数和
`--run-id <run>`，两种写法等价。

**"我关了那个终端，代理还活着吗？"——跑 `list` 就知道。**派发方终端关闭不影响任务
（监督进程是独立的分离进程），在任何终端、任何目录都能列出全机的 run 并核对实况：
`LIVE Np` 表示监督进程活着且 Job 里有 N 个进程；账面在跑但进程已死的孤儿记录会被
自动修正为 `failed` 并补写终态（标 `healed`）。

`status` 一屏包含：阶段（requesting_model / reasoning / running_tool / completed / failed /
cancelled）、正在执行的命令、计划完成度（Codex 自己维护的 todo list）、token 用量与
**上下文占用百分比**、改动文件（来自事件流）、健康告警、进程树 CPU/IO/内存峰值。
`--json` 输出机器可读的完整状态。`status` 也接受 **session id**——自动解析到该会话
当前最新的 run（旧 run 的状态里会有 `superseded_by` 指向接替者）。

健康告警分两类：**条件类**（context_high、stalled、no_diff 等）实时重估，条件消失即自动
resolve——压缩后上下文降回去，`context_high` 会自己消掉，`health.current` 只含当前仍
成立的告警，历史在 `health.history`；**违规类**（越界写文件、命令重复）保留整个 run 可见。

`stream:` 行显示会话文件最近写盘时间——模型长推理期间 `--json` 通道安静，但会话文件仍在
写，它同时计入卡死计时（部分信号：单次超长请求两个通道都可能安静）。

`state.json` 里的 `cli_entrypoint` 字段是本工具的完整调用前缀，编排方直接复用即可——
**没有裸 `codexctl` 命令**，一律 `python <技能目录>/scripts/codexctl.py`。

**工作树的权威状态自己跑 `git status` / `git diff --stat` 去看**（在任务的 `-C` 目录里），
或者用 `status <run> --diff` 让工具**按需**做一次快照（相对 baseline 的增删行、文件清单、
未提交数）——只在你要的时候跑一次，运行期间从不周期性碰 git。任务结束时会记录一次
未提交改动数进 `result.json`。状态文件里的改动文件来自 Codex 的补丁事件，它用命令
直接写的文件不在其中。

run 目录里可直接读的文件：`state.json`（实时状态）、`result.json`（终态：退出码、原因、
推荐 resume 命令）、`last-message.md`（最终回复）、`events.jsonl`（全部事件+时间戳）、
`alerts.jsonl`（告警history）。**即使派发方进程退出，这些照常落盘。**

### 可选：让代理主动上报进度

在任务书里加一条约定即可启用：

```text
进度上报：每完成一个阶段，向工作区根目录的 .codex-progress.jsonl 追加一行 JSON：
{"phase": "...", "completed": ["..."], "next": "...", "blocker": null}
```

监督进程自动 tail 这个文件（文件名可用 `--progress-file` 改，解析后的绝对路径在
`state.json.progress_path`），最新一条显示在 `status` 的 `report:` 行；**每次上报会
重置无事件卡死计时**，长命令的静默期不再误报 stalled。上报是语义补充，不作为存活的
唯一判据——走偏或卡住的代理可能根本不上报。

**跨 run 不冒充**：续跑的新 run 会把工作区里已有的旧上报标为 `[historical, previous run]`
展示，只有本 run 的新上报才算当前进度（带 `origin_run_id`）——上一轮的
"milestone=completed" 不会掩盖正在返工的事实。

## 中途改方向：steer（语义 = interrupt-and-resume）

```bash
python .../codexctl.py steer <run> "纠偏提示词"        # 或 --prompt-file <路径>
```

**明确它是什么**：在条目边界优雅停下正在跑的任务（避免命令执行到一半被拦腰），然后用
**同一个 session** 续接新提示词——是"打断再续跑"，不是无中断的旁路消息（`codex exec`
没有给运行中进程的输入通道，这是结构限制）。停止那一刻之前的上下文全部保留。
`--grace N` 控制等待边界的秒数（默认 20，超时强停）。

输出最后一行是结构化 JSON：`{"steer": {"old_run", "new_run", "session_id", "boundary"}}`
——**监控目标要切到 new_run**；旧 run 的状态会写入 `superseded_by`，会话索引
（`~/.codex-orchestrator/sessions.json`）始终指向当前 run。

**boundary 是一等信号，读它判断这次纠偏的代价**：

| boundary | 含义 | 代价 |
|---|---|---|
| `completed` | 旧 run 早已终态，直接续接 | 无损 |
| `item` | 在条目边界停下 | 低（已完成的工作全保留） |
| `forced` | 宽限期内没等到边界，强停 | **有损**：在途的远端推理被丢弃，按一次真实中断对待 |

等边界期间 `status` 会显示 `pending:` 行（已请求、正在等边界）。

只想停不想续：

```bash
python .../codexctl.py cancel <run>            # 软取消，等条目边界
python .../codexctl.py cancel <run> --hard     # 立即杀整棵进程树
```

取消是进程树级的（含孙进程与残留 host），监督进程意外死亡也能兜底杀干净。

### 备选：手动杀进程 + 同 id 续接

不是 codexctl 派发的任务（比如别的终端直接 `codex exec` 起的，没有 run 注册表）仍然可以
中途改方向：自己杀掉 codex 进程，然后续接同一个 session：

```bash
python .../codexctl.py resume <session-id> "纠偏提示词"
```

会话实时写盘，被杀那一刻之前已完成的内容都在。但相比 `steer` 有明确劣势，能用 steer 就用 steer：

- **杀的时机是盲的**：steer 等条目边界，手动杀可能把正在执行的命令拦腰截断，
  工作树里留下改到一半的文件（实际发生过）。
- **在途的推理作废**：正在进行的模型请求整个丢弃，token 和几分钟的等待白花；
  steer 停在边界，浪费最小。
- **杀单个 PID 杀不干净**：codex 是一棵进程树（shim → node → codex → 沙箱子进程），
  漏掉的子进程可能占着文件锁继续写盘；还有 PID 复用杀错进程的风险。
- **没有终态记录**：不知道它停在哪一步，得自己去看 git status 和会话文件核对进度；
  steer/cancel 会留下 result.json（停在哪个边界、退出码、推荐命令）。

## 续接与压缩

```bash
python .../codexctl.py resume <session-id> "后续任务"     # dispatch --session-id 的简写
python .../codexctl.py compact <session-id>               # 主动触发上下文压缩
python .../codexctl.py fork <session-id>                  # 只分叉不执行，返回新 id
```

**`resume` 不带 `-C` 时自动继承该会话上一个 run 的工作目录**——手工续跑不会再落到
编排方当前所在的目录（多工作树 + 宽沙箱下那是把半成品写进主仓库的路）。显式 `-C`
仍可覆盖，但与会话历史目录不一致时会打警告。`recommended_resume` / `result.json`
里的续跑命令永远带绝对 `-C`。

`compact` 会临时拉起 app-server 对该会话做真实的上下文压缩（压缩事件写进会话文件，
完成后自动确认）。长会话在 `status` 显示上下文占用偏高时先压缩再续接。

**长任务的标准压缩流程用一条原子命令**（推荐 60% 关注、75% 写检查点、80% 压缩的节奏）：

```bash
python .../codexctl.py ccr <run> --prompt-file 续跑提示.md
```

`checkpoint-compact-resume`（别名 `ccr`）一口气完成：边界停下 → 让代理写 CHECKPOINT.md
（已完成/半成品/恢复第一步/剩余清单）→ 压缩并验证压缩事件 → 用续跑提示恢复，输出新
run id。压缩未确认会**在恢复前中止**，会话与检查点原样保留，可加大 `--timeout` 重试。

三点须知：

- 事务全程记录在旧 run 目录的 `ccr.json`（各阶段时间戳、子 run id）。ccr 客户端比调用它
  的 shell 长命——外层超时不代表事务失败，去看 `ccr.json` 和会话索引即可接续追踪。
- `--checkpoint-prompt` 覆盖检查点指令时，**必须写成"禁止继续实现、只做状态快照"**，
  并自带绝对目标路径；写成"完成当前小项后再存档"实测会变成几十分钟的继续施工。
  `--timeout` 只约束压缩确认阶段，不是整个事务的总时限。
- 一次 ccr 的 `compactions` 计数可能 +2 属正常：检查点短轮自身可能触发一次自动压缩，
  加上显式压缩各计一次。

`fork`（以及 `dispatch --fork-from`）优先走 Codex 原生分叉：谱系正确、正规入库、
且在 TUI resume 里默认可见；原生面不可用时自动回退为手工复制会话文件。父会话
始终一字不动。用完的实验性 fork 直接删：

```bash
codex delete --force <fork-id>     # 会话文件与索引一起删干净
```

## 第三方端点与多配置（profile）

Codex 原生支持多配置文件：`$CODEX_HOME/<名字>.config.toml` 会叠加在基础 `config.toml`
之上，派发时加 `--profile <名字>` 选用（`dispatch` / `resume` 都透传这个参数）。
接第三方 OpenAI 兼容端点就用它：

```toml
# ~/.codex/third.config.toml
model = "your-model-name"
model_provider = "myproxy"

[model_providers.myproxy]
name = "my proxy"
base_url = "https://api.example.com/v1"
env_key = "MYPROXY_API_KEY"   # key 从这个环境变量读取
wire_api = "chat"             # 端点只兼容 chat completions 就填 chat，否则 responses
```

```bash
export MYPROXY_API_KEY=sk-xxx
python <技能目录>/scripts/codexctl.py dispatch --profile third -C <项目> "任务"
```

API key 只走环境变量：沿派发链自然继承到 codex 进程，不进状态文件、事件日志或命令行。

**一条会话线从头到尾用同一个 profile。**会话按线记录 provider，换 provider 去 resume
同一条会话的兼容性未验证；要换端点就开新会话。

## 会话考古与检索：codex-trace

```bash
python .../codex-trace.py -l                    # 最近会话列表（* 在跑  ^ 子代理  a 归档）
python .../codex-trace.py -l -p /path/to/project
python .../codex-trace.py --tree                # 按谱系分组（fork + 子代理边都算）
python .../codex-trace.py --grep "关键词"        # 跨会话正文检索（正则，忽略大小写）
python .../codex-trace.py <session-id> -n 10    # 某会话最近 N 轮（含上下文占用）
python .../codex-trace.py <session-id> --last-message
python .../codex-trace.py -l --archived         # 把 codex archive 归档的也算上
```

`--grep` 解决"上次讨论某话题是哪条会话"；`--tree` 同时读会话文件与本地索引库，
子代理会挂在父会话下面。

## 在 TUI 里接管派发的会话

`codex exec` 产生的会话 source 是 `exec`，**TUI 的 `codex resume` 选择器默认只列交互
会话（cli/vscode），所以看不到它们**。两个办法：

```bash
codex resume --include-non-interactive        # 选择器里显示 exec 会话
codex resume --include-non-interactive --all  # 再关掉按当前目录的过滤
codex resume <session-id>                     # 或直接按 id 进入，无视过滤
```

## 使用法则（实测经验）

**默认后台派发，保留并行能力。**并行按**工作树**划分：不同工作树各派一个互不干扰；
同一工作树绝不跑两个。要比较同一上下文的两种走法，用 `--fork-from` 分线，比给两个新会话
各讲一遍背景便宜，且起点严格相同。实验性 fork 用完记得 `codex delete --force <id>`。

**任务主题换了就开新会话。**判据：如果新任务书需要先否认旧会话里的某个结论，就开新的。

**任务书决定产出质量：**

- 基线数字必须自己先核过；写不准就别写。
- 要求**变异验证**而不是"跑通测试"：把 X 破坏掉，确认有测试失败，报出是哪一个。
- 要求报告"你认为该改但被范围挡住的东西"——经常是最有价值的部分。
- 要求维护计划（todo list）——`status` 的计划进度就来自这里。
- 它推回来的时候先当信号看，不要默认派发方是对的。
- **只有它明确说完成才算完成。**"我将/正在做 X"不是完成；以 `result.json` 终态和
  最终回复为准，重要产出去工作树核实。

**提交默认归派发方。**让它只改文件不 commit，审完再提。兄弟工作树的 `.git` 在可写根之外，
真要让它提交就加 `--add-dir <主仓库>/.git`。

**需要网络才用 `danger-full-access`**，且任务书里把边界写死（不许 force push、不许动
主干等）。沙箱放开了，约束就只能靠任务书。**exec 模式没有 TUI 那种"等待批准"环节**
（approval 恒为 never）：越权动作直接失败并返回给模型，不会挂起等人——所以派发前选对
沙箱档位就是全部的权限决策。

**`-C` 给项目根，不要给家目录**（Windows 管理沙箱在家目录起不了子进程），也不要另建
临时目录（会话会脱离项目档案，`AGENTS.md`/`.codex` 项目规则也不生效）。工作区外的路径
用 `--add-dir`，不要抬高 `-C`。

## 会话谱系速记

`id` 是身份（resume 用它），`forked_from_id` 是谱系，`parent_thread_id` 只有子代理有。
fork 开新线程；子代理留在父线程里（所以按 `session_id` 看会像重复条目）。

## 已知限制

1. Codex 的 `--json` 事件结构是内部契约，CLI 升级后可能漂移；`status` 出现
   `unknown event types` 提示时说明格式变了，功能可能不全但不会崩。
2. `compact` 依赖 app-server（官方标 experimental），失败时的兜底是
   `--fork-from` + 让它自己总结迄今上下文。
3. 存活判定看 `state.json` 的 `silence_seconds` 与 `last_event_at`，不要只看进程在不在。

## 注意事项

- 这是外部 Codex 进程，不是 Claude 内置 Agent；派发消耗 Codex 额度。
- 不要让 Codex 递归启动更多 Codex，除非用户明确要求。
- 复杂 prompt 走脚本传参或 stdin，不要拼复杂 shell 命令。
