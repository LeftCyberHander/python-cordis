# cordis_py 功能规约与验证体系

## 0. 验收标准

本框架的"正确"是**功能意义上的正确**，与任何具体实现的写法无关：

> 插件的**加载、解析、卸载**三大核心能力，以及事件派发、日志、服务等其余能力，  
> 在任何操作序列下都必须表现出**可预期的行为**——不崩溃、不静默出错、  
> 不泄漏、不越权、资源精确归还。

判据按以下优先级裁决，冲突时以上位者为准：

1. **调用方可依赖的行为契约**（本文件 §1、§2）——与参考实现的写法无关；
2. 参考实现（`../src/*.ts`）的行为——用于补齐未言明的细节；
3. 参考实现自身的缺陷——**一律不作为正确性依据**（§4 记录了因此被修正的项）。

## 1. 核心能力规约

### 1.1 加载（Load）

| 编号  | 规约                                                                                    | 验证                                                                                        |
| --- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| L-1 | `ctx.plugin(fn, config)` 立即返回一个 fiber；fiber 经 `await fiber` 或 `fiber.await_()` 驱动到稳定态 | `test_spec.TestLoading` / `test_cordis.TestLifecycle`                                     |
| L-2 | 依赖未满足时 fiber 停在 PENDING，插件主体**不执行**；提供方就绪后自动激活                                        | `test_depends_on_inject_before_activation`                                                |
| L-3 | 插件主体抛错 → 状态 FAILED，`await fiber` **抛出同一个错误**（不得静默）                                    | `test_load_failure_is_observable`                                                         |
| L-4 | 配置经 `runtime.Config` 校验；校验失败抛 `ValidationError`                                       | `test_config_validation_failure_is_reported`                                              |
| L-5 | 同一作用域内重复提供同名服务必须报错，不得静默覆盖                                                             | `test_duplicate_provide_is_rejected`                                                      |
| L-6 | 类插件构造即注册服务；服务方法体内的 `self.ctx` 指向**调用方**                                               | `test_service_class_registers_on_construction`、`test_service_method_sees_calling_context` |
| L-7 | effect 主体在**注册时**执行；异步主体同样立即启动                                                        | `test_async_effect_body_runs_at_registration`                                             |
| L-8 | effect 主体返回非法形态必须报错                                                                   | `test_invalid_effect_shape_is_rejected`                                                   |
| L-9 | `internal/plugin` 观察者抛错时，已启动的插件仍须被完整拆卸                                                | `test_regressions.TestFireAndForgetCatchChains`                                           |

### 1.2 解析（Resolve）

| 编号  | 规约                                           | 验证                                                                                                    |
| --- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| R-1 | 已 provide 的服务从其作用域解析到实现值                     | `test_service_resolves_to_impl_value`                                                                 |
| R-2 | 插件读取未声明 `inject` 的服务**必须报错**——依赖关系显式化，漏声明要暴露 | `test_undeclared_dependency_is_refused`                                                               |
| R-3 | 隔离作用域（`isolate`）内同名服务互不干扰；同 label 合并同一作用域    | `test_isolation_scopes_are_independent`、`test_same_label_isolations_share_scope`                      |
| R-4 | 隔离**按服务名**生效：未为某名字建立隔离作用域的名字天然全局共享           | `test_isolation_does_not_cross_into_parent`、`test_unisolated_name_is_global_even_from_isolated_scope` |
| R-5 | `ctx.get(name)` 不要求 inject，但按**调用方**作用域解析    | `test_get_reads_without_inject_but_respects_scope`                                                    |
| R-6 | 计算型属性（`accessor`）每次读取重新求值，且随注册插件卸载而消失        | `test_accessor_recomputes_on_every_read`、`test_accessor_lives_with_its_plugin`                        |
| R-7 | `mixin` 把服务成员暴露到 ctx，并随注册插件卸载                | `test_mixin_exposes_service_members`                                                                  |
| R-8 | 任意上下文（含隔离作用域）都能 `ctx.effect(...)`            | `test_effect_from_isolated_scope_works`                                                               |
| R-9 | `in` 判定覆盖内置能力与已注册服务，未知名返回假                   | `test_contains_covers_builtins_and_services`                                                          |

### 1.3 卸载（Unload）

| 编号  | 规约                                          | 验证                                                                                    |
| --- | ------------------------------------------- | ------------------------------------------------------------------------------------- |
| U-1 | effect 的 disposer 按**注册逆序**执行               | `test_effects_release_in_reverse_order`、`test_nested_effects_release_innermost_first` |
| U-2 | 每个 disposer 在整个运行期间**恰好执行一次**（幂等）           | `test_each_disposer_runs_exactly_once`、INV-3                                          |
| U-3 | 监听器随注册它的插件消亡，卸载后不再被派发                       | `test_listener_dies_with_plugin`、INV-6                                                |
| U-4 | 卸载后服务从 store 注销；插件全部 fiber 卸载后 runtime 记录移除 | `test_service_unregistered_on_unload`、`test_registry_delete_disposes_every_fiber`     |
| U-5 | 提供方消失后，依赖方退回 PENDING（不得保持"看起来可用"）           | `test_dependents_return_to_pending`                                                   |
| U-6 | 卸载父插件级联卸载其子插件创建的 fiber                      | `test_unloading_plugin_cascades_to_children`                                          |
| U-7 | 已释放插件拒绝新 effect（`INACTIVE_EFFECT`）          | `test_disposed_plugin_refuses_new_effects`                                            |
| U-8 | `restart()` 先卸载再原地重载                        | `test_restart_reloads_plugin`                                                         |
| U-9 | 每个资源只归还自己登记的对象（disposer 不得删别人的）             | `test_each_exporter_disposer_releases_its_own_entry`                                  |

### 1.4 其余能力

| 编号   | 规约                                                                  | 验证                                                                                          |
| ---- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| E-1  | `bail` 的判据是「有没有返回值」而非「真不真」：除 `None`/`False` 外一律中止                   | `test_bail_stops_on_any_value_except_none_and_false`                                        |
| E-2  | 派发诊断（`internal/dispatch`）如实反映本次策略，`parallel` 报 `parallel`           | `test_parallel_reports_its_own_mode`                                                        |
| E-3  | `waterfall` 续体**恰好一次**推进链条：重复调用不跳监听器、不重复执行内置行为                      | `test_waterfall_continuation_advances_exactly_once`                                         |
| E-4  | 事件名是用户数据，不得与事件名映射自身的成员冲突                                            | `test_event_names_never_collide_with_map_members`                                           |
| E-5  | 服务变更事件只送达同一隔离作用域的监听器                                                | `test_internal_service_respects_isolation`                                                  |
| E-6  | `ctx.logger()` 的名称由**调用方 fiber** 派生；`logger.exporter` 归调用方 fiber 所有 | `test_default_name_derives_from_calling_plugin`、`test_exporter_disposed_with_owning_plugin` |
| E-7  | 带 cause 的错误链完整记录，不丢外层                                               | `test_error_chain_is_recorded_in_full`                                                      |
| E-8  | 多级 intercept 按 key 合并，祖先条目的其它 key 不丢                                | `test_intercept_config_merges_across_levels`                                                |
| E-9  | `await fiber.update(cfg)` 在任何状态下都不崩；已释放则明确拒绝                        | `test_update_is_awaitable_in_every_state`                                                   |
| E-10 | 根 fiber 的 effect 元数据真实反映实际注册                                        | `test_root_fiber_reports_its_own_effects`                                                   |

## 2. 全局不变量

以下命题在任何操作前缀之后都成立，由 §3 的 L2/L3/L4/L5 手段自动检查。
L5（无界验证器）另盯两条依赖传播方向的性质：INV-10（notify 级联完备：
每个依赖了被通知服务的 fiber 都必须被刷新）与 INV-11（依赖失效传播：提供方
已死时消费方不得仍停留在 epoch=ACTIVE）。

| 编号    | 不变量                                                                        | 规约来源            |
| ----- | -------------------------------------------------------------------------- | --------------- |
| INV-1 | fiber 状态机只沿合法迁移边（迁移表见 §2.1）                                                | L-2、L-3、U-5、U-8 |
| INV-2 | `uid is None ⟺ state == DISPOSED`；根 fiber 恒为 `uid=0, runtime=None, ACTIVE` | U-7             |
| INV-3 | 每个 disposer 至多执行一次，且执行的都是已登记项；派发序 = 注册序的逆序（INV-3a / INV-3b 两个子性质，后者由 `clear()` 插桩边属性判定） | U-1、U-2、U-9 |
| INV-4 | 注册表与 fiber 集合一致；空 fibers 的 runtime 不残留                                     | U-4             |
| INV-5 | 隔离作用域互不串扰；未隔离的名字全局共享                                                       | R-3、R-4         |
| INV-6 | 已释放 fiber 名下的监听器不得再存在于派发表                                                  | U-3             |
| INV-7 | 不存在从未 await 的协程（`RuntimeWarning` 升级为错误强制）                                  | §4 第 1 条        |
| INV-8 | 提供方存活时，声明 inject 的依赖方不得停在 PENDING                                          | L-2             |

### 2.1 fiber 状态机迁移表

| 迁移边                  | 触发条件                 |
| -------------------- | -------------------- |
| PENDING → LOADING    | 依赖满足，启动加载            |
| PENDING → UNLOADING  | 激活前被卸载               |
| PENDING → FAILED     | 激活即抛错                |
| LOADING → ACTIVE     | 加载完成且依赖未在途中变化        |
| LOADING → UNLOADING  | 加载途中依赖变化             |
| LOADING → FAILED     | 加载抛错                 |
| ACTIVE → UNLOADING   | 主动卸载 / 依赖变化          |
| FAILED → LOADING     | 失败后重试                |
| FAILED → UNLOADING   | 失败后被卸载               |
| UNLOADING → LOADING  | 卸载完成但依赖重新满足（restart） |
| UNLOADING → PENDING  | 卸载完成、仍缺依赖            |
| UNLOADING → DISPOSED | 卸载完成且 fiber 已释放      |
| UNLOADING → FAILED   | 卸载完成但错误仍在（失败插件的卸载）   |

（另保留 ACTIVE → FAILED、UNLOADING → ACTIVE 两条防御性边：当前操作字母表未观测到，  
对应"状态更新回调未显式给值时按当前事实推导"的兜底路径。）

## 3. 验证体系

```
L1   行为对照   tests/test_cordis.py        35 例   逐条对照参考实现（细节补齐用）
L1'  功能规约   tests/test_spec.py          54 例   §1 的每条规约一条用例
L1'  缺陷回归   tests/test_regressions.py   24 例   §4 每条缺陷一条锁定用例
L2   属性测试   tests/test_properties.py  120 条种子化随机序列 × 30 步，每步断言 INV-1~8
L3   模型检测   tests/test_properties.py    9 元操作字母表穷举长度 ≤ N 的全部序列
                                                    （抽象状态去重；实测见下表）
L4   形式化证明 tools/verify.py              归纳不变式：性质 + 闭包两个证明义务；
                                            N=2 时已闭包 → PROVED（18.8s）
L5   形式化证明 tools/verify_unbounded.py    无界归纳不变式：存在集投影 + 级联感知的
                                            幸存二分，不设插件数上限。字母表分三层：
                                              结构层 23 元（闭包义务）
                                              探针层 10 元（逐状态核对 + 自环断言）
                                              快档  13 元（回归每次改动都跑，depth 7 闭包）
L5+  元验证   tools/verify_unbounded.py    16 条域内变异全部被捕获（每条对应一个
                                            不同性质），域外盲区单独记账
X    横切检查   -W error::RuntimeWarning    未 await 协程零容忍（INV-7）
```

L3 深度分档（环境变量 `CORDIS_BMC_DEPTH`，默认 4）：

| 档位      | 深度 | 展开路径 | 抽象状态 | 检查点   | 耗时     |
| ------- | -- | ---- | ---- | ----- | ------ |
| 快速档（默认） | 4  | 341  | 341  | 2898  | ≈ 2 s  |
| 完整档     | 5  | 1139 | 1139 | 12033 | ≈ 10 s |

两档都**完整遍历各自深度内的全部操作序列**（去重后仍逐一展开回放），差异只在  
锚定的序列长度。字母表 = {加载提供方, 加载消费方, 加载带 effect 插件, 加载隔离  
作用域提供方, 卸载 #0, 卸载 #1, 更新 #0, 重启 #0, 派发事件}。隔离作用域操作  
（`load_isolated`）的加入使 INV-5 成为可达且被实际检查的性质。

全量 127 例：功能档 ≈ 6 min（`python -W error::RuntimeWarning -m unittest discover
-s tests`；含 `test_closure.py` 对 L4/L5 结论的回归锁定与 16 条变异试验 ≈ 81 s），
完整档再加 `CORDIS_BMC_DEPTH=5` 与 `CORDIS_FORMAL=full`（18 元字母表闭包
266 s 独占 / 576 s 并发负载下），合计 ≈ 15 min。

**L3 的结论边界（务必读）**：设 Σ 为影响框架状态的公开操作完备集，对任意长度 N 的
序列 σ = a₁…a_N，要从"长度 ≤ K 无反例"推出"长度 N 无反例"，需要以下三点同时成立：

1. **有穷性**：可观测状态（各 fiber 的状态/epoch/快照/effect 计数、注册表、
   派发表、清理账本）构成有穷集合，uid 等纯身份量不计入抽象状态；
2. **封闭性**：每个 a ∈ Σ 的结果仍在该集合内（展开过程本身即验证，无一步逸出）；
3. **归纳步骤**：不变量在每条单步迁移上保持。

其中第 3 点在本套验证里是**被断言的，不是被检查的**——它写在本文档里，靠读者
信任。故 L3 的严格结论只到"长度 ≤ K 的全部序列上 INV 成立"（实测 K = 4 或 5）。
"任意长度"的说法依赖第 3 点成立，而第 3 点本身未被机器验证。

**L4 正是为了消除这个缺口**：`tools/verify.py` 把第 3 步变成可判定的**闭包义务**
——`∀ s ∈ S. ∀ a ∈ Σ. post(s, a) ∈ S`——由算法逐条检查。

实测结果（基数约束 N=2，即"任意时刻存活插件 ≤ 2"）：可达集于深度 15 **闭包**
（569 个抽象状态、5691 个检查点、18.8s），裁决 **PROVED**。即：在这条限定范围内，
"任意长度"是被**机器证明**的，不再依赖本文档里那句人写的归纳论证。

无约束时状态空间本质无界（层间增长率 6–7×），裁决退回 INCONCLUSIVE——它诚实地
报告"性质成立但证明不完整"，而不是把"没查到反例"说成"正确"。算法、六个无界源的
逐一处理与强度边界见 [`FORMAL_VERIFICATION.md`](FORMAL_VERIFICATION.md)。

**L5：把"存活插件 ≤ N"这个上界本身消掉。** L4 的结论带显式参数 N，且在该路线
上 N 不可消去（不同 N 的闭包集互不包含，`S₂ ⊄ S₃`；N 每加 1 状态数涨 5–6 倍）。
`tools/verify_unbounded.py` 换投影达到**无界**结论：把抽象状态从"fiber 视图多重集"
换成"互异视图存在集"（fiber 计数彻底离开状态空间），操作按互异视图定向，每个
dispose 展开"视图消失 / 幸存"两个分支。实测 depth 7 闭包（43 个存在集状态、
560 个检查点、1.5s），裁决 **PROVED-UNBOUNDED**：

> **任意长度、任意时刻存活插件数（无上界）**的操作序列上，不变量全部成立。

三个计数依赖的识别与解法、一致性检验（60 个重复实例 / 780 个状态×操作组合，
机器检查"计数不影响转移"）、浸润测试、以及唯一未机器检查的数学步骤（计数 ≥3
与 =2 的幸存等价性，由代码结构事实覆盖），见
[`FORMAL_VERIFICATION.md`](FORMAL_VERIFICATION.md) §6.2。该结论由
`tests/test_closure.py::TestUnboundedProof` 回归锁定（闭包、二分非空转、
投影有界、域内变异 5/5 捕获）。

### 3.1 “证明无 bug”的精确进度账

"无 bug"的可执行定义 = §1 的 **37 条功能规约**（L-1~9 / R-1~9 / U-1~9 /
E-1~10）全部成立。第七轮把剩余 25 条全部纳入形式化验证，覆盖账如下——
这是本项目对"无 bug"诉求的兑现形式：**每条规约都标明它的证明手段与强度**。

| 规约 | 不变量（机器检查） | 强度 |
| --- | --- | --- |
| L-1 | INV-23 | 加载后停在稳定态 + `plugin()` 返回可 await 的 fiber 句柄 |
| L-2 | INV-1、INV-8 | 无界 |
| L-3 | INV-1 | 无界 |
| L-4 | INV-13 | 加载路径，配置校验失败必以 `ValidationError` 抛错 |
| L-5 | INV-12 | 同作用域重复 provide 必拒（以"已有提供方"为前提） |
| L-6 | INV-24 | 服务方法体里的 `self.ctx` 随调用方重绑 |
| L-7 | INV-25 | 异步 effect 主体在注册时执行 |
| L-8 | INV-14 | 非法 effect 形态必以 `TypeError` 抛错 |
| L-9 | INV-15 | 观察者抛错后已启动插件仍被完整拆卸 |
| R-1 | INV-26 | 已 provide 的服务解析到实现值 |
| R-2 | INV-27 | 未声明 inject 的读取必拒（无条件） |
| R-3、R-4 | INV-5 | 无界 |
| R-5 | INV-28 | `get` 按调用方作用域解析 |
| R-6 | INV-17、INV-18 | accessor 随插件消亡 + 每次读取重新求值 |
| R-7 | INV-17、INV-19 | mixin 成员随插件消亡 + 取到成员值 |
| R-8 | INV-20 | 隔离作用域上下文可注册 effect |
| R-9 | INV-21 | `in` 覆盖内置能力与已注册服务，未知名为假 |
| U-1 | INV-3b | disposer 逆序派发 |
| U-2、U-9 | INV-3a | disposer 至多一次且都是已登记项 |
| U-3 | INV-6 | 监听器随注册方 fiber 消亡 |
| U-4 | INV-4 | 注册表与 fiber 集合一致 |
| U-5 | INV-1 | 依赖变化触发合法迁移 |
| U-6 | INV-16 | 父卸载级联子 fiber（无孤儿） |
| U-7 | INV-2 | `uid is None ⟺ DISPOSED` |
| U-8 | INV-1 | restart 的合法迁移 |
| E-1 | —— | **仅测试锁定**：`is_bailed` 是纯函数，不经状态转移，性质集不观测（见 §6.4） |
| E-2 | INV-29 | `internal/dispatch` 如实反映本次策略 |
| E-3 | INV-30 | waterfall 续体恰好一次推进 |
| E-4 | INV-31 | 怪事件名不与事件名映射自身的成员冲突 |
| E-5 | INV-22 | 服务变更事件只送达"对该名字同一隔离作用域"的监听器 |
| E-6 | INV-32 | logger 名字由调用方 fiber 派生 + exporter 归调用方所有 |
| E-7 | INV-33 | 带 cause 的错误链完整记录（内层在前、外层在后） |
| E-8 | INV-34 | 多级 intercept 按 key 合并，祖先条目的其它 key 不丢 |
| E-9 | INV-35 | 任何状态下 `update` 可 await；已释放则明确拒绝 |
| E-10 | INV-36 | 根 fiber 的 effect 元数据真实反映实际注册 |

**合计：37 条中 36 条已形式化为不变量（36/37），仅 E-1（bail 判据）仍只有
测试锁定**——它是纯函数判定，不产生状态转移，无法纳入边属性体系；这是
**已知且显式记录**的性质集边界，不是遗漏。

#### 强度的分层（读这张表必须看这一段）

"已形式化"不等于"同一种强度"，本项目的结论分三层：

| 层 | 手段 | 强度表述 |
| --- | --- | --- |
| **结构层** | `UOPS_CORE`（23 元）存在集 BFS + 幸存二分闭包 | 13 元与 18 元字母表**已闭包** ⇒ 任意长度、任意插件数；23 元字母表算力预算内未闭包（depth 5 / 10 279 状态 / 545 s，无反例但**不等于闭包**） |
| **探针层** | `UOPS_PROBE`（10 元）在**每个**可达状态上逐一执行，核对性质 + 投影自环 | 覆盖全部可达状态；强度限于"该状态上性质成立" |
| **元验证** | 一致性检验（重复实例后继比对）+ 变异测试 | 验证器本身的可信度：域内变异全捕获 |

探针层不进 BFS 展开（它们在投影上是自环，进展开只会让检查点数按探针数翻倍
而不增状态数）；覆盖面由"每个可达状态都执行一遍"保证。探针的**自环断言**
是这套分层的安全阀：若某个探针实际改变了抽象状态，它就必须被挪进结构层
重跑闭包。

即便 36 条全部形式化，结论的精确形式依然是"**在框架自身操作的封闭模型
内、对已形式化的 36 条规约，任意长度、任意插件数成立**"——而不是哲学
意义上的"无 bug"。两条原理性边界与工作量无关：**（a）** 插件是用户写的
任意代码，"框架对任意用户插件都正确"不可判定（插件可以不归还资源、可以
死循环，那不是框架的 bug）；**（b）** 抽象投影的正确性（α 不混淆行为
不同的具体状态）是归纳证明的公设，由一致性检验与变异测试提供经验信心，
本身不是演绎证明。

**残留风险（显式处置）**：

- 异步交错空间未穷举。处置：每个场景用固定收敛策略把悬挂任务驱动到收敛后再断言，
  随机序列覆盖操作顺序维度；
- Σ 未覆盖的操作（accessor/mixin 的运行时增删、内部瀑布 veto）由 L1' 规约用例锁定；
- `asyncio.run` 分步驱动会取消未完成任务——跨步骤的长链条需在单一循环内断言
  （规约用例中涉及多 fiber 传播的场景均使用单循环）；
- L4/L5 的性质集存在已知盲区：`bail` 判据不被任何不变量观测到（见
  `FORMAL_VERIFICATION.md` §6.4）；
- L5 的计数 ≥3 幸存等价性由代码结构事实论证（更新函数不读 fiber 计数），
  是整条无界论证中唯一未机器检查的步骤（`FORMAL_VERIFICATION.md` §6.2）。

## 4. 缺陷台账

按"违反哪条规约"归类。标 **[规约级]** 的条目在参考实现中同样存在，但属于功能  
缺陷而非"照搬即可"，故按 §0 第 3 条予以修正。

| #           | 位置                    | 违反的规约           | 现象                                                                                          |
| ----------- | --------------------- | --------------- | ------------------------------------------------------------------------------------------- |
| 1 **[规约级]** | fiber.py              | L-9、L-7、INV-7   | 4 处 fire-and-forget catch 链从未被调度：插件拆卸不完整、错误不上报、协程从未 await；异步 effect 主体直到卸载才运行               |
| 2 **[规约级]** | events.py             | U-3、E-2、E-3、E-4 | `internal/update` 钩子错挂根 fiber（卸载后泄漏、误触发）；`parallel` 上报错误模式；waterfall 续体可多次调用会跳监听器；事件名可撞映射成员 |
| 3 **[规约级]** | logger.py             | U-9、E-6         | exporter 的 disposer 读取"当时的最新序号"，释放 A 会删掉 B，多 exporter 时永久泄漏                                 |
| 4 **[规约级]** | context.py            | E-10            | 构造期清空根 fiber 的 disposer，使 effect 元数据失真（内置注册项变成无人调用的孤儿）                                      |
| 5 **[规约级]** | events.py             | E-1             | `is_bailed` 特判 NaN，导致同一类"真实返回值"的判定自相矛盾                                                      |
| 6 **[规约级]** | context.py            | R-8             | `ctx.effect` 用 `object.__getattribute__` 取 fiber，绕过模拟原型链 → 隔离作用域上直接崩                        |
| 7 **[规约级]** | utils.py              | L-6             | 服务方法内的 `self.ctx` 恒为提供方上下文，方法里注册的服务/effect 错挂作用域                                            |
| 8           | reflect.py            | E-5             | `internal/service` 不带作用域过滤器，跨隔离域泄漏                                                          |
| 9           | context.py / fiber.py | E-8             | 多级 intercept 同服务名配置不按 key 合并，祖先条目丢失                                                         |
| 10          | logger.py             | L-6、E-6、E-7     | logger 从不重绑调用方（名称/级别/meta/exporter 归属全落根）；带 cause 的错误只记内层                                   |
| 11          | events.py             | E-4             | thisArg 识别遗漏 `None`/list/tuple/set，`emit([…], 'e')` 直接崩溃                                    |
| 12          | context.py            | R-5、R-6         | `Context.get/set` 退化为根作用域；`accessor`/`mixin` 恒注册在根 fiber                                    |
| 13          | reflect.py            | R-7             | `mixin` 读不了 dict 形态服务的成员                                                                    |
| 14          | fiber.py              | E-9、U-5         | `update()` 非 ACTIVE 分支返回 None（`await` 直接崩）；`await_()` 吞掉生命周期任务的失败                           |
| 15          | fiber.py              | U-2、U-9         | `_run_disposable` 用 `or` 回退吞掉 falsy 结果；`_await_with_catch` 不等待处理器返回的协程（异步拆卸被丢弃）             |
| 16          | utils.py / logger.py  | —               | `_bind` 为死代码（派发上下文无处送达）；`_LoggerCallable` 为死代码                                              |

## 5. 已知限制（非缺陷，明确记录）

- **`Logger.meta["fiber"]` 为强引用**（参考实现用弱引用）：仅用于诊断，不影响所有权。
- **无事件循环时 effect 的驱动方式**：`ctx.effect(异步主体)` 在纯同步路径下会阻塞  
  驱动到完成（参考实现在微任务队列上异步完成）；语义结果一致，时序不同。
- **服务方法的上下文切换窗口**：方法被重绑到调用方上下文，若方法内部 await，  
  存在极短窗口让并发调用观察到该绑定。服务方法通常只做同步调用与 effect 注册。
- **`is_special_property` 的数字判定**：判据为"全数字串"，比参考实现更宽  
  （`"01"` 在参考实现中不特殊）；两侧都是保守地绕过服务解析，不影响解析结果。
- **配置校验协议**：本移植版约定 `Config` 为可调用对象  
  （`(config) -> 校验后值 | {"value": ...} | {"issues": [...]}`），  
  不实现参考实现的 standard-schema 对象协议。
