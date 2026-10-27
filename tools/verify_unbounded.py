"""cordis_py 的**无界**形式化正确性验证：不设插件数上限的归纳不变式证明。

## 与 ``tools/verify.py`` 的关系

``verify.py`` 在基数约束 N（同时存活插件 ≤ N）下给出 PROVED。N 是**显式限定
条件**：超过 N 的序列被基数守卫移出转移系统，未被声称正确；且 N 不可消去——实测
不同 N 的闭包集互不包含（``S₂ ⊄ S₃``），每个 N 是独立的证明义务。把 N 从 2 放到
4，状态数涨 5–6 倍、耗时涨 7–9 倍，N=4 已需 19 分钟。

本模块把结论推到**无界**：不设插件数上限，证明"任意长度、**任意插件数**"。

## 字母表分三层（第七轮）

33 个操作一次全进 BFS 不可行（depth 5 已 10279 个存在集状态 / 521 s，深度每层
约 ×5）。故按"是否改变抽象状态"分层：

| 层 | 常量 | 操作数 | 验证方式 |
| --- | --- | --- | --- |
| 结构层 | ``UOPS_CORE`` | 23 | 存在集 BFS + 幸存二分**闭包**——唯一给出"任意长度"结论的一层 |
| 一级 | ``UOPS_TIER1`` | 18 | 发布前验证（``--alphabet tier1`` / ``CORDIS_FORMAL=full``） |
| 快档 | ``UOPS_L5`` | 13 | 回归套件每次改动都跑（depth 7 闭包 / 43 状态 / ~2 s） |
| 探针层 | ``UOPS_PROBE`` | 10 | 只读/自限操作，在投影上是**自环**；由 ``run_probes`` 在每个可达状态上逐一执行，核对性质 + **投影自环断言** |

自环断言是这套分层的安全阀：某探针若实际改变了抽象状态，它就属于结构层，必须
挪进 ``UOPS_CORE`` 重跑闭包——否则分层会静默漏掉它的转移。

## 为什么不能直接放大 N，也不能去掉守卫

无界性的根源不是"状态多"，而是三个**具体的计数依赖**——它们使"按抽象状态去重"
不再可靠：

1. **存活数决定注册表键集**：``_teardown`` 在 runtime 的最后一个 fiber 消失时才
   ``registry.delete``。同一投影的两个具体状态（某视图有 1 个 vs 2 个 fiber）
   在 dispose 后投影不同（键保留 vs 删除）。
2. **结构定向的选择依赖计数**：按"第 i 个 fiber"派发操作时，计数不同选中的
   fiber 视图不同。
3. **同视图多副本的幸存**：dispose 掉一个消费者时，若还有第二个同视图消费者，
   该视图在继态中仍然存在。

## 本模块的解法：存在集投影 + 视图定向 + 级联感知的幸存二分

把抽象状态从"视图**多重集**"换成"视图**存在集**"（去重），并把三个计数依赖
逐一显式化：

- **视图定向**：操作按"结构序第 i 个**互异视图**"定向，与计数无关（消灭 2）；
- **幸存二分**：dispose 的后继取**两个**——被处置视图消失（计数=1）与仍存在
  （计数≥2），两者都检查性质、都继续展开（覆盖 1 与 3 的计数=1 侧）；
  二分后继的全局态按"幸存者继承被处置者的 store 贡献"构造（同视图 ⇒ 贡献相同）；
  **级联感知**（U-6）：分支连被处置视图的后代一起消去/恢复——父卸载级联子 fiber，
  存在集层面必须连子树一起处理，否则级联语义在二分分支上丢失；
- **注册表键集**从全局态中**导出**（= 有存活 fiber 的插件名集合），不再作为独立
  维度（消灭 1）。

于此得到**无界归纳定理**：若 BFS 在存在集层面闭包、且不变式在全部可达存在集上
成立，则不变式在**任意 fiber 数、任意长度**的操作序列后成立。定理依赖五个前提
（H1–H5），其中四个由本模块**机器检查**（H1 视图完备性、H2/H3 计数无关性与逐视图
更新一致性、H4 幸存者惰性、H5 级联完备性）。

## 规约覆盖（第七轮）

``docs/CORRECTNESS.md`` §1 的 37 条功能规约里**36 条已形式化为不变量**
（INV-1~11 与 INV-12~36），仅 E-1（``bail`` 判据）仍只有测试锁定——它是纯函数
判定、不经状态转移，无法纳入边属性体系。逐条映射见 ``CORRECTNESS.md`` §3.1。

元验证：16 条域内变异全部被捕获（每条对应一个不同性质，其中六条是本项目历史缺陷
的重新注入）。变异检验必须**两层都跑**（短 BFS + 探针层），见 ``run_mutation``。

## 运行

::

    python tools/verify_unbounded.py --alphabet l5     # 快档：闭包 2.7s + 探针 + 变异
    python tools/verify_unbounded.py --alphabet tier1  # 一级结构层：闭包 266s
    python tools/verify_unbounded.py                   # 完整结构层：小时级
    python tools/verify_unbounded.py --no-mutate --no-soak --uniformity-sample 60
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify as V  # noqa: E402
from cordis_py import FiberState  # noqa: E402
from cordis_py import fiber as fiber_mod  # noqa: E402
from cordis_py import reflect as reflect_mod  # noqa: E402
from cordis_py.logger import Exporter  # noqa: E402
from cordis_py.reflect import Accessor, ReflectService, _chain_isolate  # noqa: E402
from cordis_py.service import Service  # noqa: E402
from cordis_py.utils import symbols  # noqa: E402

# ---------------------------------------------------------------------------
# §1 无界抽象投影：全局态（有限）+ fiber 视图存在集（有限视图、去重）
# ---------------------------------------------------------------------------
#
# 与 verify.py 的投影相比，本投影**刻意丢掉全部计数**：
#
# - ``fibers`` 从"视图元组（含重复）"变为"互异视图的存在集"—— fiber 数由此
#   不再进入状态空间；
# - ``pending`` / ``dispatch`` / ``edges`` / ``listener_hits`` 等账本摘要全部
#   移出状态，改为**边属性**在迁移发生处判定（INV-1/3a/3b 与 verify.py 同法）；
# - ``listeners`` 只保留"是否有死亡归属者"（INV-6 只需要这一位），计数丢弃；
# - store 增加**作用域种类**（root/iso）维度：本模型只有这两种作用域（加载
#   要么在根上、要么在一次性 isolate 作用域里），该维度使"重复 provide 是否
#   抛错"这类判定成为投影的函数。
#
# 视图则**加强**到完整结构身份：插件主体（shape）、状态、epoch、inject、
# hook、store 贡献（provides）、监听器归属（listens）、disposer 数（截断）。
# 两个 fiber 视图相同 ⇒ 同主体、同贡献、同生命周期位置——这正是无界证明里
# "同视图 fiber 可互换"的前提（H1/H3）。


@dataclass(frozen=True)
class UFiberView:
    """单个插件 fiber 的**有界**结构视图（全部字段参与相等性）。"""

    shape: str  # 创建它的加载操作：provider / consumer / effect / isolated / parent / child / accessor / mixin
    state: str  # FiberState 名
    alive: bool  # uid is not None
    epoch: str  # "" / ACTIVE / INACTIVE（uid 拼接已折叠）
    inject: Tuple[str, ...]  # 声明的依赖名
    hooks: Tuple[str, ...]  # 自有 _hooks 键（规范名）
    provides: Tuple[Tuple[str, str], ...]  # (服务名, 作用域种类)
    listens: Tuple[str, ...]  # 拥有的监听器所属事件（规范名）
    accessors: Tuple[str, ...]  # 本 fiber 注册的 accessor / mixin 成员名（R-6/R-7）
    n_disp: int  # min(len(_disposables), 4)
    depth: int  # 插件祖先数（0 = 顶层加载；≥1 = 嵌套在别的插件主体里，U-6）
    parent_key: Optional[Tuple[Any, ...]]  # 父 fiber 视图的 key（顶层为 None）
    key: Tuple[Any, ...]  # 规范序（由全部字段构成，互异视图 ⇒ key 互异）


@dataclass(frozen=True)
class UGlobal:
    """全局状态的有界摘要。"""

    # (服务名, 作用域种类, 提供方存活, 提供方状态)
    store: Tuple[Tuple[str, str, bool, str], ...]
    # 存在死亡归属监听器的事件规范名（INV-6 要求恒空）
    dead_listeners: Tuple[str, ...]
    # 是否见过提供方（INV-8 的使能位，与 verify.py 一致）
    provider_seen: bool
    # 已声明的上下文属性：(名, service|accessor)
    props: Tuple[Tuple[str, str], ...]
    # 根 fiber 注册的 accessor 名（内置 mixin 的 16 个，常量）——
    # INV-17 的基线：props 的 accessor 集必须等于它 ∪ 存活插件注册的集
    builtin_accessors: Tuple[str, ...]


@dataclass(frozen=True)
class UState:
    """无界抽象状态 = 全局态 + fiber 视图**存在集**（排序去重）。"""

    g: UGlobal
    views: Tuple[UFiberView, ...]

    def render(self) -> str:
        lines = [f"views={len(self.views)}"]
        for v in self.views:
            lines.append(
                f"  [{v.shape} {v.state} alive={int(v.alive)} epoch={v.epoch or '-'} "
                f"inject={list(v.inject)} hooks={list(v.hooks)} "
                f"provides={list(v.provides)} listens={list(v.listens)} "
                f"accessors={list(v.accessors)} disp={v.n_disp} depth={v.depth}]"
            )
        lines.append(f"store={list(self.g.store)}")
        lines.append(f"dead_listeners={list(self.g.dead_listeners)}")
        lines.append(f"props={list(self.g.props)}")
        if self.g.builtin_accessors:
            lines.append(f"builtin_accessors={len(self.g.builtin_accessors)} 个")
        return "\n".join(lines)


def _canon_epoch(epoch: Any) -> str:
    """epoch 折叠：只有"是否 INACTIVE"这一位有语义（见 verify.py 同款函数）。"""
    text = V._canonical_epoch(epoch)
    return text if text is not None else ""


def _scope_kind(fiber: Any, root: Any) -> str:
    """fiber 的作用域种类。

    本模型的加载只有两种落点：``root.plugin(...)``（根作用域）与
    ``root.isolate(...).plugin(...)``（一次性隔离作用域）。isolate label 的
    具体值（``iso-<序号>``）是无界身份量，但其**种类**有界——模型内的全部
    跨作用域判定（重复 provide、依赖解析、notify 过滤）都只依赖这一位。
    """
    return "root" if fiber.parent is root else "iso"


def _fiber_of_ctx(ctx: Any) -> Any:
    try:
        return ctx.fiber
    except AttributeError:  # pragma: no cover - 原型链总有 fiber
        return None


def _fiber_depth(fiber: Any, root: Any) -> int:
    """fiber 的**插件祖先数**（0 = 顶层加载，≥1 = 嵌套在别的插件主体里）。

    顶层加载（root.plugin / iso.plugin）的父是根上下文，其 fiber 即根 fiber，
    故插件祖先数为 0；嵌套加载（插件主体内 ``ctx.plugin``）的父是宿主插件
    fiber，祖先数 ≥1。U-6（父卸载级联子 fiber）的存在集建模依赖这一位：
    级联只在 depth ≥1 的视图上发生，且幸存二分必须连子视图一起消去/恢复。
    """
    root_fiber = root.fiber
    depth = 0
    current = fiber
    while depth < 4:
        parent = getattr(current, "parent", None)
        parent_fiber = _fiber_of_ctx(parent) if parent is not None else None
        if parent_fiber is None or parent_fiber is current or parent_fiber is root_fiber:
            break
        current = parent_fiber
        depth += 1
    return depth


def _parent_view(fiber: Any, world: Any) -> Optional[UFiberView]:
    """父插件 fiber 的视图（顶层加载返回 None）。"""
    root = world.root
    if _fiber_depth(fiber, root) == 0:
        return None
    parent_fiber = _fiber_of_ctx(getattr(fiber, "parent", None))
    if parent_fiber is None or parent_fiber is root.fiber:
        return None
    return _uview(parent_fiber, world)


def _uview(fiber: Any, world: Any) -> UFiberView:
    """计算单个 fiber 的视图（投影的基本单元）。"""
    root = world.root
    provides: Set[Tuple[str, str]] = set()
    for impl in root.reflect.store.values():
        if impl.fiber is fiber:
            provides.add((impl.name, _scope_kind(fiber, root)))
    listens: Set[str] = set()
    for name, hooks in root.events._hooks.items():
        for hook in hooks:
            if _fiber_of_ctx(hook.ctx) is fiber:
                listens.add(V._canonical_event(name))
    # accessor 归属：插桩台账按 fiber 身份匹配（不用 id——id 会复用）
    accessors: Set[str] = set()
    for name, owner in getattr(world, "accessor_regs", ()):
        if owner is fiber:
            accessors.add(name)
    runner = fiber._runner if isinstance(fiber._runner, dict) else {}
    shape = "root"
    if fiber.runtime is not None:
        shape = getattr(fiber.runtime.callback, "__cordis_shape__", "?")
    state = FiberState.name_of(fiber.state)
    alive = fiber.uid is not None
    inject = tuple(sorted((fiber.inject or {}).keys()))
    hooks_keys = tuple(sorted(V._canonical_event(k) for k in fiber._hooks))
    epoch = _canon_epoch(runner.get("epoch"))
    provides_t = tuple(sorted(provides))
    listens_t = tuple(sorted(listens))
    accessors_t = tuple(sorted(accessors))
    n_disp = min(len(fiber._disposables), 4)
    depth = _fiber_depth(fiber, root)
    parent_view = _parent_view(fiber, world)
    parent_key = parent_view.key if parent_view is not None else None
    key = (
        shape,
        state,
        alive,
        epoch,
        inject,
        hooks_keys,
        provides_t,
        listens_t,
        accessors_t,
        n_disp,
        depth,
        parent_key,
    )
    return UFiberView(
        shape=shape,
        state=state,
        alive=alive,
        epoch=epoch,
        inject=inject,
        hooks=hooks_keys,
        provides=provides_t,
        listens=listens_t,
        accessors=accessors_t,
        n_disp=n_disp,
        depth=depth,
        parent_key=parent_key,
        key=key,
    )


def uproject(world: Any) -> UState:
    """把 World（具体状态）投影为无界抽象状态。"""
    root = world.root
    store: Set[Tuple[str, str, bool, str]] = set()
    for impl in root.reflect.store.values():
        store.add(
            (
                impl.name,
                _scope_kind(impl.fiber, root),
                impl.fiber.uid is not None,
                FiberState.name_of(impl.fiber.state),
            )
        )
    dead: Set[str] = set()
    for name, hooks in root.events._hooks.items():
        canon = V._canonical_event(name)
        for hook in hooks:
            owner = _fiber_of_ctx(hook.ctx)
            if owner is not None and owner.uid is None:
                dead.add(canon)
    props: Set[Tuple[str, str]] = set()
    for name, kind in root.reflect.props.items():
        props.add((name, kind if isinstance(kind, str) else "accessor"))
    # 根 fiber 注册的 accessor（内置 mixin）——INV-17 的基线。根 fiber 不在
    # 视图集中（registry 无它的 runtime），其注册只在构造期发生一次，是常量。
    builtin: Set[str] = set()
    for name, owner in getattr(world, "accessor_regs", ()):
        if owner is root.fiber:
            builtin.add(name)
    # 存在集：同视图只留一份（按 key 去重后按 key 排序，保证规范形）
    distinct: Dict[Tuple[Any, ...], UFiberView] = {}
    for fiber in world.fibers():
        view = _uview(fiber, world)
        distinct.setdefault(view.key, view)
    return UState(
        g=UGlobal(
            store=tuple(sorted(store)),
            dead_listeners=tuple(sorted(dead)),
            provider_seen=world.provider_seen,
            props=tuple(sorted(props)),
            builtin_accessors=tuple(sorted(builtin)),
        ),
        views=tuple(distinct[k] for k in sorted(distinct)),
    )


# ---------------------------------------------------------------------------
# §2 性质集：全局/存在集层的状态性质 + 迁移处的边性质
# ---------------------------------------------------------------------------
#
# INV-1（合法迁移边）、INV-3a（disposer 至多一次）、INV-3b（逆序派发）在
# verify.py 中已改为边属性，本模块沿用同一机制（插桩在迁移发生处判定，结果
# 记入 ``World.edge_violations``）。INV-10（级联完备性）是本模块新增的边
# 性质——它是无界定理前提 H5 的机器检查形式。


def _p_inv2(state: UState) -> Optional[str]:
    """INV-2：uid 与 state 一致（每个视图）。"""
    for v in state.views:
        if v.alive and v.state == "DISPOSED":
            return "state=DISPOSED 但 fiber 仍存活"
        if not v.alive and v.state != "DISPOSED":
            return f"uid=None 但 state={v.state}"
    return None


def _p_inv4(state: UState) -> Optional[str]:
    """INV-4：store 条目声称提供方存活，但没有任何存活的插件 fiber。"""
    if any(v.alive for v in state.views):
        return None
    for name, _scope, alive, _st in state.g.store:
        if name == "logger":
            continue  # 内置 logger 由根 fiber 常驻提供（根不在视图集中）
        if alive:
            return f"store 中 {name!r} 的提供方标记为存活，但无任何存活插件 fiber"
    return None


def _p_inv5(state: UState) -> Optional[str]:
    """INV-5：store 不残留提供方已释放的条目（作用域不串扰）。"""
    for name, scope, alive, _st in state.g.store:
        if not alive:
            return f"store 残留提供方已释放的服务条目 {name!r}@{scope}"
    return None


def _p_inv6(state: UState) -> Optional[str]:
    """INV-6：监听器随注册方 fiber 消亡（存在集层只判定"有/无"）。"""
    if state.g.dead_listeners:
        return f"事件 {list(state.g.dead_listeners)} 仍有属于已释放 fiber 的监听器"
    return None


def _root_svc_alive(state: UState) -> bool:
    """根作用域的 svc 是否有存活提供方（消费方都在根作用域）。"""
    return any(
        name == "svc" and scope == "root" and alive
        for name, scope, alive, _st in state.g.store
    )


def _p_inv8(state: UState) -> Optional[str]:
    """INV-8：依赖唤醒——提供方存活时消费方不得停留 PENDING。"""
    if not state.g.provider_seen or not _root_svc_alive(state):
        return None
    for v in state.views:
        if v.alive and "svc" in v.inject and v.state == "PENDING":
            return "消费方 inject=svc 但停留 PENDING（提供方存活）"
    return None


def _p_inv11(state: UState) -> Optional[str]:
    """INV-11：依赖失效传播——提供方已死时消费方不得仍认为依赖满足。

    这是 INV-8 的逆方向：``_check_impl`` 在提供方消失时必须清除消费方缓存，
    ``_refresh`` 才会把 epoch 打成 INACTIVE。若缓存不清，消费方会带着已失效
    的依赖继续以 ACTIVE 运行——它持有的服务值早已被归还。
    """
    if _root_svc_alive(state):
        return None
    for v in state.views:
        if v.alive and "svc" in v.inject and v.epoch == "ACTIVE":
            return "消费方 inject=svc 且 epoch=ACTIVE，但提供方已不存在（依赖失效未传播）"
    return None


def _p_inv17(state: UState) -> Optional[str]:
    """INV-17（R-6/R-7 生命周期）：props 中的 accessor 必须恰好由存活 fiber 持有。

    accessor（含 mixin 暴露的成员）以 effect 形式注册在**调用方** fiber 上，
    其 disposer 在 fiber 卸载时把它从 ``props`` 摘除。故：

        props 的 accessor 名集合 == 根 fiber 的内置 mixin ∪ 存活插件视图注册的 accessor 名

    左边多出名字 ⇒ 注册方已卸载但属性仍可解析（泄漏，且读到的是失效服务）；
    右边多出名字 ⇒ 属性被提前摘除（活着却读不到）。
    """
    prop_accessors = {
        name for (name, kind) in state.g.props if kind == "accessor"
    }
    alive_accessors = set(state.g.builtin_accessors)
    for v in state.views:
        alive_accessors.update(v.accessors)
    if prop_accessors != alive_accessors:
        leaked = sorted(prop_accessors - alive_accessors)
        missing = sorted(alive_accessors - prop_accessors)
        return (
            f"props 的 accessor 与存活归属不一致：残留={leaked} 缺失={missing}"
        )
    return None


#: 状态性质表：编号 → (说明, 判定函数)
PROPERTIES: Dict[str, Tuple[str, Callable[[UState], Optional[str]]]] = {
    "INV-2": ("uid 与 state 一致", _p_inv2),
    "INV-4": ("注册表与 fiber 集合一致", _p_inv4),
    "INV-5": ("隔离作用域不串扰（store 无孤儿）", _p_inv5),
    "INV-6": ("监听器随注册方 fiber 消亡", _p_inv6),
    "INV-8": ("依赖唤醒：提供方存活则消费方不 PENDING", _p_inv8),
    "INV-11": ("依赖失效传播：提供方已死则消费方不 ACTIVE", _p_inv11),
    "INV-17": ("accessor 生命周期：props 与存活归属一致（R-6/R-7）", _p_inv17),
}


def check_properties(state: UState) -> List[Tuple[str, str]]:
    violations = []
    for name, (_, predicate) in PROPERTIES.items():
        reason = predicate(state)
        if reason is not None:
            violations.append((name, reason))
    return violations


# ---------------------------------------------------------------------------
# §3 转移系统：视图定向的操作字母表（与 fiber 计数无关）
# ---------------------------------------------------------------------------


class UWorld(V.World):
    """在 verify.World 之上增加：最近加载 fiber 记录 + 视图定向操作 + 观测台账。

    加载逻辑（插件主体、账本插桩）全部继承自 ``V.World``；本类只加两样
    无界证明需要的东西。

    ``obs`` 是**观测台账**：每个操作把可观测结果（``(操作, 观测名, 值)``）
    追加进来，由 §4 的边性质在迁移发生处判定。台账**不进入**抽象状态——
    它是边属性的输入，不是状态的一部分（每条重放从空账本开始）。
    """

    def __init__(self) -> None:
        global _ACCESSOR_REGS, _NOTIFY_EMITS
        # 构造期（内置 mixin 的 16 个 accessor、logger 的 provide）必须进本
        # world 的台账：先清零模块级接收表，再构造，然后收养为 world 的属性。
        _ACCESSOR_REGS = []
        _NOTIFY_EMITS = []
        super().__init__()
        self.accessor_regs: List[Tuple[str, Any]] = _ACCESSOR_REGS
        # 事件日志：("emit", 服务名, 作用域label) / ("alive", 观察者号) /
        # ("dead", 观察者号)——E-5 的期望值必须在**发射时刻**按当时的存活集
        # 计算，故存活标记与 emit 记录必须落在同一条有序日志里。
        self.event_log: List[Tuple[Any, ...]] = _NOTIFY_EMITS
        # 最近一次加载产生的 fiber（一致性检验据此确定"多出来的那一份"）
        self.last_load_fiber: Any = None
        # 观测台账：(操作标签, 观测名, 值)——边性质的输入，不进抽象状态
        self.obs: List[Tuple[str, str, Any]] = []
        # internal/service 派发到隔离作用域监听器的次数（每个隔离加载一份）
        self.iso_hits: List[Dict[str, int]] = []
        # 与 iso_hits 平行的 isolate label（E-5 的配对判定用）
        self.iso_labels: List[str] = []
        # 与 iso_hits 平行的监听方上下文（E-5 的按名期望值判定用）
        self.iso_ctxs: List[Any] = []
        # 根作用域 internal/service 监听器的命中（E-5 正方向）
        self.root_hits: Dict[str, int] = {}
        # accessor 求值次数（R-6"每次读取重新求值"的观测）
        self.accessor_evals: Dict[str, int] = {}
        # throw_observer 的受害者插件（settle 后判定拆卸完整性用）
        self._observer_victim: Any = None
        # 三级观测台账
        self.seen: Dict[str, Any] = {}          # load_misc 的具名观测
        self.async_log: List[str] = []          # L-7：异步 effect 主体执行轨迹
        self.dispatch_log: List[Tuple[str, str]] = []  # E-2：派发诊断 (模式, 事件名)
        self.weird_hits: Dict[str, int] = {}    # E-4：怪事件名的监听器命中
        self.last_iso_ctx: Any = None           # R-5：作用域读取的隔离方
        self.last_disposed_fiber: Any = None    # E-9：已释放 fiber 的 update 拒绝
        # E-5 的正方向观测：根作用域监听器必须收到根作用域的服务事件。
        # 挂在根 fiber 上（永不释放），对其它性质无影响（INV-6 只查死亡
        # 归属者；投影不含根 fiber）。
        self.root.on("internal/service", self._on_internal_service)
        # E-2：派发诊断必须如实反映本次策略。
        self.root.on("internal/dispatch", self._on_internal_dispatch)
        # E-4：这些名字若命中事件名映射自身的成员，监听器会挂错键、派发时
        # 取到方法而非监听器列表。注册它们并统计命中即得观测点。
        for weird in _WEIRD_EVENT_NAMES:
            self.root.on(weird, self._make_weird_listener(weird))

    def _on_internal_service(self, name: Any, value: Any = None) -> None:
        if isinstance(name, str):
            self.root_hits[name] = self.root_hits.get(name, 0) + 1

    def _on_internal_dispatch(
        self, mode: Any, name: Any, args: Any = None, this_arg: Any = None
    ) -> None:
        if isinstance(mode, str) and isinstance(name, str):
            self.dispatch_log.append((mode, name))

    def _make_weird_listener(self, name: str) -> Any:
        def listener(*args: Any) -> None:
            self.weird_hits[name] = self.weird_hits.get(name, 0) + 1

        return listener

    def _on_status(self, fiber: Any, old_state: int) -> None:
        """INV-1 边属性：每条状态迁移在发生处就地判定合法性。

        verify.py 把迁移边尾部带进抽象状态；无界投影丢掉了这本账（它随序列
        长度增长），故改为在 ``internal/status`` 监听处逐条判定——非法边在
        产生的那一刻即被记录，与状态无关。
        """
        super()._on_status(fiber, old_state)
        edge = (FiberState.name_of(old_state), FiberState.name_of(fiber.state))
        if edge[0] != edge[1] and edge not in V.LEGAL_EDGES:
            self.edge_violations.append(f"INV-1 非法状态迁移 {edge[0]} -> {edge[1]}")

    # -- 视图定向 -----------------------------------------------------------
    def _views_in_order(self) -> List[UFiberView]:
        """按规范序返回**互异**视图（操作定向用，与计数无关）。"""
        distinct: Dict[Tuple[Any, ...], UFiberView] = {}
        for fiber in self._ordered_fibers():
            view = _uview(fiber, self)
            distinct.setdefault(view.key, view)
        return [distinct[k] for k in sorted(distinct)]

    def _fiber_with_view(self, view: UFiberView) -> Any:
        for fiber in self._ordered_fibers():
            if _uview(fiber, self).key == view.key:
                return fiber
        return None

    def dispose_view(self, index: int) -> Any:
        """释放结构序第 ``index`` 个**互异视图**的一个 fiber（有则释放）。"""
        views = self._views_in_order()
        if index >= len(views):
            return None
        fiber = self._fiber_with_view(views[index])
        if fiber is None:
            return None
        self.last_disposed_fiber = fiber  # E-9：已释放 fiber 的 update 拒绝
        return fiber.dispose()

    def dispose_view_last(self) -> Any:
        views = self._views_in_order()
        if not views:
            return None
        return self.dispose_view(len(views) - 1)

    def update_view(self, index: int) -> Any:
        views = self._views_in_order()
        if index >= len(views):
            return None
        fiber = self._fiber_with_view(views[index])
        if fiber is None:
            return None
        result = fiber.update({"n": self._label_seq})
        # E-9 前半：任何状态下 update 都必须返回 awaitable，否则调用方的
        # `await fiber.update(cfg)` 会在某个状态直接崩。
        self._note("update_view", "awaitable", inspect.isawaitable(result))
        return result

    def restart_view(self, index: int) -> Any:
        views = self._views_in_order()
        if index >= len(views):
            return None
        fiber = self._fiber_with_view(views[index])
        return fiber.restart() if fiber is not None else None

    # -- 规约定向的新操作（第七轮：25 条剩余规约的形式化）--------------------
    def _note(self, op: str, name: str, value: Any) -> None:
        self.obs.append((op, name, value))

    def dup_provide(self) -> None:
        """L-5：同一作用域重复 provide 同名服务必须报错（不得静默覆盖）。

        观测分两段：``had_root_svc`` 在操作处记录（根作用域 store 中是否
        已有 svc 条目——store 键在根作用域即服务名本身，隔离作用域是
        label，故 ``"svc" in store`` 精确等价于"根作用域已有提供方"）；
        settle 之后由 ``_check_dup_provide`` 读 ``last_load_fiber``：有提供方
        ⇒ 本次加载必须以 RuntimeError 失败；否则提供成功（fiber ACTIVE）。
        BFS 覆盖全部前提成立的可达状态。
        """
        self._note("dup_provide", "had_root_svc", "svc" in self.root.reflect.store)

        def plugin(ctx, config):
            ctx.provide("svc", "v")

        plugin.__cordis_shape__ = "dup"
        handle = self.root.plugin(plugin)
        # L-1：plugin() 必须**立即**返回一个 fiber 句柄（可 await、可
        # 读状态），而不是等加载完成后才给。
        self._note(
            "dup_provide",
            "handle",
            (
                FiberState.name_of(handle.state),
                callable(getattr(handle, "await_", None)),
                callable(getattr(handle, "dispose", None)),
            ),
        )

    def bad_config(self) -> None:
        """L-4：配置经 ``runtime.Config`` 校验，失败必须抛 ValidationError。"""
        def plugin(ctx, config):
            ctx.effect(lambda: None, "bad-config")

        def Config(cfg):
            return {"issues": [{"message": "invalid"}]}

        plugin.Config = Config
        plugin.__cordis_shape__ = "badcfg"
        self.root.plugin(plugin)

    def bad_effect(self) -> None:
        """L-8：effect 主体返回非法形态必须报错（TypeError）。"""
        def plugin(ctx, config):
            ctx.effect(lambda: 42, "bad-effect")  # 42 不是合法 disposer 形态

        plugin.__cordis_shape__ = "badeff"
        self.root.plugin(plugin)

    def load_nested(self) -> None:
        """U-6：在插件主体里再加载插件（父卸载必须级联卸载子 fiber）。

        子 fiber 的 disposer 以 effect 形式挂在**父** fiber 上
        （``fiber.py:394`` 的 ``parent.fiber.effect(_make_disposer())``），
        故父卸载时子必须一起消失。观测由 ``_check_cascade`` 在每个 dispose
        之后执行：任何存活 fiber 的插件祖先链上出现已释放 fiber 即违规。
        """
        def child(ctx, config):
            self._new_batch()
            disposer = self._track("child", ctx)
            ctx.effect(lambda: disposer, self.disposer_expected[-1])

        child.__cordis_shape__ = "child"

        def parent(ctx, config):
            self._new_batch()
            disposer = self._track("parent", ctx)
            ctx.effect(lambda: disposer, self.disposer_expected[-1])
            ctx.plugin(child)

        parent.__cordis_shape__ = "parent"
        self.root.plugin(parent)

    def throw_observer(self) -> None:
        """L-9：``internal/plugin`` 观察者抛错时，已启动的插件仍须被完整拆卸。

        历史缺陷 #1（fire-and-forget catch 链从未调度）的模型化：观察者同步
        抛错后，``_drive_catch`` 必须真正驱动拆卸协程——runtime 记录被移除、
        无残留 fiber。修补前拆卸完全不跑，fiber 停留在半释放状态。

        观测分两段：``raised`` 在操作处记录（同步抛出）；``registry_clean`` /
        ``fiber_leak`` 由 ``_check_throw_observer`` 在 settle 之后读世界状态
        （拆卸协程可能在事件循环上异步完成，不能假设操作返回时已清完）。
        """
        def boom(fiber: Any) -> None:
            raise RuntimeError("observer boom")

        self.root.on("internal/plugin", boom)

        def victim(ctx, config):
            ctx.effect(lambda: None, "victim")

        victim.__cordis_shape__ = "victim"
        self._observer_victim = victim
        try:
            self.root.plugin(victim)
            self._note("throw_observer", "raised", False)
        except Exception as error:  # noqa: BLE001 - 观测点：必须抛出
            self._note("throw_observer", "raised", type(error).__name__)

    # -- 二级：accessor / mixin / 隔离对偶 / contains -------------------------
    def load_iso_pair(self) -> None:
        """E-5 / R-8：在同一隔离作用域里加载"提供方 + 观察者"一对。

        E-5 的观测器必须与发射方**解耦**：若观察者与提供方同插件，它在自己
        插件的拆卸窗口内就被摘除（逆序清 disposer 时监听器先于 provide 的
        disposer 摘掉），于是收不到拆卸期的事件——那是 U-3/INV-6 已覆盖的
        正确行为，不属于 E-5 的期望值。故观察者是**独立插件**：提供方被
        卸载时它仍活着，"收到本作用域内的全部事件"才是良定义的期望。

        同一 op 内两个插件共享同一 isolate label（R-3：同 label 即同作用域）：
        提供方在隔离作用域 provide svc，观察者监听 ``internal/service``。
        """
        label = f"iso-{self._label_seq}"
        iso = self.root.isolate("svc", label=label)
        self._label_seq += 1
        # R-5 的读取目标必须在**操作处**就登记：插件主体要到 settle 才执行，
        # 那时再登记 ctx 就晚了（下一条操作读到 None 会被误判成泄漏）。
        self.last_iso_ctx = iso
        hits: Dict[str, int] = {}
        self.iso_hits.append(hits)
        self.iso_labels.append(label)

        def provider(ctx, config):
            self._new_batch()
            self.last_iso_ctx = ctx
            ctx.provide("svc", "ISO")
            # R-8：隔离作用域上下文上注册 effect 必须成功（历史缺陷：隔离
            # 作用域上 ctx.effect 直接崩）。
            try:
                ctx.effect(lambda: None, "iso-effect")
                self._note("load_iso_pair", "iso_effect", "ok")
            except Exception as error:  # noqa: BLE001 - 观测点
                self._note("load_iso_pair", "iso_effect", type(error).__name__)

        def observer(ctx, config):
            self._new_batch()
            index = len(self.iso_ctxs)
            self.iso_ctxs.append(ctx)

            def _lifecycle():
                # 存活标记在注册时立即落下；disposer（死亡标记）因逆序释放而
                # **后于**监听器执行，故顺序上"监听器已消失"先于"它死了"。
                self.event_log.append(("alive", index))
                return lambda: self.event_log.append(("dead", index))

            ctx.effect(_lifecycle, "obs-lifecycle")

            def track(name, value):
                hits[name] = hits.get(name, 0) + 1

            ctx.on("internal/service", track)

        observer.__cordis_shape__ = "iso_observer"
        # 观察者必须**先**加载：两个 fiber 的主体在 settle 里按注册序执行，
        # 若提供方先执行，它的 provide 事件会在监听器注册之前就发完。
        iso.plugin(observer)

        provider.__cordis_shape__ = "isolated"
        iso.plugin(provider)

    def load_accessor(self) -> None:
        """R-6：注册一个计算型属性，其 get 钩子每次求值并自增计数。"""
        def plugin(ctx, config):
            self._new_batch()

            def get(this: Any, error: Any) -> Any:
                self.accessor_evals["acc"] = self.accessor_evals.get("acc", 0) + 1
                return self.accessor_evals["acc"]

            ctx.accessor("acc", Accessor(get=get))

        plugin.__cordis_shape__ = "accessor"
        self.root.plugin(plugin)

    def read_accessor(self) -> None:
        """R-6：读取计算型属性——每次读取都必须重新求值。"""
        before = self.accessor_evals.get("acc", 0)
        try:
            value = getattr(self.root, "acc")
            self._note("read_accessor", "value", value)
        except Exception as error:  # noqa: BLE001 - 观测点
            self._note("read_accessor", "value", f"raise:{type(error).__name__}")
        self._note("read_accessor", "evals_before", before)
        self._note("read_accessor", "evals_after", self.accessor_evals.get("acc", 0))
        self._note("read_accessor", "acc_in_props", "acc" in self.root.reflect.props)

    def load_mixin(self) -> None:
        """R-7：provide 一个 dict 服务并把它的成员 mixin 到 ctx 上。"""
        def plugin(ctx, config):
            self._new_batch()
            ctx.provide("tools", {"ping": "pong"})
            ctx.mixin("tools", ["ping"])

        plugin.__cordis_shape__ = "mixin"
        self.root.plugin(plugin)

    def read_mixin(self) -> None:
        """R-7：读取 mixin 暴露的成员。"""
        try:
            self._note("read_mixin", "value", getattr(self.root, "ping"))
        except Exception as error:  # noqa: BLE001 - 观测点
            self._note("read_mixin", "value", f"raise:{type(error).__name__}")
        self._note("read_mixin", "ping_in_props", "ping" in self.root.reflect.props)
        self._note("read_mixin", "tools_alive", "tools" in self.root.reflect.store)

    # -- 三级：具体值语义（L-1/L-6/L-7/R-1/R-2/R-5/E-2/E-3/E-4/E-6/E-7/E-8/E-9/E-10）
    def load_misc(self) -> None:
        """一次加载五个探针插件，覆盖五条"值语义"规约。

        | 子插件 | 规约 | 观测 |
        | --- | --- | --- |
        | 服务类 + 调用方 | L-6 | 服务方法体里的 ``self.ctx`` 必须是**调用方** |
        | 异步 effect | L-7 | 主体在**注册时**执行（历史缺陷：直到卸载才跑） |
        | logger 探针 | E-6 | ``ctx.logger()`` 的名字由调用方 fiber 派生 |
        | 错误链 | E-7 | 带 cause 的错误链完整记录（内层与外层都在） |
        | intercept 两级 | E-8 | 按 key 合并，祖先条目的其它 key 不丢 |

        合并成一个操作是**有意的代价**：五个形态之间的交错未被覆盖，理由是
        这五条规约彼此独立，而合并把状态空间从"五路交错"降到"一路"。每条
        规约自身的判定强度不受影响（观测点仍在 settle 之后逐条核对）。
        """
        seen = self.seen

        # (a) L-6：服务类的 self.ctx 随**调用方**重绑
        def svc_provider(ctx, config):
            self._new_batch()

            class Api(Service):
                provide = "api"

                def whoami(self):
                    caller = seen.get("caller_ctx")
                    seen["api_ctx_is_caller"] = self.ctx is caller
                    seen["api_ctx_is_provider"] = self.ctx is ctx
                    return None

            Api(ctx)

        def svc_caller(ctx, config):
            self._new_batch()
            seen["caller_ctx"] = ctx
            ctx.api.whoami()

        svc_caller.inject = ["api"]

        # (b) L-7：异步 effect 主体在注册时立即启动
        def async_plugin(ctx, config):
            self._new_batch()

            async def body():
                self.async_log.append("start")
                return lambda: self.async_log.append("disposed")

            ctx.effect(body, "async-effect")

        # (c) E-6：logger 名字由调用方 fiber 派生 + exporter 归调用方所有
        def log_plugin(ctx, config):
            self._new_batch()
            seen["logger_name"] = ctx.logger().name
            # exporter 注册在**服务**上（ctx.logger 是 LoggerService 的追踪
            # 包装）；它是 ctx_aware 的，故注册时归**调用方** fiber 所有。
            ctx.logger.exporter(Exporter(export=lambda message: None))

        # (d) E-7：带 cause 的错误链完整记录
        def chain_plugin(ctx, config):
            self._new_batch()
            try:
                try:
                    raise ValueError("inner")
                except ValueError as inner:
                    raise RuntimeError("outer") from inner
            except RuntimeError as error:
                ctx.logger.error(error)

        # (e) E-8：多级 intercept 按 key 合并
        level1 = self.root.intercept("api", {"a": 1, "b": 2})
        level2 = level1.intercept("api", {"b": 20, "c": 3})

        def intercept_plugin(ctx, config):
            self._new_batch()
            merged = ctx.api.resolve_config()
            seen["intercept_merged"] = tuple(sorted((str(k), str(v)) for k, v in merged.items()))

        intercept_plugin.inject = ["api"]

        for plugin, name in (
            (svc_provider, "svc-provider"),
            (async_plugin, "async-probe"),
            (log_plugin, "log-probe"),
            (chain_plugin, "chain-probe"),
            (svc_caller, "svc-caller"),
            (intercept_plugin, "intercept-probe"),
        ):
            plugin.__cordis_shape__ = name
            plugin.name = name
        # 服务提供方必须最先加载（调用方依赖它）；intercept 插件挂在两级
        # intercept 上下文之下，其余挂在根上。
        self.root.plugin(svc_provider)
        self.root.plugin(async_plugin)
        self.root.plugin(log_plugin)
        self.root.plugin(chain_plugin)
        self.root.plugin(svc_caller)
        level2.plugin(intercept_plugin)

    def read_service(self) -> None:
        """R-1：已 provide 的服务从其作用域解析到实现值。"""
        root = self.root
        for name in ("svc", "tools"):
            try:
                value = getattr(root, name)
            except Exception as error:  # noqa: BLE001 - 观测点
                self._note("read_service", name, f"raise:{type(error).__name__}")
                continue
            self._note(
                "read_service",
                name,
                sorted(value) if isinstance(value, dict) else value,
            )

    def undeclared_read(self) -> None:
        """R-2：插件读取未声明 ``inject`` 的服务必须报错（漏声明要暴露）。

        有提供方时读取合法成功；无提供方时必须以 AttributeError 失败——
        静默给 None 会把配置错误藏起来。
        """
        self._note(
            "undeclared_read", "had_root_svc", "svc" in self.root.reflect.store
        )

        def plugin(ctx, config):
            self._new_batch()
            self._note("undeclared_read", "value", ctx.svc)  # 未声明 inject

        plugin.__cordis_shape__ = "undeclared"
        plugin.name = "undeclared"
        self.root.plugin(plugin)

    def get_scope(self) -> None:
        """R-5：``ctx.get`` 不要求 inject，但按调用方作用域解析。"""
        self._note("get_scope", "root", self._canon_value(self.root.get("svc")))
        iso = self.last_iso_ctx
        if iso is None:
            self._note("get_scope", "iso", None)
            self._note("get_scope", "iso_label", None)
        else:
            self._note("get_scope", "iso", self._canon_value(iso.get("svc")))
            self._note(
                "get_scope", "iso_label", _chain_isolate(iso).get("svc")
            )

    async def emit_parallel(self) -> None:
        """E-2：``parallel()`` 必须向 internal/dispatch 报 ``parallel``。"""
        await self.root.parallel("ev")
        self._note("emit_parallel", "last", self.dispatch_log[-1:] and self.dispatch_log[-1])

    def waterfall_probe(self) -> None:
        """E-3：waterfall 续体恰好一次推进链条（重复调用不跳链、不重做内置）。"""
        calls: List[str] = []
        # 监听器只注册一次（挂在根 fiber 上），但每次探针都经它触发：它把
        # 记录写进 ``self._wf_calls``——本探针开始时指向本次的列表。
        self._wf_calls = calls
        if not getattr(self, "_wf_installed", False):
            self._wf_installed = True

            def listener(next_fn):
                first = next_fn()
                second = next_fn()  # 第二次调用必须交回同一次的结果
                sink = getattr(self, "_wf_calls", None)
                if sink is not None:
                    sink.append("listener")
                self._note("waterfall_probe", "same", first is second)
                return second

            self.root.on("wf", listener)

        def inner():
            calls.append("inner")
            return "done"

        self.root.events.waterfall(self.root, "wf", inner)
        self._note("waterfall_probe", "calls", tuple(calls))

    def weird_event(self) -> None:
        """E-4：怪事件名不得与事件名映射自身的成员冲突。"""
        before = dict(self.weird_hits)
        for name in _WEIRD_EVENT_NAMES:
            self.root.emit(name)
        self._note("weird_event", "before", tuple(sorted(before.items())))
        self._note("weird_event", "after", tuple(sorted(self.weird_hits.items())))

    def update_disposed(self) -> None:
        """E-9：已释放 fiber 的 ``update`` 必须明确拒绝（不得崩在 await 上）。"""
        fiber = self.last_disposed_fiber
        if fiber is None:
            self._note("update_disposed", "result", "no-target")
            return
        try:
            result = fiber.update({"n": self._label_seq})
        except Exception as error:  # noqa: BLE001 - 观测点：明确拒绝即可
            self._note("update_disposed", "raised", type(error).__name__)
            return
        self._note(
            "update_disposed",
            "result",
            "awaitable" if inspect.isawaitable(result) else type(result).__name__,
        )

    def effect_meta_probe(self) -> None:
        """E-10：根 fiber 的 effect 元数据真实反映实际注册。"""
        root = self.root
        before = len(root.fiber._disposables)
        dispose = root.effect(lambda: None, "meta-probe")
        during = len(root.fiber._disposables)
        dispose()
        after = len(root.fiber._disposables)
        self._note("effect_meta_probe", "delta", (during - before, after - before))
        self._note("effect_meta_probe", "root_total", before)

    @staticmethod
    def _canon_value(value: Any) -> Any:
        """把服务实现值折叠成有界形状（dict → 键集；其余原样）。"""
        if isinstance(value, dict):
            return ("DICT", tuple(sorted(str(k) for k in value)))
        return value

    def contains_check(self) -> None:
        """R-9：``in`` 判定覆盖内置能力与已注册服务，未知名返回假。"""
        root = self.root
        out: Dict[str, Any] = {}
        for name in (
            "extend",
            "isolate",
            "intercept",
            "get",
            "provide",
            "logger",
            "svc",
            "nope",
            "constructor",
        ):
            try:
                out[name] = name in root
            except Exception as error:  # noqa: BLE001 - 观测点
                out[name] = f"raise:{type(error).__name__}"
        self._note("contains_check", "results", tuple(sorted(out.items())))
        self._note("contains_check", "svc_in_props", "svc" in root.reflect.props)


#: **结构层字母表**（闭包证明用）：只有会创建 fiber 的加载操作与生命周期操作。
#: 它决定可达集，故闭包义务只需对这 21 个操作成立。
UOPS_CORE: List[str] = [
    "load_provider",
    "load_consumer",
    "load_effect",
    "load_isolated",
    "load_nested",
    "load_iso_pair",
    "load_accessor",
    "load_mixin",
    "dup_provide",
    "bad_config",
    "bad_effect",
    "throw_observer",
    "undeclared_read",
    "load_misc",
    "dispose_v0",
    "dispose_v1",
    "dispose_v2",
    "dispose_vlast",
    "update_v0",
    "update_v1",
    "restart_v0",
    "restart_v1",
    "emit",
]

#: **探针层字母表**（值语义规约用）：只读或自限操作，在抽象投影上是**自环**
#: ——它们不创建 fiber、不改变全局态，只把可观测值记录进边检查台账。因此：
#:
#: - 它们不进入 BFS 展开（否则同一批状态要按 10 个探针各展开一次，状态数
#:   不增而检查点数翻十倍）；
#: - 覆盖面等价：对**每个**可达状态执行全部探针，检查全部状态性质与边性质，
#:   并额外断言"投影自环"（探针确实没有改变抽象状态）——见 ``run_probes``。
UOPS_PROBE: List[str] = [
    "read_accessor",
    "read_mixin",
    "read_service",
    "contains_check",
    "get_scope",
    "weird_event",
    "waterfall_probe",
    "emit_parallel",
    "update_disposed",
    "effect_meta_probe",
]

#: **回归快档字母表 L5**（13 元）：第六轮的形态集——4 种加载 + 4 个视图定向的
#: 卸载/更新/重启 + 派发。它决定"生命周期主轴"的可达集，实测 depth 7 闭包 /
#: 43 个存在集状态 / 1.5 s，故适合放进每次改动的回归套件；覆盖面小于
#: ``UOPS_TIER1``（无嵌套加载与规约错误路径）与 ``UOPS_CORE``。
UOPS_L5: List[str] = [
    "load_provider",
    "load_consumer",
    "load_effect",
    "load_isolated",
    "dispose_v0",
    "dispose_v1",
    "dispose_v2",
    "dispose_vlast",
    "update_v0",
    "update_v1",
    "restart_v0",
    "restart_v1",
    "emit",
]

#: **一级结构层字母表**（18 元）：第六轮的形态集 + 第七轮一级新增的嵌套加载与
#: 三条规约错误路径。它是回归套件的快档——实测 depth 12 闭包 / 1416 个存在集
#: 状态 / 190 s，代价是覆盖面小于完整结构层（无 accessor / mixin / 隔离对偶 /
#: 混合探针插件形态）。
UOPS_TIER1: List[str] = [
    "load_provider",
    "load_consumer",
    "load_effect",
    "load_isolated",
    "load_nested",
    "dup_provide",
    "bad_config",
    "bad_effect",
    "throw_observer",
    "dispose_v0",
    "dispose_v1",
    "dispose_v2",
    "dispose_vlast",
    "update_v0",
    "update_v1",
    "restart_v0",
    "restart_v1",
    "emit",
]

#: 全字母表（浸润测试用：把结构层与探针层混在一起跑超长序列）
UOPS: List[str] = UOPS_CORE + UOPS_PROBE

_DISPOSE_OPS = frozenset({"dispose_v0", "dispose_v1", "dispose_v2", "dispose_vlast"})

#: 根 fiber 在构造期与本模型初始化时**独立可数**的 effect 注册数（INV-36 的
#: 下界）：``ReflectService.__init__`` 的 4 次 mixin + ``LoggerService`` 的
#: provide 与 exporter = 6；``UWorld.__init__`` 自己注册的 7 个监听器
#: （internal/status、internal/service、internal/dispatch + 4 个怪事件名）。
#: 实测总数 15（框架另有 2 个内部注册），故下界取 13：任何把元数据清空的
#: 改动都会跌破它，而探针自身的一次注册/释放仍能对上 (1, 0) 的增量。
_ROOT_EFFECT_FLOOR = 13

#: E-4 的怪事件名：若事件名映射是裸 dict，这些名字会命中其自身成员
#: （``constructor`` / ``items`` / ``keys`` / ``values``），监听器会被挂到
#: 错误的键上，派发时取到方法而非监听器列表。
_WEIRD_EVENT_NAMES: Tuple[str, ...] = ("constructor", "items", "keys", "values")

#: 会产生新 fiber 的加载类操作（``last_load_fiber`` 对这些操作有意义）。
#: ``throw_observer`` 不在其中：它的受害加载在构造期同步抛错，不产生 fiber。
_LOAD_OPS = frozenset(
    {
        "load_provider",
        "load_consumer",
        "load_effect",
        "load_isolated",
        "load_nested",
        "load_iso_pair",
        "load_accessor",
        "load_mixin",
        "load_misc",
        "undeclared_read",
        "dup_provide",
        "bad_config",
        "bad_effect",
    }
)


def _dispose_target(state: UState, op: str) -> Optional[UFiberView]:
    """该 dispose 操作在前态 ``state`` 上定向的视图（用于幸存二分）。"""
    if not state.views:
        return None
    if op == "dispose_vlast":
        return state.views[-1]
    index = int(op[len("dispose_v"):])
    return state.views[index] if index < len(state.views) else None


async def _uapply_op(world: UWorld, op: str, pending: List[Any]) -> None:
    """执行一个操作；把可能残留的 awaitable 放进 ``pending`` 而非丢弃。"""
    result: Any = None
    # 观测台账按操作清零：每个操作的观测只在本次迁移的检查中有效。
    world.obs.clear()
    # 加载类操作的观测以"本次是否真的产生了新 fiber"为准：操作前先清空，
    # 加载失败（同步抛错，如 throw_observer 之后的路径）时保持 None，
    # 后续边检查据此区分"加载失败"与"加载成功"。
    world.last_load_fiber = None
    if op in _DISPOSE_OPS:
        if op == "dispose_vlast":
            result = world.dispose_view_last()
        else:
            result = world.dispose_view(int(op[len("dispose_v"):]))
    elif op.startswith("update_v"):
        result = world.update_view(int(op[len("update_v"):]))
    elif op.startswith("restart_v"):
        result = world.restart_view(int(op[len("restart_v"):]))
    else:
        result = getattr(world, op)()
    if op in _LOAD_OPS:
        fibers = world.fibers()
        world.last_load_fiber = (
            max(fibers, key=lambda f: f.uid or 0) if fibers else None
        )
    if inspect.isawaitable(result):
        pending.append(result)
        try:
            await result
        finally:
            pending.remove(result)


async def ureplay(path: Sequence[str]) -> UWorld:
    """从初态重放一条操作序列（与 verify.replay 同构，改用 UWorld）。

    合法错误路径（重复 provide、对已释放 fiber 操作等）不得中断重放；中断处
    的 awaitable 必须被排空而不是丢弃（INV-7：验证器自身不产生悬挂协程）。

    每个操作之后立即执行**操作级边检查**（§4.2：INV-12~16）——与 INV-1/3b
    同构，在迁移发生处判定，违规记入 ``edge_violations``。这让 BFS、一致性
    检验与浸润三条路径都自动获得逐迁移的检查，无需各自实现。
    """
    world = UWorld()
    # 构造期的 logger provide 会触发一次 internal/service 事件——那时任何
    # 监听器都还不存在，把它从 E-5 的期望值里去掉（否则根监听器的"应收"
    # 会多算一条它客观上收不到的事件）。
    world.event_log.clear()
    # 派发/推送账本是进程级的，每条路径从空账本开始，保证边检查与该路径对应。
    V._DISPATCH_LOG.clear()
    V._PUSH_LOG.clear()
    V._EDGE_SINK = world.edge_violations
    for op in path:
        pending: List[Any] = []
        try:
            await _uapply_op(world, op, pending)
        except Exception:  # noqa: BLE001 - 合法错误路径
            pass
        await V._drain(pending)
        try:
            await V._settle(world)
        except Exception:  # noqa: BLE001
            pass
        for code, message in _op_checks(world, op):
            world.edge_violations.append(f"{code} {message}")
    # 台账接收器复位：world 自己持有列表引用，后续 verify.py 的运行不再
    # 往本 world 的台账里追加。
    global _ACCESSOR_REGS, _NOTIFY_EMITS
    _ACCESSOR_REGS = None
    _NOTIFY_EMITS = None
    return world


# ---------------------------------------------------------------------------
# §4 边性质插桩：级联完备性（INV-10）+ 服务事件记录（INV-22）
# ---------------------------------------------------------------------------
#
# 无界定理的前提 H5：notify 的级联必须触达**每一个**依赖了被通知服务的
# fiber——否则大系统里未被刷新的消费方就会停留在陈旧状态，而 BFS 的存在集
# 探索只有一份代表实例，看不到它。这里在 notify 处插桩：快照"应该被刷新的
# fiber 集合"，与"实际被刷新的集合"比对，遗漏即违规。
#
# 同一插桩还记录每次 ``internal/service`` 事件的 (服务名, 作用域 label)——
# 这是 INV-22（E-5：服务变更事件只送达同一隔离作用域的监听器）的期望值
# 来源：每个监听器实际收到的事件必须与"自己作用域内的事件"逐一相符。


#: accessor 注册的当前接收表（None = 无 UWorld 在跑，不记录）。
#: 由 ``UWorld.__init__`` 收养：构造期（内置 mixin 的 16 个 accessor）与
#: 运行期（插件注册）都进同一张表，``_uview`` 据此算每 fiber 的 accessors。
_ACCESSOR_REGS: Optional[List[Tuple[str, Any]]] = None

#: 事件日志的当前接收表（None = 无 UWorld 在跑，不记录）。
_NOTIFY_EMITS: Optional[List[Tuple[Any, ...]]] = None


def _instrument_notify() -> None:
    """给 ``ReflectService.notify`` 装上级联完备性断言（进程内幂等）。

    内层实现经``_cordis_notify_inner`` 间接调用：变异测试可以只替换内层
    （模拟"级联不完整"），而完备性断言（外层）保持装配——否则变异会把断言
    一起绕掉，INV-10 便永远得不到 exercised。
    """
    if getattr(ReflectService.notify, "__cordis_traced__", False):
        return
    original_notify = ReflectService.notify
    original_refresh = fiber_mod.Fiber._refresh
    refreshed: Set[int] = set()

    def traced_refresh(self: Any) -> None:
        refreshed.add(id(self))
        return original_refresh(self)

    inner: List[Any] = [original_notify]

    def traced_notify(
        self: Any,
        names: List[str],
        filter_fn: Any = None,
        scope_ctx: Any = None,
    ) -> Any:
        origin = scope_ctx if scope_ctx is not None else self.ctx
        if _NOTIFY_EMITS is not None:
            # 记录本次通知触发的 internal/service 事件（按作用域 label）。
            # label 即 ``_chain_isolate(origin).get(name)``：根作用域等于
            # 服务名本身，隔离作用域是 isolate label——两者可判别。
            origin_isolate = _chain_isolate(origin)
            for name in names:
                _NOTIFY_EMITS.append(("emit", name, origin_isolate.get(name)))
        if filter_fn is None:
            # 与 notify 的默认过滤器保持一致（同作用域才通知）
            def filter_fn(ctx: Any, name: str, _origin: Any = origin) -> bool:  # type: ignore[misc]
                return _chain_isolate(ctx).get(name) == _chain_isolate(_origin).get(name)

        expected: Set[int] = set()
        for runtime in self.ctx.registry.values():
            for fiber in runtime.fibers:
                if fiber.inject is None:
                    continue
                for name in names:
                    if name in fiber.inject and filter_fn(fiber.ctx, name):
                        expected.add(id(fiber))
                        break
        refreshed.clear()
        try:
            return inner[0](self, names, filter_fn, scope_ctx)
        finally:
            missing = expected - refreshed
            if missing and V._EDGE_SINK is not None:
                V._EDGE_SINK.append(
                    f"INV-10 notify 级联遗漏 {len(missing)} 个依赖方（通知 {list(names)}）"
                )

    setattr(traced_notify, "__cordis_traced__", True)
    setattr(traced_refresh, "__cordis_traced__", True)
    ReflectService.notify = traced_notify
    fiber_mod.Fiber._refresh = traced_refresh
    #: 内层实现槽（变异测试替换目标）
    ReflectService._cordis_notify_inner = inner  # type: ignore[attr-defined]


_instrument_notify()


def _instrument_accessor() -> None:
    """给 ``ReflectService.accessor`` 装上注册台账（进程内幂等）。

    accessor 的归属（哪个 fiber 注册了它）无法从 ``props`` 反推，而 R-6/R-7
    的生命周期半条（"随注册插件卸载而消失"）需要逐 fiber 的归属信息。这里
    在唯一的注册收口点记录 ``(名字, 注册方 fiber)``：``ctx.accessor`` 与
    ``ctx.mixin``（后者经 ``ReflectService.mixin`` 转调本方法）都经过它，
    一个插桩同时覆盖两条规约。记录只进接收表，不改变任何行为。
    """
    if getattr(ReflectService.accessor, "__cordis_traced__", False):
        return
    original_accessor = ReflectService.accessor

    def traced_accessor(
        self: Any,
        name: str,
        options: Any,
        caller: Any = None,
    ) -> Any:
        result = original_accessor(self, name, options, caller)
        if _ACCESSOR_REGS is not None:
            # 只记录**真正注册成功**的 accessor：同名重复注册会在 ``_setup``
            # 里抛错（"property already declared as accessor"），此时它从未
            # 进入 props。按身份确认（``props[name] is options``）而不是在
            # 调用处记账，否则失败的 fiber 会带着它并不持有的 accessor 名。
            if self.props.get(name) is options:
                owner = caller if caller is not None else self.ctx
                fiber = getattr(owner, "fiber", None)
                if fiber is not None:
                    _ACCESSOR_REGS.append((name, fiber))
        return result

    setattr(traced_accessor, "__cordis_traced__", True)
    ReflectService.accessor = traced_accessor


_instrument_accessor()


#: 边性质违规消息 → 性质编号的映射（插桩消息自带编号时优先）
_EDGE_CODES: Tuple[Tuple[str, str], ...] = (
    ("INV-1 ", "INV-1"),
    ("INV-10 ", "INV-10"),
    ("INV-12 ", "INV-12"),
    ("INV-13 ", "INV-13"),
    ("INV-14 ", "INV-14"),
    ("INV-15 ", "INV-15"),
    ("INV-16 ", "INV-16"),
    ("INV-17 ", "INV-17"),
    ("INV-18 ", "INV-18"),
    ("INV-19 ", "INV-19"),
    ("INV-20 ", "INV-20"),
    ("INV-21 ", "INV-21"),
    ("INV-22 ", "INV-22"),
    ("INV-23 ", "INV-23"),
    ("INV-24 ", "INV-24"),
    ("INV-25 ", "INV-25"),
    ("INV-26 ", "INV-26"),
    ("INV-27 ", "INV-27"),
    ("INV-28 ", "INV-28"),
    ("INV-29 ", "INV-29"),
    ("INV-30 ", "INV-30"),
    ("INV-31 ", "INV-31"),
    ("INV-32 ", "INV-32"),
    ("INV-33 ", "INV-33"),
    ("INV-34 ", "INV-34"),
    ("INV-35 ", "INV-35"),
    ("INV-36 ", "INV-36"),
    ("派发非逆序", "INV-3b"),
    ("notify 级联遗漏", "INV-10"),
)


def _edge_violations(world: UWorld) -> List[Tuple[str, str]]:
    """汇总一次重放中观测到的全部边性质违规（带性质编号）。

    来源：``clear()`` 插桩（INV-3b 逆序派发）、notify 插桩（INV-10 级联
    完备）、``_on_status`` 覆写（INV-1 合法迁移）、操作级检查（INV-12~16，
    由 ``ureplay`` 在每个操作后写入 ``edge_violations``），加上 disposer
    重复执行账本（INV-3a）。
    """
    out: List[Tuple[str, str]] = []
    for message in world.edge_violations:
        code = "EDGE"
        for needle, tag in _EDGE_CODES:
            if needle in message:
                code = tag
                break
        out.append((code, message))
    if world.duplicate_runs:
        out.append(
            ("INV-3a", f"disposer 被重复执行：{list(world.duplicate_runs[:3])}")
        )
    return out


# ---------------------------------------------------------------------------
# §4.2 操作级边检查：规约"该报错时报错 / 级联完整 / 拆卸完整"（第七轮新增）
# ---------------------------------------------------------------------------
#
# 这五条把剩余规约中"拒绝非法操作"与"生命周期完整性"的两半形式化。它们与
# INV-1/3b 同构：在迁移发生处判定，违规记入 ``edge_violations``，**不**进入
# 抽象状态。每条检查只依赖操作处的观测（``world.obs``）与 settle 之后的
# 世界状态，因此 BFS、一致性检验、浸润三条路径自动全覆盖。


def _last_obs(world: UWorld, op: str, name: str) -> Optional[Any]:
    """取本次操作中名为 ``name`` 的最后一条观测值（无则 None）。"""
    found = None
    for tag, obs_name, value in world.obs:
        if tag == op and obs_name == name:
            found = value
    return found


def _check_dup_provide(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-12（L-5）：重复 provide 必须报错，且仅当确有提供方时。

    操作处记录"根作用域是否已有 svc 提供方"（``had_root_svc``）；settle
    之后读 ``last_load_fiber``：有提供方 ⇒ 本次加载必须以 RuntimeError
    失败（不得静默覆盖）；无提供方 ⇒ 提供合法成功。
    """
    had = _last_obs(world, op, "had_root_svc")
    if had is None:
        return []
    fiber = world.last_load_fiber
    if fiber is None:
        return [("INV-12", "重复 provide 操作未产生 fiber")]
    if had:
        error = getattr(fiber, "_error", None)
        if fiber.state != FiberState.FAILED or not isinstance(error, RuntimeError):
            return [
                (
                    "INV-12",
                    f"已存在根作用域 svc 提供方，重复 provide 未报错："
                    f"state={FiberState.name_of(fiber.state)} "
                    f"error={type(error).__name__ if error else None}",
                )
            ]
    else:
        if fiber.state != FiberState.ACTIVE:
            return [
                (
                    "INV-12",
                    f"无提供方时 provide 应成功，实际 state="
                    f"{FiberState.name_of(fiber.state)}",
                )
            ]
    return []


def _check_failed_with(world: UWorld, code: str, spec: str, error_type: type) -> List[Tuple[str, str]]:
    """共用判定：``last_load_fiber`` 必须以指定错误类型失败。"""
    fiber = world.last_load_fiber
    if fiber is None:
        return [(code, f"{spec} 操作未产生 fiber")]
    error = getattr(fiber, "_error", None)
    if fiber.state != FiberState.FAILED or not isinstance(error, error_type):
        return [
            (
                code,
                f"{spec}：state={FiberState.name_of(fiber.state)} "
                f"error={type(error).__name__ if error else None}，"
                f"期望 FAILED + {error_type.__name__}",
            )
        ]
    return []


def _check_throw_observer(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-15（L-9）：观察者抛错后，已启动的插件仍须被完整拆卸。"""
    raised = _last_obs(world, op, "raised")
    if raised is None:
        return []
    out: List[Tuple[str, str]] = []
    if not raised:
        out.append(("INV-15", f"观察者抛错未向上传播（raised={raised!r}）"))
    victim = world._observer_victim
    if victim is not None:
        if any(cb is victim for cb in world.root.registry.keys()):
            out.append(("INV-15", "拆卸后 runtime 记录仍残留"))
        leaked = [
            f
            for f in world.fibers()
            if f.runtime is not None and f.runtime.callback is victim
        ]
        if leaked:
            out.append(
                ("INV-15", f"拆卸后仍有 {len(leaked)} 个残留 fiber（半释放状态）")
            )
    return out


def _check_cascade(world: UWorld) -> List[Tuple[str, str]]:
    """INV-16（U-6）：父卸载必须级联卸载子 fiber（无孤儿）。

    任何存活 fiber 的插件祖先链上出现已释放 fiber 即违规。顶层加载的
    fiber 父链直达根 fiber（永不为释放态），故此检查只对嵌套加载
    （``depth ≥ 1``）有约束力——``load_nested`` 是它的可达性来源。
    """
    root_fiber = world.root.fiber
    for fiber in world.fibers():
        if fiber.uid is None:
            continue
        current = fiber
        for _ in range(8):
            parent = getattr(current, "parent", None)
            parent_fiber = _fiber_of_ctx(parent) if parent is not None else None
            if parent_fiber is None or parent_fiber is current:
                break
            if parent_fiber is root_fiber:
                break
            if parent_fiber.uid is None:
                return [
                    (
                        "INV-16",
                        f"fiber(uid={fiber.uid}, shape="
                        f"{getattr(fiber.runtime.callback, '__cordis_shape__', '?')}) "
                        f"的插件祖先已释放但自身仍存活（级联缺失）",
                    )
                ]
            current = parent_fiber
    return []


def _check_read_accessor(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-18（R-6 求值半条）：计算型属性每次读取都重新求值。

    accessor 存活（``acc`` 在 props 中）时，每次读取必须让求值计数恰好 +1
    且返回新值——缓存过一次就到此现形。accessor 不存在时读取得 None 且
    不求值（规约：插件卸载后属性不再被解析）。
    """
    alive = _last_obs(world, op, "acc_in_props")
    value = _last_obs(world, op, "value")
    before = _last_obs(world, op, "evals_before")
    after = _last_obs(world, op, "evals_after")
    if alive is None or before is None or after is None:
        return []
    if alive:
        if after != before + 1:
            return [
                (
                    "INV-18",
                    f"读取 accessor 未重新求值：evals {before} -> {after}",
                )
            ]
        if value != after:
            return [
                ("INV-18", f"accessor 返回值 {value!r} 与求值计数 {after} 不符")
            ]
    else:
        if after != before:
            return [("INV-18", f"accessor 不存在时仍发生求值：{before} -> {after}")]
        if value is not None:
            return [("INV-18", f"accessor 不存在时读取应得 None，实际 {value!r}")]
    return []


def _check_read_mixin(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-19（R-7 求值半条）：mixin 成员随源服务与注册方存活而可读。

    - mixin 存活（``ping`` 在 props）且源服务存活 ⇒ 读到成员值；
    - 任一死亡 ⇒ 读得 None（mixin 的 get 钩子对失效源返回 None；mixin 自身
      被摘除后名字退回普通未知属性，根上下文读取同样得 None）。
    """
    in_props = _last_obs(world, op, "ping_in_props")
    tools_alive = _last_obs(world, op, "tools_alive")
    value = _last_obs(world, op, "value")
    if in_props is None or tools_alive is None:
        return []
    if in_props and tools_alive:
        if value != "pong":
            return [
                ("INV-19", f"mixin 成员应读到 'pong'，实际 {value!r}")
            ]
    else:
        if value is not None:
            return [
                (
                    "INV-19",
                    f"mixin/源服务已失效时读取应得 None，实际 {value!r}",
                )
            ]
    return []


def _check_iso_effect(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-20（R-8）：隔离作用域上下文上注册 effect 必须成功。"""
    result = _last_obs(world, op, "iso_effect")
    if result is None:
        return []
    if result != "ok":
        return [
            ("INV-20", f"隔离作用域上 ctx.effect 失败：{result}")
        ]
    return []


def _check_contains(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-21（R-9）：``in`` 覆盖内置能力与已注册服务，未知名返回假。"""
    results = _last_obs(world, op, "results")
    svc_in_props = _last_obs(world, op, "svc_in_props")
    if results is None or svc_in_props is None:
        return []
    got = dict(results)
    out: List[Tuple[str, str]] = []
    for name in ("extend", "isolate", "intercept", "get", "provide", "logger"):
        if got.get(name) is not True:
            out.append(
                ("INV-21", f"内置能力 {name!r} 的 in 判定应为 True，实际 {got.get(name)!r}")
            )
    if got.get("svc") is not svc_in_props:
        out.append(
            (
                "INV-21",
                f"已注册服务的 in 判定应随提供状态（{svc_in_props}）变化，"
                f"实际 {got.get('svc')!r}",
            )
        )
    for name in ("nope", "constructor"):
        if got.get(name) is not False:
            out.append(
                ("INV-21", f"未知名 {name!r} 的 in 判定应为 False，实际 {got.get(name)!r}")
            )
    return out


def _check_service_isolation(world: UWorld) -> List[Tuple[str, str]]:
    """INV-22（E-5）：服务变更事件只送达"对该名字同一隔离作用域"的监听器。

    期望值按**发射时刻**计算，故按 ``world.event_log`` 的时间顺序重放：
    观测者的 ``alive`` / ``dead`` 标记与 ``emit`` 记录在同一条有序日志里，
    重放时维护"当前存活的观察者集合"。对每次 emit (name, label)：

        监听方收到 ⟺ 该监听方当时存活 且
                     _chain_isolate(监听方 ctx).get(name) == label

    两处必须按**名字**判定（隔离只对建立过 isolate 的名字生效，未隔离的
    名字沿原型链继承根作用域标签）：
    - 观察者若晚于某次 emit 存在，不该被期望收到那一次事件；
    - 观察者若在自己的插件拆卸窗口内已失效，也不该再收到事件。

    多收 = 跨作用域泄漏；少收 = 自己作用域的事件被漏掉。本条期望值判定被
    实测修正过两次（见函数 docstring 的历史）。
    """
    out: List[Tuple[str, str]] = []
    root_map = _chain_isolate(world.root)
    alive: Set[int] = set()
    expected_root: Dict[str, int] = {}
    expected_iso: Dict[int, Dict[str, int]] = {}
    for entry in world.event_log:
        kind = entry[0]
        if kind == "alive":
            alive.add(entry[1])
            continue
        if kind == "dead":
            alive.discard(entry[1])
            continue
        _, name, label = entry
        if root_map.get(name) == label:
            expected_root[name] = expected_root.get(name, 0) + 1
        for index in sorted(alive):
            ctx = world.iso_ctxs[index] if index < len(world.iso_ctxs) else None
            if ctx is None:
                continue
            if _chain_isolate(ctx).get(name) == label:
                bucket = expected_iso.setdefault(index, {})
                bucket[name] = bucket.get(name, 0) + 1
    if dict(world.root_hits) != expected_root:
        out.append(
            (
                "INV-22",
                f"根作用域 internal/service 监听器收到 {dict(world.root_hits)}，"
                f"期望 {expected_root}",
            )
        )
    for index, hits in enumerate(world.iso_hits):
        expected = expected_iso.get(index, {})
        if dict(hits) != expected:
            label = world.iso_labels[index] if index < len(world.iso_labels) else None
            out.append(
                (
                    "INV-22",
                    f"隔离作用域 {label!r} 的 internal/service 监听器收到 "
                    f"{dict(hits)}，期望 {expected}",
                )
            )
    return out


def _alive_shapes(world: UWorld) -> Dict[str, int]:
    """按结构标签统计存活插件数（三级检查的期望值来源）。"""
    counts: Dict[str, int] = {}
    for fiber in world.fibers():
        if fiber.uid is None or fiber.runtime is None:
            continue
        if fiber.state in (FiberState.FAILED, FiberState.DISPOSED):
            continue  # 失败/已释放的 fiber 不持有任何资源
        shape = getattr(fiber.runtime.callback, "__cordis_shape__", "?")
        counts[shape] = counts.get(shape, 0) + 1
    return counts


def _check_load_stable(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-23（L-1）：加载后 fiber 必须停在稳定态（不得停在 LOADING/UNLOADING）。

    ``await fiber`` 的契约是"驱动到稳定态再返回"；若 settle 之后仍停在
    LOADING / UNLOADING，说明 await 的收敛承诺不成立。
    """
    fiber = world.last_load_fiber
    if fiber is None:
        return []
    state = FiberState.name_of(fiber.state)
    if state in ("LOADING", "UNLOADING"):
        return [
            ("INV-23", f"{op} 之后 fiber 停在非稳定态 {state}（await 契约不成立）")
        ]
    handle = _last_obs(world, "dup_provide", "handle")
    if op == "dup_provide" and handle is not None:
        names, has_await, has_dispose = handle
        if not (has_await and has_dispose):
            return [
                (
                    "INV-23",
                    f"plugin() 返回值不是可 await 的 fiber 句柄：{handle}",
                )
            ]
    return []


def _check_load_misc(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """三级合并检查：L-6 / L-7 / E-6 / E-7 / E-8。"""
    if op != "load_misc":
        return []
    out: List[Tuple[str, str]] = []
    seen = world.seen
    shapes = _alive_shapes(world)
    # L-6：服务方法体里的 self.ctx 必须是**调用方**（另一个插件的上下文）
    if "api_ctx_is_caller" in seen:
        if seen["api_ctx_is_caller"] is not True:
            out.append(
                (
                    "INV-24",
                    "服务方法体里的 self.ctx 不是调用方上下文（L-6 调用方重绑失效）",
                )
            )
    # L-7：异步 effect 主体在**注册时**执行（历史缺陷：直到卸载才跑）
    starts = world.async_log.count("start")
    disposed = world.async_log.count("disposed")
    alive_async = shapes.get("async-probe", 0)
    if starts - disposed != alive_async:
        out.append(
            (
                "INV-25",
                f"异步 effect 主体未在注册时执行：start={starts} disposed={disposed} "
                f"存活 async 插件={alive_async}",
            )
        )
    # E-6：logger 名字由调用方 fiber 派生 + exporter 归调用方所有
    if "logger_name" in seen and seen["logger_name"] != "log-probe":
        out.append(
            (
                "INV-32",
                f"ctx.logger() 的名字应为调用方 fiber 名 'log-probe'，"
                f"实际 {seen['logger_name']!r}",
            )
        )
    exporters = len(world.root._logger_service.exporters)  # type: ignore[attr-defined]
    expected_exporters = 1 + shapes.get("log-probe", 0)  # 1 = 内置缓冲 exporter
    if exporters != expected_exporters:
        out.append(
            (
                "INV-32",
                f"logger exporter 归属错位：实际 {exporters} 个，"
                f"期望 {expected_exporters} 个（内置 1 + 存活 log 插件）",
            )
        )
    # E-7：带 cause 的错误链完整记录（内层与外层都在，且内层在前）
    chain = [
        message
        for message in world.root._logger_service.buffer  # type: ignore[attr-defined]
        if getattr(message, "name", None) == "chain-probe"
    ]
    texts = []
    for message in chain:
        args = list(getattr(message, "args", []) or [])
        if args and isinstance(args[0], BaseException):
            outer = getattr(args[0], "__cordis_outer__", None)
            first = args[0]
            texts.append(
                str(outer or (first.args[0] if first.args else first))
            )
        else:
            texts.append(str(args[0]) if args else "")
    # 每次 chain-probe 加载记录一对（内层在前、外层在后）；次数随加载次数
    # 增长，故按对分组判定而非比对整表。
    if texts and (
        len(texts) % 2 != 0
        or any(
            pair != ("inner", "outer")
            for pair in zip(texts[::2], texts[1::2])
        )
    ):
        out.append(
            (
                "INV-33",
                f"带 cause 的错误链记录不完整：{texts}（每份期望 ['inner','outer']）",
            )
        )
    # E-8：多级 intercept 按 key 合并，祖先条目的其它 key 不丢
    if "intercept_merged" in seen:
        if seen["intercept_merged"] != (("a", "1"), ("b", "20"), ("c", "3")):
            out.append(
                (
                    "INV-34",
                    f"多级 intercept 合并结果错误：{seen['intercept_merged']}",
                )
            )
    return out


def _check_read_service(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-26（R-1）：已 provide 的服务从其作用域解析到实现值。"""
    out: List[Tuple[str, str]] = []
    store = world.root.reflect.store
    root_svc = "svc" in store
    value = _last_obs(world, op, "svc")
    if value is None and root_svc:
        return [("INV-26", "svc 有根作用域提供方但读取未得到值")]
    if value is not None and not root_svc:
        return [("INV-26", f"无提供方却读到 svc={value!r}")]
    if value is not None and root_svc and value != "v":
        return [("INV-26", f"svc 实现值应为 'v'，实际 {value!r}")]
    tools = _last_obs(world, op, "tools")
    if tools is None and "tools" in store:
        return [("INV-26", "tools 有提供方但读取未得到值")]
    if tools is not None and "tools" not in store:
        return [("INV-26", f"tools 无提供方却读到 {tools!r}")]
    if tools is not None and tools != ["ping"]:
        return [("INV-26", f"tools 实现值应为 dict{{ping}}，实际 {tools!r}")]
    return out


def _check_undeclared_read(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-27（R-2）：未声明 inject 的读取，有提供方则成功、无则报错。"""
    fiber = world.last_load_fiber
    if fiber is None:
        return [("INV-27", "undeclared_read 未产生 fiber")]
    error = getattr(fiber, "_error", None)
    if fiber.state != FiberState.FAILED or not isinstance(error, AttributeError):
        return [
            (
                "INV-27",
                f"未声明 inject 的读取必须以 AttributeError 报错（无论是否已有"
                f"提供方——插件上下文只解析已声明依赖），实际 "
                f"{FiberState.name_of(fiber.state)}/{type(error).__name__ if error else None}",
            )
        ]
    return []


def _check_get_scope(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-28（R-5）：``ctx.get`` 按调用方作用域解析。"""
    store = world.root.reflect.store
    root_value = _last_obs(world, op, "root")
    iso_value = _last_obs(world, op, "iso")
    if root_value is None and "svc" in store:
        return [("INV-28", "根作用域有 svc 提供方，root.get('svc') 却读到 None")]
    if root_value is not None and "svc" not in store:
        return [("INV-28", f"根作用域无 svc 提供方，却读到 {root_value!r}")]
    if root_value is not None and root_value != "v":
        return [("INV-28", f"root.get('svc') 应为 'v'，实际 {root_value!r}")]
    label = _last_obs(world, op, "iso_label")
    if label is None or label == "svc":
        return []  # 本次没有隔离读取目标（未建立隔离作用域）
    expected_iso = "ISO" if label in store else None
    if iso_value != expected_iso:
        return [
            (
                "INV-28",
                f"隔离作用域 {label!r} 的 get('svc') 应为 {expected_iso!r}，"
                f"实际 {iso_value!r}",
            )
        ]
    return []


def _check_dispatch_mode(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-29（E-2）：派发诊断必须如实反映本次策略。"""
    expected = {"emit_parallel": "parallel", "emit": "emit"}.get(op)
    if expected is None:
        return []
    last = world.dispatch_log[-1] if world.dispatch_log else None
    if last is None or last[0] != expected:
        return [
            ("INV-29", f"{op} 的派发诊断上报 {last!r}，期望模式 {expected!r}")
        ]
    return []


def _check_waterfall(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-30（E-3）：waterfall 续体恰好一次推进（重复调用不重做内置行为）。"""
    calls = _last_obs(world, op, "calls")
    same = _last_obs(world, op, "same")
    if calls is None:
        return []
    out: List[Tuple[str, str]] = []
    if calls.count("inner") != 1:
        out.append(
            ("INV-30", f"内置行为被执行 {calls.count('inner')} 次（续体被多次推进）：{calls}")
        )
    if calls.count("listener") != 1:
        out.append(("INV-30", f"监听器被跳过或重复执行：{calls}"))
    if same is not True:
        out.append(("INV-30", "续体第二次调用未交回同一次的结果"))
    return out


def _check_weird_event(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-31（E-4）：怪事件名不得与事件名映射自身的成员冲突。"""
    before = dict(_last_obs(world, op, "before") or ())
    after = dict(_last_obs(world, op, "after") or ())
    out: List[Tuple[str, str]] = []
    for name in _WEIRD_EVENT_NAMES:
        if after.get(name, 0) - before.get(name, 0) != 1:
            out.append(
                (
                    "INV-31",
                    f"事件名 {name!r} 的监听器命中增量 "
                    f"{after.get(name, 0) - before.get(name, 0)}（期望 1）",
                )
            )
    return out


def _check_update_disposed(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-35（E-9 后半）：已释放 fiber 的 update 必须明确拒绝。"""
    raised = _last_obs(world, op, "raised")
    result = _last_obs(world, op, "result")
    if raised is None and result is None:
        return []
    if raised is not None:
        if raised != "CordisError":
            return [
                ("INV-35", f"已释放 fiber 的 update 应以 CordisError 拒绝，实际 {raised}")
            ]
        return []
    if result == "no-target":
        return []
    if result != "awaitable":
        return [
            ("INV-35", f"已释放 fiber 的 update 返回非 awaitable：{result}")
        ]
    return []


def _check_effect_meta(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """INV-36（E-10）：根 fiber 的 effect 元数据真实反映实际注册。"""
    delta = _last_obs(world, op, "delta")
    total = _last_obs(world, op, "root_total")
    if delta is None:
        return []
    out: List[Tuple[str, str]] = []
    if delta != (1, 0):
        out.append(
            ("INV-36", f"根 fiber effect 元数据不自洽：注册后增量 {delta[0]}、释放后增量 {delta[1]}（期望 1 / 0）")
        )
    if not total or total < _ROOT_EFFECT_FLOOR:
        out.append(
            (
                "INV-36",
                f"根 fiber 只报告 {total} 个 effect（下界 {_ROOT_EFFECT_FLOOR}："
                f"构造期内置 6 + 本模型监听器 7）——元数据与实际注册不符（E-10）",
            )
        )
    return out


#: 操作 → 操作级检查（只对会产生观测的操作登记）
_OP_CHECKS: Dict[str, Callable[[UWorld, str], List[Tuple[str, str]]]] = {
    "dup_provide": _check_dup_provide,
    "bad_config": lambda w, op: _check_failed_with(
        w, "INV-13", "配置校验失败", fiber_mod.ValidationError
    ),
    "bad_effect": lambda w, op: _check_failed_with(
        w, "INV-14", "非法 effect 形态", TypeError
    ),
    "throw_observer": _check_throw_observer,
    "load_iso_pair": _check_iso_effect,
    "load_misc": _check_load_misc,
    "read_accessor": _check_read_accessor,
    "read_service": _check_read_service,
    "undeclared_read": _check_undeclared_read,
    "get_scope": _check_get_scope,
    "emit_parallel": _check_dispatch_mode,
    "waterfall_probe": _check_waterfall,
    "weird_event": _check_weird_event,
    "update_disposed": _check_update_disposed,
    "effect_meta_probe": _check_effect_meta,
    "read_mixin": _check_read_mixin,
    "contains_check": _check_contains,
}


def _op_checks(world: UWorld, op: str) -> List[Tuple[str, str]]:
    """一次操作之后的全部操作级边检查（含级联与服务隔离检查）。"""
    out: List[Tuple[str, str]] = []
    check = _OP_CHECKS.get(op)
    if check is not None:
        out.extend(check(world, op))
    if op in _LOAD_OPS:
        out.extend(_check_load_stable(world, op))  # L-1：加载后停在稳定态
    if op.startswith("update_v"):
        awaitable = _last_obs(world, "update_view", "awaitable")
        if awaitable is False:
            out.append(
                ("INV-35", f"{op} 的 update 返回非 awaitable（await 会崩）")
            )
    if op == "emit":
        out.extend(_check_dispatch_mode(world, op))
    out.extend(_check_cascade(world))
    out.extend(_check_service_isolation(world))
    return out


# ---------------------------------------------------------------------------
# §5 算法主体：存在集 BFS + 幸存二分 + 不变式
# ---------------------------------------------------------------------------


@dataclass
class Counterexample:
    path: List[str]
    violations: List[Tuple[str, str]]
    state: UState

    def render(self) -> str:
        lines = [f"反例路径（{len(self.path)} 步）："]
        for i, op in enumerate(self.path):
            lines.append(f"  {i:>3}. {op}")
        lines.append("终态：")
        lines.append("\n".join("    " + l for l in self.state.render().splitlines()))
        lines.append("违反：")
        for name, reason in self.violations:
            lines.append(f"  [{name}] {reason}")
        return "\n".join(lines)


@dataclass
class UProofResult:
    depth_reached: int
    states: int
    checked: int
    closure_ok: bool
    counterexample: Optional[Counterexample] = None
    truncated: bool = False
    elapsed: float = 0.0

    def verdict(self) -> str:
        if self.counterexample is not None:
            return "REFUTED（找到真实反例）"
        if self.closure_ok:
            return "PROVED-UNBOUNDED（归纳不变式封闭，插件数无界、长度无界）"
        if self.truncated:
            return "INCONCLUSIVE（状态数超限，未闭包）"
        return "INCONCLUSIVE（深度耗尽，未闭包）"


def _descendants(pre: UState, view: UFiberView) -> List[UFiberView]:
    """``pre`` 中 ``view`` 的全部后代视图（传递闭包，按 parent_key 归纳）。

    U-6 的级联语义：父 fiber 被卸载时，挂在它上面的子 fiber 一并消失。
    存在集层面这意味这"父视图消失"必须连"只由它支撑的子视图"一起消失——
    幸存二分因此是**级联感知**的，两个方向都要连子树一起处理。
    """
    out: List[UFiberView] = []
    frontier = [view.key]
    seen = {view.key}
    while frontier:
        current = frontier.pop()
        for candidate in pre.views:
            if candidate.parent_key == current and candidate.key not in seen:
                seen.add(candidate.key)
                out.append(candidate)
                frontier.append(candidate.key)
    return out


def _bifurcate(successor: UState, disposed: UFiberView, pre: UState) -> UState:
    """幸存二分后继（正向）：被处置视图在计数 ≥2 时仍有一个 fiber 存活。

    该幸存者与被处置者**同视图**（H1），因此：

    - 它继承被处置者的全部 store 贡献（provides 中的条目仍在——同视图 ⇒
      同贡献，见 H3）；
    - 它自身不被级联改写（H4，由一致性检验验证）；
    - **级联感知**（U-6）：它的后代视图也一并幸存——代表实例（计数=1）
      中父子同消，重复实例（计数≥2）中幸存父带着幸存子。后代视图的 store
      贡献同样要恢复。

    与 ``_unbifurcate`` 互逆；两者合起来覆盖计数 =1 与 ≥2 两种情形。
    """
    survivors = [disposed, *_descendants(pre, disposed)]
    store = set(successor.g.store)
    props = set(successor.g.props)
    for view in survivors:
        for name, scope in view.provides:
            store.add((name, scope, view.alive, view.state))
        # 幸存视图仍持有它注册的 accessor（R-6/R-7）：代表实例里这些条目已
        # 随被处置 fiber 被摘除，幸存分支必须一并恢复。
        for name in view.accessors:
            props.add((name, "accessor"))
    g2 = UGlobal(
        store=tuple(sorted(store)),
        dead_listeners=successor.g.dead_listeners,
        provider_seen=successor.g.provider_seen,
        props=tuple(sorted(props)),
        builtin_accessors=successor.g.builtin_accessors,
    )
    views = tuple(
        sorted(set(successor.views) | set(survivors), key=lambda v: v.key)
    )
    return UState(g2, views)


def _unbifurcate(successor: UState, disposed: UFiberView, pre: UState) -> UState:
    """幸存二分后继（反向）：被处置视图的最后一个 fiber 消失。

    代表路径恰好是该视图计数 ≥2 的实例时，重放只给出"幸存"后继；计数 =1
    的实例（同一存在集状态的另一具体实例）会给出"视图消失"后继，必须补齐。

    **级联感知**（U-6）：最后一个父 fiber 消失时，其全部后代随级联消失，
    故本分支连后代视图一起移除；store 条目只在**没有其他幸存视图**提供同一
    (名, 作用域) 时才移除，且只移除与被处置视图贡献**完全一致**的条目——
    不同视图的相同贡献由它维持，陈旧的死亡提供方条目（本身即 INV-5 违规）
    不被掩盖。
    """
    removed = [disposed, *_descendants(pre, disposed)]
    removed_keys = {v.key for v in removed}
    kept = [v for v in successor.views if v.key not in removed_keys]
    still_provided = {(name, scope) for v in kept for (name, scope) in v.provides}
    own = {
        (name, scope, v.alive, v.state)
        for v in removed
        for (name, scope) in v.provides
    }
    store = {
        triple
        for triple in successor.g.store
        if not (
            triple in own and (triple[0], triple[1]) not in still_provided
        )
    }
    # 被移除视图的 accessor 条目随之失效（"最后一个 fiber 消失"分支）。
    props = set(successor.g.props)
    for view in removed:
        for name in view.accessors:
            props.discard((name, "accessor"))
    g1 = UGlobal(
        store=tuple(sorted(store)),
        dead_listeners=successor.g.dead_listeners,
        provider_seen=successor.g.provider_seen,
        props=tuple(sorted(props)),
        builtin_accessors=successor.g.builtin_accessors,
    )
    views = tuple(sorted(kept, key=lambda v: v.key))
    return UState(g1, views)


async def uverify(
    max_depth: int = 24,
    max_states: int = 20000,
    verbose: bool = True,
    bifurcate: bool = True,
    alphabet: Optional[Sequence[str]] = None,
) -> UProofResult:
    """无界归纳不变式验证主入口（只返回裁决；可达集用 ``_uverify_impl``）。

    BFS 在**存在集**层面构造可达集 S；每个 dispose 操作展开**两个**后继
    （被处置视图消失 / 仍存在）。闭包（frontier 为空）且不变式在 S 上全员
    成立 ⇒ 对任意 fiber 数、任意长度的操作序列成立（§6 定理）。

    ``bifurcate=False`` 关闭幸存二分（仅用于回归测试证明二分非空转）。
    """
    result, _visited = await _uverify_impl(
        max_depth, max_states, verbose, bifurcate, alphabet
    )
    return result


async def _uverify_impl(
    max_depth: int,
    max_states: int,
    verbose: bool,
    bifurcate: bool = True,
    alphabet: Optional[Sequence[str]] = None,
    bifurcated: Optional[Set[UState]] = None,
) -> Tuple[UProofResult, Dict[UState, List[str]]]:
    """存在集 BFS。``alphabet`` 缺省为结构层 ``UOPS_CORE``。

    ``bifurcated`` 非 None 时，幸存二分**派生**出的合成状态会被填进去。
    合成状态没有对应的具体路径（``visited`` 里存的是代表路径，它的后继是
    代表后继本身），故逐状态重放类验证（探针层）必须跳过它们——这一点
    曾经是一个真实的误报源。

    探针层操作（``UOPS_PROBE``）在投影上是自环，不进入展开——它们由
    ``run_probes`` 在每个可达状态上逐一执行并核对。
    """
    ops = list(alphabet) if alphabet is not None else list(UOPS_CORE)
    start = time.time()

    initial = uproject(await ureplay([]))
    bad = check_properties(initial)
    if bad:
        return (
            UProofResult(
                depth_reached=0,
                states=1,
                checked=1,
                closure_ok=False,
                counterexample=Counterexample([], bad, initial),
                elapsed=time.time() - start,
            ),
            {initial: []},
        )

    visited: Dict[UState, List[str]] = {initial: []}
    frontier: List[Tuple[List[str], UState]] = [([], initial)]
    checked = 1
    depth = 0
    closure_ok = False
    truncated = False

    while frontier and depth < max_depth:
        depth += 1
        next_frontier: List[Tuple[List[str], UState]] = []
        for path, state in frontier:
            for op in ops:
                new_path = path + [op]
                world = await ureplay(new_path)
                successor = uproject(world)
                checked += 1

                # 边性质（INV-1/3a/3b/10）：在迁移发生处判定，与状态无关
                edge_bad = _edge_violations(world)
                if edge_bad:
                    return (
                        UProofResult(
                            depth_reached=depth,
                            states=len(visited),
                            checked=checked,
                            closure_ok=False,
                            counterexample=Counterexample(
                                new_path, edge_bad, successor
                            ),
                            elapsed=time.time() - start,
                        ),
                        visited,
                    )
                bad = check_properties(successor)
                if bad:
                    return (
                        UProofResult(
                            depth_reached=depth,
                            states=len(visited),
                            checked=checked,
                            closure_ok=False,
                            counterexample=Counterexample(new_path, bad, successor),
                            elapsed=time.time() - start,
                        ),
                        visited,
                    )

                # 幸存二分：dispose 的后继按"被处置视图的计数"一分为二，
                # 两个分支都检查性质、都继续展开（存在集状态是具体状态的等价
                # 类，必须覆盖类内全部计数实例）。级联感知：分支连被处置视图
                # 的后代一起消去/恢复（U-6，父卸载级联子 fiber）。
                candidates = [successor]
                if bifurcate and op in _DISPOSE_OPS:
                    disposed = _dispose_target(state, op)
                    if disposed is not None:
                        if disposed not in successor.views:
                            # 代表实例计数=1 ⇒ 补幸存分支（计数≥2）
                            candidates.append(_bifurcate(successor, disposed, state))
                        else:
                            # 代表实例计数≥2 ⇒ 补"最后一个被处置"分支
                            other = _unbifurcate(successor, disposed, state)
                            if other != successor:
                                candidates.append(other)
                for cand in candidates[1:]:
                    bad2 = check_properties(cand)
                    if bad2:
                        return (
                            UProofResult(
                                depth_reached=depth,
                                states=len(visited),
                                checked=checked,
                                closure_ok=False,
                                counterexample=Counterexample(
                                    new_path,
                                    [("SURVIVOR/" + n, r) for n, r in bad2],
                                    cand,
                                ),
                                elapsed=time.time() - start,
                            ),
                            visited,
                        )

                for cand in candidates:
                    if cand not in visited:
                        visited[cand] = new_path
                        next_frontier.append((new_path, cand))
                        if cand is not successor and bifurcated is not None:
                            bifurcated.add(cand)
                if len(visited) >= max_states:
                    truncated = True
                    break
            if truncated:
                break
        frontier = next_frontier
        if verbose:
            print(
                f"  depth={depth} |S|={len(visited)} frontier={len(frontier)} "
                f"checked={checked} t={time.time() - start:.1f}s",
                flush=True,
            )
        if truncated:
            break
        if not frontier:
            closure_ok = True
            break

    return (
        UProofResult(
            depth_reached=depth,
            states=len(visited),
            checked=checked,
            closure_ok=closure_ok,
            truncated=truncated,
            elapsed=time.time() - start,
        ),
        visited,
    )


# ---------------------------------------------------------------------------
# §6 一致性检验：无界定理前提 H2/H3/H4 的机器检查
# ---------------------------------------------------------------------------
#
# 存在集投影把"同视图的多个 fiber"并成了一个抽象状态。定理要求这种合并
# 不改变行为：对每个可达状态，构造一个"多一份同视图 fiber"的**重复实例**
# （在代表路径后追加一次对应形态的加载），重放全部操作，要求：
#
#   1. 重复实例的投影 == 原状态（加载是"干净"的重复，没有改变存在集）；
#   2. 对每个操作，重复实例的后继 == 代表实例的后继，或（当操作处置的正是
#      被复制的视图时）== 后继 ∪ {该视图}；
#   3. 全局态完全一致（store / dead_listeners / props 不含计数信息）；
#   4. 两条路径的边性质都无违规。
#
# 1–4 全部通过 ⇒ 计数（1 vs 2）不影响抽象转移。计数 ≥3 的等价性由代码结构
# 事实保证：唯一的 fiber 计数读取是 ``not len(runtime.fibers)``（只测归零）。
#
# 第 2 条同时**直接验证幸存二分公式**：重复实例的 dispose 后继必须等于
# ``_bifurcate`` 的符号构造结果。


_LOAD_OF_SHAPE = {
    "provider": "load_provider",
    "consumer": "load_consumer",
    "effect": "load_effect",
    "isolated": "load_isolated",
    # 嵌套加载：重复实例是"多一份父子对"——存在集层面与既有父子对重合
    # （父视图 parent_key=None、子视图 parent_key=父 key，均确定），故可作为
    # 干净重复参与一致性检验；其级联语义由 _bifurcate 的 _descendants 覆盖。
    "parent": "load_nested",
}


def _stratified_sample(
    visited: Dict[UState, List[str]], cap: int
) -> List[Tuple[UState, List[str]]]:
    """分层抽样：结构性状态全保留 + 按路径长度均匀取样。

    "结构性状态"指含嵌套视图（depth ≥1，级联语义的载体）或含第七轮新增形态
    （accessor / mixin / child / parent）的状态——这些是语义最-rich 的点。
    其余状态按代表路径长度分层，每层均匀取一定比例，保证浅层（新形态首次
    出现的区域）与深层（饱和区域）都有覆盖。
    """
    items = list(visited.items())
    special: List[Tuple[UState, List[str]]] = []
    rest: List[Tuple[UState, List[str]]] = []
    for state, path in items:
        if any(v.depth >= 1 for v in state.views) or any(
            v.shape in ("accessor", "mixin", "child", "parent") for v in state.views
        ):
            special.append((state, path))
        else:
            rest.append((state, path))
    if len(special) >= cap:
        # 结构性状态已超配额：按路径长度排序后均匀取（保证深度覆盖）
        special.sort(key=lambda kv: len(kv[1]))
        step = max(1, len(special) // cap)
        return special[::step][:cap]
    remaining = cap - len(special)
    rest.sort(key=lambda kv: len(kv[1]))
    if len(rest) <= remaining:
        return special + rest
    step = max(1, len(rest) // remaining)
    return special + rest[::step][:remaining]


async def check_uniformity(
    visited: Dict[UState, List[str]],
    verbose: bool = True,
    sample: Optional[int] = None,
) -> List[str]:
    """对可达状态执行一致性检验，返回失败描述列表（空 = 通过）。

    检验内容（无界定理前提 H2/H3/H4）：

    1. 重复实例（代表路径 + 一次对应形态加载）的投影 == 原状态——加载是
       "干净"的重复；
    2. 对每个操作，重复实例的后继 == **期望后继**：
       - 操作处置的正是被复制视图（或其级联后代）、且代表实例该视图计数=1
         ⇒ 期望后继 = ``_bifurcate(代表后继, 被处置视图, 前态)``（幸存分支，
         公式与实测互证）；
       - 否则 ⇒ 期望后继 = 代表后继（计数不影响转移）；
    3. 两条路径的边性质都无违规。

    计数 ≥3 与 =2 的等价性由代码结构事实保证（唯一的 fiber 计数读取是
    ``not len(runtime.fibers)``，只测归零）。

    ``sample`` 非 None 时按**分层抽样**取至多这么多个状态：含嵌套视图的
    状态、含各加载形态的状态全部保留，其余按路径长度分层均匀取样。全量
    检查是 H2/H3 的完整机器证据（随字母表增长耗时线性上升，第七轮 23 元
    字母表下全量约需一小时级）；抽样是 CI 用的快速档，两者都只报告实测
    结果，不把抽样说成全量。
    """
    if sample is not None and len(visited) > sample:
        states = _stratified_sample(visited, sample)
    else:
        states = list(visited.items())
    failures: List[str] = []
    total = len(states)
    done = 0
    for state, path in states:
        done += 1
        if verbose and done % 25 == 0:
            print(f"  一致性检验 {done}/{total} …", flush=True)
        for shape, load_op in _LOAD_OF_SHAPE.items():
            dup_world = await ureplay(path + [load_op])
            dup_state = uproject(dup_world)
            if dup_state != state:
                continue  # 非干净重复（加载改变了存在集/全局态）
            extra = dup_world.last_load_fiber
            if extra is None:
                continue
            extra_view = _uview(extra, dup_world)
            if extra_view not in state.views:
                failures.append(
                    f"重复实例的额外视图不在原状态中：path={path} shape={shape}"
                )
                continue
            for op in UOPS:
                base_world = await ureplay(path + [op])
                base_state = uproject(base_world)
                dup2_world = await ureplay(path + [load_op] + [op])
                dup2_state = uproject(dup2_world)
                for label, w in (("代表", base_world), ("重复", dup2_world)):
                    for code, message in _edge_violations(w):
                        failures.append(
                            f"{label}实例 {code} 违规：path={path}+{op} {message}"
                        )
                # 期望后继：处置被复制视图（或其级联后代）且代表计数=1 时走
                # 幸存二分公式，其余情形计数不影响转移。注意 ``load_nested``
                # 的重复实例一次复制**两个**视图（父+子），故判定集合是被复制
                # 视图的 key 加上其全部后代 key——否则处置子视图时重复实例的
                # 计数差（2 vs 1）不被覆盖，期望后继会算错。
                disposed = (
                    _dispose_target(state, op) if op in _DISPOSE_OPS else None
                )
                dup_keys = {extra_view.key} | {
                    d.key for d in _descendants(state, extra_view)
                }
                if (
                    disposed is not None
                    and disposed.key in dup_keys
                    and disposed not in base_state.views
                ):
                    expected = _bifurcate(base_state, disposed, state)
                else:
                    expected = base_state
                if dup2_state != expected:
                    failures.append(
                        f"计数影响抽象转移：path={path} op={op} shape={shape}\n"
                        f"  期望  {sorted(v.key for v in expected.views)} | "
                        f"{sorted(expected.g.store)}\n"
                        f"  实测  {sorted(v.key for v in dup2_state.views)} | "
                        f"{sorted(dup2_state.g.store)}"
                    )
    return failures


# ---------------------------------------------------------------------------
# §6.9 探针层验证：值语义规约在每个可达状态上的覆盖
# ---------------------------------------------------------------------------
#
# 探针操作（``UOPS_PROBE``）只读或自限，在抽象投影上是**自环**：不创建 fiber、
# 不改全局态，只把可观测值写进边检查台账。它们不进 BFS 展开（状态数不增而
# 检查点数按探针数翻倍），覆盖由本函数给出：对**每个**可达状态执行全部探针，
#
#   1. 全部状态性质与边性质成立；
#   2. 投影后继 == 该状态本身（自环断言：探针确实没改变抽象状态——否则它
#      就是结构操作，必须挪进 ``UOPS_CORE`` 重跑闭包）。
#
# 与闭包证明的关系要说清：闭包义务只对结构层成立；探针层的正确性由"自环 +
# 性质成立"逐状态核对。二者合起来覆盖全部 33 个操作。


async def run_probes(
    visited: Dict[UState, List[str]],
    verbose: bool = True,
    sample: Optional[int] = None,
    bifurcated: Optional[Set[UState]] = None,
) -> List[str]:
    """对可达状态逐一执行探针操作，返回失败描述列表（空 = 通过）。

    ``bifurcated`` 是幸存二分**派生**的合成状态集合：它们没有对应的具体
    路径（``visited`` 存的是代表路径），逐状态重放无法复现，故跳过并计数
    ——跳过的是"由公式覆盖"的状态，不是"没验证"的行为。
    """
    derived = bifurcated or set()
    candidates = [
        (state, path) for state, path in visited.items() if state not in derived
    ]
    skipped = len(visited) - len(candidates)
    if verbose and skipped:
        print(f"  （跳过 {skipped} 个二分派生状态：由幸存二分公式覆盖）", flush=True)
    states = (
        _stratified_sample(dict(candidates), sample)
        if sample is not None and len(candidates) > sample
        else candidates
    )
    failures: List[str] = []
    total = len(states)
    done = 0
    for state, path in states:
        done += 1
        if verbose and done % 25 == 0:
            print(f"  探针层 {done}/{total} …", flush=True)
        for probe in UOPS_PROBE:
            world = await ureplay(path + [probe])
            for code, message in _edge_violations(world):
                failures.append(
                    f"探针 {probe} 在 path={path} 上 {code} 违规：{message}"
                )
            successor = uproject(world)
            bad = check_properties(successor)
            if bad:
                failures.append(f"探针 {probe} 在 path={path} 上性质违规：{bad}")
            if successor != state:
                failures.append(
                    f"探针 {probe} 在 path={path} 上不是投影自环：\n"
                    f"  期望 {sorted(v.key for v in state.views)} | {sorted(state.g.store)}\n"
                    f"  实测 {sorted(v.key for v in successor.views)} | {sorted(successor.g.store)}"
                )
    return failures


# ---------------------------------------------------------------------------
# §7 浸润测试：超长序列驱动全部边断言（无界尺度的经验证据）
# ---------------------------------------------------------------------------
#
# BFS 的存在集闭包给出**证明**；浸润测试在证明之外提供无界尺度的经验证据：
# 随机/结构化长序列（数百步、上百个 fiber）上，边性质与状态性质同样无违规。
# 它不构成证明——它验证的是"证明所依赖的边断言在大规模实例上同样成立"。


async def soak(seed: int = 0, steps: int = 240, rounds: int = 3) -> List[str]:
    """随机长序列浸润。"""
    rng = random.Random(seed)
    failures: List[str] = []
    for round_index in range(rounds):
        path = [rng.choice(UOPS) for _ in range(steps)]
        world = await ureplay(path)
        for code, message in _edge_violations(world):
            failures.append(
                f"浸润 seed={seed} round={round_index} {code}：{message}"
            )
        bad = check_properties(uproject(world))
        if bad:
            failures.append(
                f"浸润 seed={seed} round={round_index} 性质违规：{bad}"
            )
    return failures


async def soak_structured() -> List[str]:
    """结构化浸润：大批同形态插件加载后逐个卸载（LIFO / 级联的压力形态）。"""
    failures: List[str] = []
    patterns: List[Tuple[str, List[str]]] = [
        (
            "50 消费者 + 提供方，逐个逆序卸载",
            ["load_provider"] + ["load_consumer"] * 50 + ["dispose_vlast"] * 51,
        ),
        (
            "50 效果插件全部加载后逐一卸载",
            ["load_effect"] * 50 + ["dispose_vlast"] * 50,
        ),
        (
            "20 隔离提供方交替加载/卸载",
            (["load_isolated", "load_provider"] * 10)
            + ["dispose_vlast"] * 20
            + ["load_consumer"] * 5,
        ),
    ]
    for name, path in patterns:
        world = await ureplay(path)
        live = sum(1 for f in world.fibers() if f.uid is not None)
        for code, message in _edge_violations(world):
            failures.append(f"结构化浸润[{name}] {code}：{message}")
        bad = check_properties(uproject(world))
        if bad:
            failures.append(f"结构化浸润[{name}] 性质违规：{bad}")
        print(f"    {name}：{len(path)} 步，终态存活 fiber {live} 个，"
              f"{'无违规' if not failures else '有违规'}")
    return failures


# ---------------------------------------------------------------------------
# §8 变异测试：验证器灵敏度（含两条无界版新增变异）
# ---------------------------------------------------------------------------


class _Mutant(V._Mutant):
    """与 verify._Mutant 相同，仅复用其上下文管理器实现。"""


def _mut_notify_skips_dependent() -> Any:
    """缺陷：notify 级联遗漏最后一个依赖方 → INV-10 / INV-11。

    只替换 ``_cordis_notify_inner`` 槽里的**内层**实现（复制 notify 主体并
    把依赖方列表截断，模拟循环提前退出、索引不全），级联完备性断言（外层
    插桩）保持装配——否则变异会把断言一起绕掉。该缺陷同时是无界证明前提
    H5 的直接反例：级联不完整时，存在集探索只有一份代表实例，看不到未被
    刷新的大系统消费方。
    """

    def broken(
        self: Any,
        names: List[str],
        filter_fn: Any = None,
        scope_ctx: Any = None,
    ) -> Any:
        origin = scope_ctx if scope_ctx is not None else self.ctx
        if filter_fn is None:

            def filter_fn(ctx: Any, name: str, _origin: Any = origin) -> bool:  # type: ignore[misc]
                return _chain_isolate(ctx).get(name) == _chain_isolate(_origin).get(name)

        fibers: List[Any] = []
        for runtime in self.ctx.registry.values():
            for fiber in runtime.fibers:
                has_update = False
                for name in names:
                    if fiber.inject is None or name not in fiber.inject:
                        continue
                    if not filter_fn(fiber.ctx, name):
                        continue
                    has_update = True
                    fiber._check_impl(name)
                if has_update:
                    fibers.append(fiber)
        for fiber in fibers[:-1]:  # 缺陷：遗漏最后一个依赖方
            fiber._refresh()
        for name in names:
            impl = self._get_impl(name, False, origin)
            carrier = origin.extend(
                {symbols.filter: (lambda target, _name=name: filter_fn(target, _name))}
            )
            self.ctx.events.emit(carrier, "internal/service", name, impl.value if impl else None)
        return fibers[:-1]

    class _Patch:
        """替换 notify 内层实现的上下文管理器（外层断言不动）。"""

        def __init__(self) -> None:
            self._slot = ReflectService._cordis_notify_inner  # type: ignore[attr-defined]
            self._saved: Any = None

        def __enter__(self) -> "_Patch":
            self._saved = self._slot[0]
            self._slot[0] = broken
            return self

        def __exit__(self, *exc: Any) -> bool:
            self._slot[0] = self._saved
            return False

    return _Patch()


def _mut_stale_dep_cache() -> V._Mutant:
    """缺陷：``_check_impl`` 在提供方消失时不清除消费方缓存 → INV-11。"""

    original_check = fiber_mod.Fiber._check_impl

    def broken(self: Any, name: str) -> None:
        impl = self.ctx.reflect._get_impl(name, True, self.ctx)
        if impl is None:
            return  # 缺陷：不 pop，陈旧 impl 留在 _store 里
        if impl.check is not None:
            try:
                if not impl.check():
                    return
            except Exception as error:  # noqa: BLE001 - 转交提供方日志
                impl.fiber.ctx.logger.error(error)
                return
        self._store[name] = impl

    class _Patch(V._Mutant):
        def __init__(self) -> None:
            super().__init__(
                "依赖失效后不清除消费方缓存",
                fiber_mod.Fiber,
                "_check_impl",
                broken,
            )
            self._original = original_check

        def __enter__(self) -> "_Patch":
            setattr(self.target, self.attr, self.replacement)
            return self

        def __exit__(self, *exc: Any) -> bool:
            setattr(self.target, self.attr, self._original)
            return False

    return _Patch()


#: 变异清单：(名称, 工厂, 期望触发的性质编号)
#:
#: 前三条与 verify.py 共用（历史真实缺陷）；后两条是无界版新增的证明义务
#: 对应的缺陷——级联完备性（H5）与依赖失效传播（INV-11）。
# ---------------------------------------------------------------------------
# 第七轮新增变异：为 INV-12~INV-36 各自提供"注入缺陷能被抓住"的证据
# ---------------------------------------------------------------------------
#
# 判据与第五轮一致：每条变异必须**精确落在它声称检验的那条性质上**。其中六条
# 是本项目历史缺陷的重新注入（标注"历史缺陷 #N"），这比凭空构造的缺陷更有
# 说服力——它们曾真实存在于代码里。


def _mut_allow_duplicate_provide() -> V._Mutant:
    """缺陷：重复 provide 静默覆盖（INV-12，L-5）。

    注入点取 ``ReflectService.provide._setup`` 的唯一抛错分支——把"同名已
    注册"当成合法覆盖。它不会影响唤醒与逆序派发，因此只应被 INV-12 观测到。
    """
    original = reflect_mod.ReflectService.provide

    def allow_overwrite(self, name, value=None, check=None, caller=None):
        def _setup():
            scope = caller if caller is not None else self.ctx
            existing = self.props.get(name)
            if existing is None:
                self.props[name] = "service"
            root_isolate = getattr(self.ctx.root, symbols.isolate)
            if name not in _chain_isolate(scope) and root_isolate.get(name) is None:
                root_isolate[name] = name
            key = _chain_isolate(scope).get(name) or name
            impl = reflect_mod.Impl(name, scope.fiber, value, check)
            self.store[key] = impl  # 缺陷：直接覆盖，不再报错
            if scope.fiber.store is not None:
                scope.fiber.store[name] = impl

            async def _dispose() -> None:
                self.store.pop(key, None)
                for fiber in self.notify([name], scope_ctx=scope):
                    try:
                        await fiber.await_()
                    except Exception:  # noqa: BLE001
                        pass
                if scope.fiber.store is not None:
                    scope.fiber.store.pop(name, None)

            return _dispose

        return (caller or self.ctx).fiber.effect(_setup, f"ctx.provide({name!r})")

    return V._Mutant(
        "重复 provide 静默覆盖", reflect_mod.ReflectService, "provide", allow_overwrite
    )


def _mut_ignore_config_issues() -> V._Mutant:
    """缺陷：``resolve_config`` 忽略 ``issues``（INV-13，L-4）。"""
    def broken(runtime, config):
        # 不能先调原函数——它会先抛 ValidationError，缺陷就永远走不到。
        if getattr(runtime, "Config", None) is None:
            return config
        result = runtime.Config(config)
        if isinstance(result, dict) and "issues" in result and result["issues"]:
            return result.get("value", config)  # 缺陷：忽略 issues，不抛
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result

    return V._Mutant("配置校验 issues 被忽略", fiber_mod, "resolve_config", broken)


def _mut_tolerate_invalid_effect() -> V._Mutant:
    """缺陷：非法 effect 形态被当作"无可清理资源"接受（INV-14，L-8）。"""
    original = fiber_mod.is_nullable

    def broken(value):
        if value == 42:
            return True  # 缺陷：把 42 当成合法（无）effect
        return original(value)

    return V._Mutant("非法 effect 形态被接受", fiber_mod, "is_nullable", broken)


def _mut_skip_catch_chain() -> V._Mutant:
    """缺陷：``internal/plugin`` 观察者抛错后不驱动拆卸（INV-15，L-9）。

    历史缺陷 #1（fire-and-forget catch 链从未调度）的重新注入：把
    ``Fiber.__init__`` 里 emit 失败分支的 ``_drive_catch`` 去掉。
    """
    original = fiber_mod._drive_catch

    class _Patch(V._Mutant):
        def __init__(self) -> None:
            super().__init__(
                "观察者抛错后不驱动拆卸",
                fiber_mod,
                "_drive_catch",
                lambda dispose, report: None,
            )

    del original
    return _Patch()


def _mut_break_cascade() -> V._Mutant:
    """缺陷：子插件的 disposer 不挂在父 fiber 上（INV-16，U-6）。

    ``Fiber.__init__`` 用 ``parent.fiber.effect(self._make_disposer(), ...)``
    建立级联；把它注册到**根** fiber 上就切断了父子链——父卸载时子不会跟着走。
    """
    original = fiber_mod.Fiber.effect

    def broken_effect(self, execute, label="anonymous"):
        if label == "ctx.plugin()" and self.parent is not None:
            root_fiber = self.parent.root.fiber
            if root_fiber is not self:
                return original(root_fiber, execute, label)  # 缺陷：挂到根上
        return original(self, execute, label)

    return V._Mutant("子插件 disposer 不挂父 fiber", fiber_mod.Fiber, "effect", broken_effect)


def _mut_accessor_never_removed() -> V._Mutant:
    """缺陷：accessor 的 disposer 摘不掉 ``props`` 条目（INV-17，R-6/R-7）。

    注入点的选择踩过一次坑：``accessor`` 的**返回值**替换成空实现是无效的——
    框架从不调用它，卸载时走的是 effect 内部收集的 disposer。真正可判定的
    收口点是 ``props.pop``：accessor 的 disposer 正是 ``lambda: props.pop(name)``，
    让 ``pop`` 变成空操作即可让属性永久留在 props。
    """

    class _StickyProps(dict):
        """``pop`` 不真删——accessor 的 disposer 因此失效。"""

        def pop(self, key, *default):  # type: ignore[override]
            return default[0] if default else None

    original = ReflectService.accessor

    def broken_accessor(self, name, options, caller=None):
        if not isinstance(self.props, _StickyProps):
            self.props = _StickyProps(self.props)
        return original(self, name, options, caller)

    return V._Mutant(
        "accessor 永不从 props 摘除", ReflectService, "accessor", broken_accessor
    )


def _mut_ignore_isolation_filter() -> V._Mutant:
    """缺陷：``internal/service`` 事件不带作用域过滤载体（INV-22，E-5）。

    历史缺陷 #8 的重新注入：notify 用 ``carrier = origin.extend({symbols.filter:
    ...})`` 派发，``dispatch`` 据此只放行同作用域监听器；去掉 carrier 就等于
    取消隔离。注入点是 notify 的**内层实现**（外层的 INV-10 断言保持装配）。
    """
    original_notify = ReflectService.notify

    def broken(self, names, filter_fn=None, scope_ctx=None):
        origin = scope_ctx if scope_ctx is not None else self.ctx
        fibers = []
        for runtime in self.ctx.registry.values():
            for fiber in runtime.fibers:
                if fiber.inject is None:
                    continue
                if not any(name in fiber.inject for name in names):
                    continue
                for name in names:
                    fiber._check_impl(name)
                fiber._refresh()
                fibers.append(fiber)
        for name in names:
            impl = self._get_impl(name, False, origin)
            # 缺陷：直接派发，不带 symbols.filter 载体 ⇒ 跨作用域泄漏
            self.ctx.events.emit("internal/service", name, impl.value if impl else None)
        return fibers

    del original_notify

    class _Patch:
        """替换 notify 内层实现的上下文管理器（外层断言不动）。"""

        def __init__(self) -> None:
            self._slot = ReflectService._cordis_notify_inner  # type: ignore[attr-defined]
            self._saved: Any = None

        def __enter__(self) -> "_Patch":
            self._saved = self._slot[0]
            self._slot[0] = broken
            return self

        def __exit__(self, *exc: Any) -> bool:
            self._slot[0] = self._saved
            return False

    return _Patch()


def _mut_parallel_reports_emit() -> V._Mutant:
    """缺陷：``parallel()`` 上报 ``emit``（INV-29，E-2）。

    历史缺陷（参考实现的 parallel 派发诊断谎报）的重新注入。
    """
    original = reflect_mod.events_mod.EventsService.parallel if hasattr(
        reflect_mod, "events_mod"
    ) else None
    from cordis_py import events as events_mod

    async def broken(self, *args):
        arg_list = list(args)
        callbacks = self.dispatch("emit", arg_list)  # 缺陷：报 emit 而非 parallel
        import asyncio

        results = await asyncio.gather(
            *(self._invoke_async(cb, arg_list) for cb in callbacks),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise ExceptionGroup("parallel dispatch failed", errors)  # type: ignore[arg-type]

    del original
    return V._Mutant("parallel 上报 emit", events_mod.EventsService, "parallel", broken)


def _mut_plain_hook_map() -> V._Mutant:
    """缺陷：事件名按**属性**查（INV-31，E-4）。

    历史缺陷的重新注入：JS 的 ``hooks[name]`` 在 name 是 ``constructor`` /
    ``items`` / ``keys`` / ``values`` 时命中 ``Object.prototype`` 的成员，
    监听器挂错键、派发时取到方法而非列表。Python 的等价形态是
    ``getattr(store, name)``。

    注入点踩过两次坑：
    1. 把 ``_HookMap._store`` 换成裸 dict **无效**——``_HookMap`` 的保护来自
       "所有访问都经它的方法路由"，与 store 类型无关；
    2. 直接改 ``_HookMap.get`` **过宽**——本模型自己要注册怪事件名监听器
       （E-4 的观测点），世界会在构造期就崩掉，一条序列都跑不完。
    故改为：世界构造完成、监听器已登记之后，再把事件名映射整体换成"按属性
    查"的语义——等价于历史缺陷，且只影响后续的派发。
    """
    original_world_init = UWorld.__init__

    class _AttrHooks:
        """按属性查的映射（等价于命中 ``Object.prototype`` 成员）。"""

        __slots__ = ("_store",)

        def __init__(self, store: Dict[Any, Any]) -> None:
            self._store = store

        def get(self, key: Any, default: Any = None) -> Any:
            if key in _WEIRD_EVENT_NAMES:
                # 缺陷只针对"原型链成员名"——这正是历史缺陷的形态：
                # 普通事件名（internal/service 等）仍走正常查表，否则缺陷会
                # 先被 E-5 观测到，掩盖它对 E-4 的作用。
                return getattr(self._store, key, default)
            return self._store.get(key, default)

        def __getitem__(self, key: Any) -> Any:
            return self.get(key)

        def __setitem__(self, key: Any, value: Any) -> None:
            self._store[key] = value

        def __contains__(self, key: Any) -> bool:
            return key in self._store

        def items(self) -> Any:
            return self._store.items()

    def broken_init(self, *args, **kwargs):
        original_world_init(self, *args, **kwargs)
        self.root.events._hooks = _AttrHooks(self.root.events._hooks._store)

    return V._Mutant(
        "事件名按属性查（撞映射成员）", UWorld, "__init__", broken_init
    )


def _mut_update_returns_none() -> V._Mutant:
    """缺陷：``update`` 的非 ACTIVE 分支返回 ``None``（INV-35，E-9）。

    历史缺陷 #14 的重新注入：``await fiber.update(cfg)`` 在某些状态下直接崩。
    """
    original = fiber_mod._settle

    def broken_update(self, config, no_save=False):
        self.assert_active()
        self._config = config
        if self.state != fiber_mod.FiberState.ACTIVE:
            self._error = None
            self._set_epoch(fiber_mod.INACTIVE)
            self._refresh()
            return None  # 缺陷：不是 awaitable

        config = self._resolve_config(config)

        def _next():
            self.config = config
            self._error = None
            return self.restart()

        return self.context.waterfall(self, "internal/update", config, no_save, _next)

    del original
    return V._Mutant("update 非 ACTIVE 分支返回 None", fiber_mod.Fiber, "update", broken_update)


def _mut_clear_root_effects() -> V._Mutant:
    """缺陷：构造末尾清空根 fiber 的 disposer（INV-36，E-10）。

    历史缺陷 #4 的重新注入：``context.ts:82`` 的 ``_disposables.clear()``——
    内置注册项变成无人调用的孤儿，根 fiber 的 effect 元数据失真。

    注入点踩过一次坑：清在 ``Fiber.__init__`` 里**无效**——内置注册
    （ReflectService 的 4 次 mixin、LoggerService 的 provide 与 exporter）
    发生在 ``Fiber.__init__`` **返回之后**，清的是空列表。必须在
    ``Context.__init__`` 末尾清，才与历史缺陷等价。
    """
    from cordis_py.context import Context

    original = Context.__init__

    def broken_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.fiber._disposables.clear()  # 缺陷：根 fiber 的元数据被清空

    return V._Mutant("根 fiber effect 元数据被清空", Context, "__init__", broken_init)


UMUTATIONS: List[Tuple[str, Callable[[], V._Mutant], Optional[str]]] = [
    ("disposer 正序释放", V._mut_disposer_forward_order, "INV-3b"),
    ("provide 清理后留下孤儿条目", V._mut_store_orphan, "INV-5"),
    ("监听器不随 fiber 消亡", V._mut_listener_leak, "INV-6"),
    ("notify 级联遗漏依赖方", _mut_notify_skips_dependent, "INV-10"),
    ("依赖失效后不清除消费方缓存", _mut_stale_dep_cache, "INV-11"),
    ("重复 provide 静默覆盖", _mut_allow_duplicate_provide, "INV-12"),
    ("配置校验 issues 被忽略", _mut_ignore_config_issues, "INV-13"),
    ("非法 effect 形态被接受", _mut_tolerate_invalid_effect, "INV-14"),
    ("观察者抛错后不驱动拆卸", _mut_skip_catch_chain, "INV-15"),
    ("子插件 disposer 不挂父 fiber", _mut_break_cascade, "INV-16"),
    ("accessor 永不从 props 摘除", _mut_accessor_never_removed, "INV-17"),
    ("派发忽略隔离过滤器", _mut_ignore_isolation_filter, "INV-22"),
    ("parallel 上报 emit", _mut_parallel_reports_emit, "INV-29"),
    ("事件名按属性查（撞映射成员）", _mut_plain_hook_map, "INV-31"),
    ("update 非 ACTIVE 分支返回 None", _mut_update_returns_none, "INV-35"),
    ("根 fiber effect 元数据被清空", _mut_clear_root_effects, "INV-36"),
    # 以下两条刻意不计分：量化性质集边界（见 docs/FORMAL_VERIFICATION.md）
    ("bail 判据放行 NaN", V._mut_is_bailed_nan, None),
    ("PENDING 被误判为 ACTIVE", V._mut_state_never_pending, None),
]


# ---------------------------------------------------------------------------
# §8.5 变异试验台：每条变异跑「短 BFS（完整结构层）+ 探针层」
# ---------------------------------------------------------------------------
#
# 探针层的性质（INV-18~21、INV-23~36 多数）只在探针执行时判定，故变异检验
# 必须**两层都跑**：BFS 没抓到反例时继续跑探针层，把失败消息里的性质编号
# 也算作"被捕获"。返回 (被触发的性质集合, 是否捕获到反例)。


def _properties_in(lines: List[str]) -> Set[str]:
    """从失败描述里抽出全部性质编号（``SURVIVOR/INV-17`` 这类前缀也归一）。"""
    found: Set[str] = set()
    for line in lines:
        index = line.find("INV-")
        while index >= 0:
            end = index + 4
            while end < len(line) and (line[end].isdigit() or line[end] == "-"):
                end += 1
            found.add(line[index:end])
            index = line.find("INV-", end)
    return found


async def run_mutation(
    factory: Callable[[], Any],
    max_depth: int = 4,
    max_states: int = 400,
) -> Tuple[Set[str], bool]:
    """在给定缺陷注入下跑短 BFS + 探针层，返回 (触发的性质, 是否有反例)。

    默认预算（depth 4 / 400 状态）经实测取"够用即止"：16 条域内变异在该预算
    下**全部**被捕获且性质编号全对，总耗时约 81 s。更大的预算不会提高灵敏度，
    只增加 CI 时间。
    """
    with factory():
        derived: Set[UState] = set()
        result, visited = await _uverify_impl(
            max_depth, max_states, False, True, UOPS_CORE, derived
        )
        caught: Set[str] = set()
        found = result.counterexample is not None
        if found:
            for name, _ in result.counterexample.violations:
                caught.add(name.split("/")[-1])
            return caught, True
        if visited:
            failures = await run_probes(
                visited, verbose=False, bifurcated=derived
            )
            caught |= _properties_in(failures)
        return caught, bool(caught)


# ---------------------------------------------------------------------------
# §9 CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="cordis_py 无界形式化验证（不设插件数上限的归纳不变式证明）"
    )
    parser.add_argument("--max-depth", type=int, default=24)
    parser.add_argument("--max-states", type=int, default=20000)
    parser.add_argument("--no-mutate", action="store_true", help="跳过变异测试")
    parser.add_argument(
        "--no-uniformity", action="store_true", help="跳过一致性检验（H2/H3/H4）"
    )
    parser.add_argument("--no-soak", action="store_true", help="跳过浸润测试")
    parser.add_argument(
        "--no-probe", action="store_true", help="跳过探针层（值语义规约）验证"
    )
    parser.add_argument(
        "--probe-sample",
        type=int,
        default=None,
        help="探针层抽样上限（缺省 = 全量；抽样结果如实标注为抽样）",
    )
    parser.add_argument(
        "--alphabet",
        choices=("core", "tier1", "l5"),
        default="core",
        help="结构层字母表：core=23 元（最强结论，算力小时级）/ "
        "tier1=18 元（含嵌套加载与规约错误路径，266-576 s）/ "
        "l5=13 元（回归快档，约 2 s）",
    )
    parser.add_argument(
        "--uniformity-sample",
        type=int,
        default=250,
        help="一致性检验抽样上限（缺省 250；全量需显式传 --uniformity-sample 0）",
    )
    args = parser.parse_args(argv)

    print("=" * 72)
    print("cordis_py 形式化正确性验证 —— 无界归纳不变式（插件数不设上限）")
    print("=" * 72)
    print(f"状态性质：{', '.join(PROPERTIES)}")
    print("边性质：INV-1（合法迁移）, INV-3a（disposer 至多一次）, "
          "INV-3b（逆序派发）, INV-10（notify 级联完备）, "
          "INV-12（重复 provide 必拒）, INV-13（配置校验失败必拒）, "
          "INV-14（非法 effect 必拒）, INV-15（观察者抛错后完整拆卸）, "
          "INV-16（父卸载级联子 fiber）, INV-18（accessor 每次读取重求值）, "
          "INV-19（mixin 成员值）, INV-20（隔离作用域可注册 effect）, "
          "INV-21（in 覆盖内置与已注册）, INV-22（服务事件按名隔离）, "
          "INV-23（加载后停在稳定态）, INV-24（服务 self.ctx 随调用方重绑）, "
          "INV-25（异步 effect 注册即执行）, INV-26（服务解析到实现值）, "
          "INV-27（未声明 inject 必拒）, INV-28（get 按调用方作用域）, "
          "INV-29（派发诊断如实）, INV-30（waterfall 续体恰好一次）, "
          "INV-31（怪事件名不撞成员）, INV-32（logger 归属与 exporter 所有权）, "
          "INV-33（cause 错误链完整）, INV-34（多级 intercept 按 key 合并）, "
          "INV-35（update 任何状态可 await / 已释放明确拒绝）, "
          "INV-36（根 fiber effect 元数据真实）")
    alphabet_ops = {
        "core": UOPS_CORE,
        "tier1": UOPS_TIER1,
        "l5": UOPS_L5,
    }[args.alphabet]
    print(f"结构层字母表（{len(alphabet_ops)} 元 = --alphabet {args.alphabet}，"
          f"闭包证明用，按互异视图定向）：{', '.join(alphabet_ops)}")
    print(f"探针层字母表（{len(UOPS_PROBE)} 元，投影自环，逐状态核对）："
          f"{', '.join(UOPS_PROBE)}")
    print()

    print("[1/5] 证明义务：存在集 BFS（结构层）+ 幸存二分")
    bifurcated: Set[Any] = set()
    result, visited = asyncio.run(
        _uverify_impl(
            max_depth=args.max_depth,
            max_states=args.max_states,
            verbose=True,
            alphabet=alphabet_ops,
            bifurcated=bifurcated,
        )
    )
    print(
        f"  深度={result.depth_reached} 可达存在集={result.states} "
        f"检查点={result.checked} 封闭={result.closure_ok} "
        f"耗时={result.elapsed:.1f}s"
    )
    print(f"  裁决：{result.verdict()}")
    if result.closure_ok:
        print("  证明范围：任意长度、任意时刻存活插件数（无上界）")
    if result.counterexample is not None:
        print()
        print(result.counterexample.render())

    exit_code = 1 if result.counterexample is not None else 0

    if not args.no_probe and result.counterexample is None:
        print()
        print("[2/5] 探针层：值语义规约在每个可达状态上的核对（含投影自环断言）")
        probe_failures = asyncio.run(
            run_probes(
                visited,
                verbose=True,
                sample=args.probe_sample,
                bifurcated=bifurcated,
            )
        )
        derived_count = len(bifurcated)
        sampled = (
            args.probe_sample is not None and len(visited) > args.probe_sample
        )
        scope = (
            f"分层抽样的 {min(len(visited), args.probe_sample)} 个"
            if sampled
            else f"全部 {len(visited) - derived_count} 个"
        )
        if probe_failures:
            print(f"  ✗ {len(probe_failures)} 项违规")
            for line in probe_failures[:5]:
                print(f"    {line}")
            exit_code = 1
        else:
            print(
                f"  ✓ {scope}可达状态 × {len(UOPS_PROBE)} 个探针全部通过"
                f"（性质成立 + 投影自环）"
                + (
                    f"；另有 {derived_count} 个二分派生状态由公式覆盖（无具体路径，"
                    "不可重放核对）"
                    if derived_count
                    else ""
                )
                + ("（抽样，非全量）" if sampled else "")
            )

    if not args.no_uniformity and result.counterexample is None:
        print()
        print("[3/5] 一致性检验（定理前提 H2/H3/H4：计数不影响抽象转移）")
        # uverify 是纯函数式的，这里不能假设它缓存了什么——一致性检验需要
        # 可达集，故由调用方在上方一并收集（见 main 开头的 visited 传入）。
        failures = asyncio.run(
            check_uniformity(
                visited,
                sample=args.uniformity_sample or None,
            )
        )
        if failures:
            print(f"  ✗ {len(failures)} 项不一致——存在集投影的前提被破坏")
            for line in failures[:5]:
                print(f"    {line}")
            exit_code = 1
        else:
            checked = (
                len(visited)
                if not args.uniformity_sample or len(visited) <= args.uniformity_sample
                else args.uniformity_sample
            )
            print(f"  ✓ {checked} 个可达状态通过"
                  "（1 vs 2 副本的后继完全一致，幸存二分公式与实测相符）"
                  + ("" if checked == len(visited) else "（分层抽样，非全量）"))

    if not args.no_soak and result.counterexample is None:
        print()
        print("[4/5] 浸润测试（超长序列驱动全部边断言，无界尺度经验证据）")
        soak_failures = asyncio.run(soak())
        soak_failures += asyncio.run(soak_structured())
        if soak_failures:
            print(f"  ✗ {len(soak_failures)} 项违规")
            for line in soak_failures[:5]:
                print(f"    {line}")
            exit_code = 1
        else:
            print("  ✓ 随机 3×240 步 + 3 组结构化长序列均无违规")

    if not args.no_mutate:
        print()
        print("[5/5] 变异测试（验证器灵敏度）")
        print("  指标只统计『性质集覆盖范围内』的变异（期望性质 ≠ None）。")
        caught = 0
        in_scope = 0
        out_of_scope: List[str] = []
        for name, factory, expected in UMUTATIONS:
            caught_set, found = asyncio.run(run_mutation(factory))
            detail = ",".join(sorted(caught_set)) if found else "未捕获"
            if expected is None:
                mark = "✓ 捕获(域外)" if found else "· 域外"
                if not found:
                    out_of_scope.append(name)
                print(f"  {mark}  {name:<28} 触发性质：{detail}")
                continue
            in_scope += 1
            mark = "✓ 捕获" if found else "✗ 漏检"
            print(f"  {mark}  {name:<28} 触发性质：{detail}"
                  + ("" if found else f"（期望 {expected}）"))
            caught += int(found)
        print()
        print(f"  灵敏度（域内）：{caught}/{in_scope}"
              + ("" if caught == in_scope else "  ← 存在漏检，验证器强度不足"))
        if out_of_scope:
            print(f"  覆盖范围外的已知缺陷：{len(out_of_scope)} 项"
                  "（不被现有性质集观测，量化性质集边界而非验证器失败）")
        if caught < in_scope:
            exit_code = 1

    print()
    print("退出码：0 = 无反例、探针层通过、一致性通过、浸润无违规、域内变异全捕获；1 = 否则")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
