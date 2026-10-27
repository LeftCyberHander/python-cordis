# 办公三件套：做表 / 记账 / 会议纪要

用 `cordis-py` 把三件办公琐事做成三个插件，挂到同一个进程上：

- **每位同事一个隔离作用域**：独立数据库、独立输出目录，同一份代码服务多人；
- **能力之间靠 `inject` 声明依赖**：账本自动等存储就绪，不需要写重试代码；
- **一切副作用都是 `effect`**：数据库连接、输出目录、事件监听，插件卸载时自动回收；
- **插件可热插拔**：运行期挂上/卸载一个扩展（演示中的"审计"），不需要重启。

## 快速开始

```bash
cd examples/office
python office.py demo
```

演示流程（约 1 秒跑完）：

| 步骤 | 内容 |
| --- | --- |
| 0 | 在根上下文挂一个审计插件（监听全进程的记账事件） |
| 1 | 做表：合并两份"收表"（按资产编号去重）→ `out/张伟/终端设备台账汇总-*.xlsx` |
| 2 | 记账：张伟、王芳各自登记 → 两份独立 SQLite 账本 + 月报 xlsx |
| 3 | 会议：会议通知 docx + 纪要 docx（自动抽取决议与待办）+ 待办清单 |
| 4 | 校验：两个账本文件互不相同；卸载审计插件后再记账，日志不再增长 |

产物在 `out/`，数据库在 `data/`（均为运行时目录，可随时删除；demo 每次运行会重置自己的演示工作区）。

## CLI 用法

```bash
# 记账
python office.py ledger add --item "差旅费-南京" --amount 320 --category 交通 --who 李强
python office.py ledger list  --month 2026-10 --who 李强
python office.py ledger report --month 2026-10 --who 李强

# 做表（samples/ 下有两份示例 CSV 可直接试）
python office.py table merge --inputs samples/设备台账-公文组.csv samples/设备台账-规划组.csv \
    --title "终端设备台账汇总" --key 资产编号 --who 李强
python office.py table make --csv samples/设备台账-公文组.csv --title "公文组设备清单"

# 会议
python office.py meeting notice --title "第16期双周报例会" --when "2026年10月13日（周二）14:00" \
    --place "三层会议室" --attendees "张伟,王芳,李强" --agenda "上周工作回顾;调研问卷回收;下阶段安排"
python office.py meeting minutes --file samples/例会记录.txt --title "第16期双周报例会" \
    --attendees "张伟,王芳,李强"
```

## 能力清单（五个服务）

| 服务 | 插件文件 | 做什么 | 产物 | 依赖 |
| --- | --- | --- | --- | --- |
| `tables` | `plugins/tables.py` | 合并多份收表（去重 + 来源统计）、生成规范表格 | xlsx | `docs` |
| `ledger` | `plugins/ledger.py` | 登记 / 查询 / 月度汇总 / 报表导出 | xlsx + SQLite | `store` `docs` |
| `meetings` | `plugins/meetings.py` | 会议通知（含议程）、纪要约整（决议/待办抽取）、待办跟踪 | docx + SQLite | `store` `docs` |
| `docs` | `plugins/docs.py` | 极简 xlsx / docx 写出（仅标准库，无 openpyxl 依赖） | — | — |
| `store` | `plugins/store.py` | SQLite 封装，连接随插件生命周期开关 | — | — |

## cordis 概念 → 办公场景

| cordis 概念 | 在本示例中的用法 |
| --- | --- |
| `Service` / `provide` | 每个能力是一个 `Service` 子类，构造即注册（`tables` / `ledger` / `meetings` / `docs` / `store`） |
| `inject` | `ledger` 声明 `["store", "docs"]`，依赖未就绪时 fiber 自动 PENDING，就绪后自动激活 |
| `isolate` | `make_workspace()` 为每位同事派生隔离作用域：账本、输出目录、待办完全独立 |
| `effect` | `store` 的数据库连接、`docs` 的输出目录登记为 effect —— 卸载即回收（演示中审计监听器同理） |
| `on` / `emit` | `ledger.add()` 发出 `ledger/entry`；审计插件监听写日志 —— 跨插件协作、业务代码零改动 |
| `waterfall` | `meeting/minutes/blocks` 钩子：纪要段落可被其它插件改写（模板定制扩展点） |
| `plugin` 装卸 | 演示中的审计插件"挂上 → 卸载"，全程不重启进程 |

## 给同事用：三种交付路径（由易到难）

1. **本地工具 / 共享文件夹（最快，今天可用）**
   把 `office.py` 和常用命令包成 `.bat`，同事双击即用；或整个目录放共享盘，谁用谁跑。
   适合 1～5 人的小范围试点 —— 不需要服务器、不需要审批。
2. **内网 Web 服务（一周内）**
   用 Flask/FastAPI 包一层：页面上传文件、提交表单，后端调用同一套服务返回结果文件。
   业务插件一行不用改，只是多了一个入口插件。
3. **IM 机器人（稳定之后）**
   企业微信 / 钉钉机器人回调 → 调用同一套服务。需要一台常开机器、应用登记与回调地址。

> 三种路径共用同一套插件。**换入口不改业务代码**，这就是把能力做成插件而不是脚本的意义。

## 加一个新能力（三步）

1. 在 `plugins/` 下新建模块，写一个 `Service` 子类（`provide = "xxx"`，按需 `inject`）；
2. 在 `office.py` 的 `SERVICE_NAMES` 里加上服务名，并在 `make_workspace()` 里加一行 `scope.plugin(Xxx)`；
3. （可选）在 `office.py` 或自己的入口里调用它。

## 注意事项（本移植版的既有约定）

- **`waterfall` 终端回调是零参的**（`events.py` 的既有约定）：要传递数据请用闭包，
  例如 `self.ctx.waterfall("name", payload, lambda: payload)`；
- **会被"多份实例化"的服务都要 `isolate`**：同一进程里挂两份 `ledger`，必须让 `ledger`
  这个名字先被隔离，否则第二次注册会撞键（`service has been registered`）；
- **事件（`emit`/`on`）是进程级的**：监听器默认能收到所有作用域的事件；需要限定范围时，
  在监听器内部自行过滤（例如判断 `entry["who"]`）；
- 本示例依赖的几处框架修补（`Service` 注册 scope、调用方重绑、dict 插件解析）已经
  合并进 `cordis_py/`，克隆下来直接 `python office.py demo` 即可；修补记录见仓库
  根目录 `README.md` 的「与参考实现的差异」。

## 目录结构

```
examples/office/
├── README.md            # 本文档
├── office.py            # 入口：装配 + CLI + demo
├── plugins/
│   ├── __init__.py
│   ├── docs.py          # 文档输出（xlsx / docx，仅标准库）
│   ├── store.py         # SQLite 存储
│   ├── tables.py        # 做表
│   ├── ledger.py        # 记账
│   └── meetings.py      # 会议组织与纪要
├── samples/             # CLI 可直接使用的示例输入
├── data/                # 运行时：每人的 SQLite 库（可删）
└── out/                 # 运行时：生成的文件（可删）
```
