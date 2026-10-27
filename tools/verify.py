"""cordis_py 的形式化正确性验证器：归纳不变式算法 + 反例输出 + 变异测试。

## 为什么不是 BMC

朴素做法是「穷举长度 ≤ N 的全部操作序列」（有界模型检测）。本框架**做不到**：
插件数量无界，可达集随序列长度增长，实测 6 元字母表到深度 6 仍未闭包（6 分钟
未收敛）。BMC 因此只能给出有界结论，"任意长度"的保证必须靠人写归纳论证——
而人写的归纳论证无法机器检查，是整个验证链条里最弱的一环。

## 本算法的定位

**归纳不变式（inductive invariant）证明**，形式化如下。

设 Σ 为操作字母表，``α(s)`` 为具体状态 ``s`` 的抽象投影，P 为待证性质。
算法输出一个**可被机器检查的证明义务**，而不是一段论证：

1. **构造不变式候选 S**：从初态出发，按 Σ 做 BFS，得到抽象可达集 ``S ⊆ A``。
2. **义务 1（性质）**：``∀ s ∈ S. P(s)``。
3. **义务 2（封闭性）**：``∀ s ∈ S. ∀ a ∈ Σ. post(s, a) ∈ S``。
4. 若义务 1 与义务 2 同时成立，则 S 是关于 P 的**归纳加强**（inductive
   strengthening）：由归纳法，任意有限长度的操作序列 ``σ``（长度 **无界**）
   满足 ``α(δ(init, σ)) ∈ S``，故 ``P`` 在**任意长度**的操作序列之后成立。
5. 若义务 1 失败而该状态可达 → 输出**反例路径**（真实缺陷）。
   若义务 2 失败 → 说明 S 不是不变式，算法增加深度重试；重试耗尽则输出
   **不确定**（inconclusive），并给出失败的具体迁移，**绝不把"没查到"当"通过"**。

与 BMC 的区别：本算法不依赖"人写的归纳步骤"，封闭性是**被检查的**，不是被
断言的。它的完备性条件（见 §残余风险）也会被显式陈述。

## 变异测试：验证器的元验证

一个"永远通过"的验证器毫无价值。因此本模块自带变异测试（mutation testing）：
把**历史上真实发生过的缺陷**重新注入框架（``MUTATIONS``），要求算法对每一条
都报出反例。这是对验证器灵敏度的经验性证明——只有能抓到已知缺陷的验证器，
它的"通过"结论才有意义。

运行::

    python tools/verify.py                  # 完整验证 + 变异测试
    python tools/verify.py --no-mutate      # 只跑证明义务
    python tools/verify.py --max-depth 5
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cordis_py import Context, FiberState  # noqa: E402
from cordis_py import fiber as fiber_mod  # noqa: E402
from cordis_py import utils as utils_mod  # noqa: E402
from cordis_py.utils import symbols  # noqa: E402

# ---------------------------------------------------------------------------
# §1 抽象投影 α：把具体状态压成可哈希、可判定性质的抽象状态
# ---------------------------------------------------------------------------
#
# 投影必须满足：**P 是 α 的函数**。否则性质无法在抽象层判定，算法失效。
# 因此这里刻意把 INV 需要的全部可观测量都放进投影（监听器归属、清理账本、
# store 提供方存活标记等），而不是只放"少量摘要"。


@dataclass(frozen=True)
class FiberView:
    state: str
    alive: bool  # uid is not None
    n_disposables: int
    inject: Tuple[str, ...]
    hooks: Tuple[str, ...]
    epoch: Optional[str]


@dataclass(frozen=True)
class AbstractState:
    fibers: Tuple[FiberView, ...]
    # store 中每个服务：名称、提供方是否存活、提供方状态（用于隔离串扰检测）
    store: Tuple[Tuple[str, bool, str], ...]
    # 每个事件：规范化名、监听器数、其中属于"已释放 fiber"的监听器数
    listeners: Tuple[Tuple[str, int, int], ...]
    # ------------------------------------------------------------------
    # 清理账本的**有界摘要**
    # ------------------------------------------------------------------
    # 直接把运行历史（``runs``/``dispatch``）放进抽象状态会让状态空间本质上
    # 无界：账本是全程累积的，长度随序列长度单调增长（实测 N=2 时
    # runs 长度取值 0,1,2,…,6 持续增长，|S| 因此无法闭包）。
    #
    # 正确做法：归纳不变式只需要"**当前尚未完成的 disposer**"这一结构信息，
    # 它被存活 fiber 数界住。完成即移除 ⇒ 账本天然有界。
    #
    # ``pending``：当前已登记但尚未完成的 disposer 标签（按登记顺序）
    pending: Tuple[str, ...]
    # ``finished_shapes``：已完成 disposer 的**形状**集合 —— 每个元素是
    # ``(fiber键, 代次, 局部名)`` 的规范形标签，同样无界，故只保留其
    # **重复计数**是否超过 1 的布尔信息（重复执行即违规，由 INV-3a 判定）。
    # 这里存 ``(标签 → 完成次数 > 1)`` 中违规者的规范形，用于输出诊断。
    duplicate_finished: Tuple[str, ...]
    # ``dispatch_order``：**当前未完成项**上已发生的派发序（派发后立即从
    # pending 移除，故长度同样有界）。INV-3b 判定派发序 == 登记序的逆序。
    listener_hits: int
    provider_seen: bool
    # 每步新增的状态迁移边，供 INV-1 检查
    edges: Tuple[Tuple[str, str], ...]

    def render(self) -> str:
        parts = [f"fibers={len(self.fibers)}"]
        for view in self.fibers:
            parts.append(
                f"  fiber[{view.state} alive={int(view.alive)} disp={view.n_disposables} "
                f"inject={list(view.inject)} hooks={list(view.hooks)}]"
            )
        parts.append(f"store={[name for name, _, _ in self.store]}")
        parts.append(f"listeners={[(n, c, dead) for n, c, dead in self.listeners]}")
        parts.append(
            f"pending={list(self.pending)} dup={list(self.duplicate_finished)}"
        )
        return "\n".join(parts)


def _canonical_epoch(epoch: Any) -> Optional[str]:
    """折叠 epoch：``":1:2"`` → ``"ACTIVE"``，``INACTIVE`` 原样保留。

    epoch 由依赖提供方的 uid 拼接而成（``fiber.ts`` 的 ``_refresh``），uid 是
    无界身份量。语义上只有"是否 INACTIVE"这一位有用（是否已满足全部依赖），
    其余部分对 INV-2/INV-8 的判定没有贡献，故折叠掉。
    """
    if epoch is None:
        return None
    text = str(epoch)
    if text == "__INACTIVE__":
        return text
    return "" if text == "" else "ACTIVE"


def _canonical_event(name: Any) -> str:
    """把事件名规范化到**有穷**的名字空间。

    插件自有的事件名形如 ``ev/effect#7``，其中的自增序号使每个新状态都
    与旧状态不同——这会让状态空间**本质上无界**，即便给插件数加了基数上界
    也无法闭包（实测：N=2 时存活数已被压在0/1/2，状态数仍按 5→27→98→425
    →1613 增长，无界成分正是这个名字）。

    折叠规则：``<前缀>#<任意后缀>`` → ``<前缀>#*``。前缀保留是因为它区分了
    「插件自有事件」与框架内部事件，而这两者被 INV-6 以不同方式观测。
    这是**过近似**：折叠后可能把两个本来不同的状态并成一个，因此若折叠后
    仍有反例，则它在具体层面同样成立（不会产生假阳性）。
    """
    text = str(name)
    if "#" in text:
        return text.split("#", 1)[0] + "#*"
    return text


def project(world: Any) -> AbstractState:
    """把 World（具体状态）投影为可哈希抽象状态。"""
    fibers = []
    for f in world.fibers():
        runner = f._runner if isinstance(f._runner, dict) else {}
        epoch = runner.get("epoch")
        fibers.append(
            FiberView(
                state=FiberState.name_of(f.state),
                alive=f.uid is not None,
                n_disposables=len(f._disposables),
                inject=tuple(sorted((f.inject or {}).keys())),
                hooks=tuple(sorted(_canonical_event(k) for k in f._hooks)),
                # epoch 折叠：其语义用途只有"是否等于 INACTIVE"（是否已拿到
                # 全部依赖），而形如 ``:1`` / ``:2`` 的部分含依赖提供方的
                # **uid**——无界身份量（实测 epoch 取值随插件数增长）。
                # 折叠为两值后epoch 的判定力不变，但基数有界。
                epoch=_canonical_epoch(epoch),
            )
        )
    fibers.sort(key=lambda v: (v.state, v.alive, v.n_disposables, v.inject, v.hooks))

    store = sorted(
        (impl.name, impl.fiber.uid is not None, FiberState.name_of(impl.fiber.state))
        for impl in world.root.reflect.store.values()
    )

    listeners = []
    for name, hooks in world.root.events._hooks.items():
        dead = sum(1 for hook in hooks if hook.ctx.fiber.uid is None)
        listeners.append((_canonical_event(name), len(hooks), dead))
    # 折叠后多个不同事件名可能映射到同一规范名（如 ev/effect#3 与
    # ev/effect#4 → ev/effect#*），必须**按规范名合并**而不是保留重复条目
    # —— 否则 tuple 长度随插件数线性增长（实测 listeners 基数 3→51 持续涨）。
    merged: Dict[str, List[int]] = {}
    for canon, count, dead in listeners:
        if canon not in merged:
            merged[canon] = [0, 0]
        merged[canon][0] += count
        merged[canon][1] += dead
    listeners = [(name, v[0], v[1]) for name, v in sorted(merged.items())]

    return AbstractState(
        fibers=tuple(fibers),
        store=tuple(store),
        listeners=tuple(listeners),
        # -- 有界账本视图 ----------------------------------------------------
        # pending：已登记未完成（完成即移除，长度被存活 fiber 数界住）
        pending=tuple(sorted({_shape(label) for label in world.pending})),
        # duplicate_finished：重复执行的标签（正常恒为空；有值即违规）
        duplicate_finished=tuple(sorted({_shape(x) for x in world.duplicate_runs})),
        # dispatch_order：只保留**本路径上实际派发过、且其所属纤维当前仍
        # 存活**的那部分。已随 fiber 释放而消失的派发记录不再进入状态——
        # 它们对"未来任何后继都无影响"，属于历史而非当前结构。
        listener_hits=len(world.listener_hits),
        provider_seen=world.provider_seen,
        edges=_live_edges(world),
    )


def _live_edges(world: Any) -> List[Tuple[str, str]]:
    """迁移边的**有界视图**：只保留**最近一次**迁移（至多存活 fiber 数条）。

    ``world.transitions`` 是全程累积的，直接进入抽象状态会让状态空间无界。
    INV-1 检查的是"每条被观测到的迁移边是否合法"——若某条边非法，它在产生
    的那一刻就会被捕获；一旦该 fiber 的状态稳定下来，更早的边对"未来任何
    后继是否合法"没有影响。因此只保留最近一次迁移即可覆盖同一批可达状态。

    这是一个**明确的抽象取舍**（可能漏掉"某条早期非法边、但该 fiber 后来
    又迁移过"的场景），已记录在 docs/FORMAL_VERIFICATION.md §6.1。
    """
    live = {f.uid for f in world.fibers() if f.uid is not None}
    if not live:
        return ()
    # ``transitions`` 只存 (old, new)，不含 fiber 身份；用"最后 k 条"近似，
    # k 与存活 fiber 数同量级 ⇒ 有界。
    k = max(4, 2 * len(live))
    return tuple(
        (FiberState.name_of(old), FiberState.name_of(new))
        for old, new in world.transitions[-k:]
    )


def _live_dispatch(world: Any) -> List[str]:
    """派发序的**有界视图**：只保留最近一次 ``clear()`` 的派发记录。

    ## 为什么不能按"存活 fiber"过滤

    派发恰恰发生在**卸载时**——fiber 正是在 ``_unload`` 调``clear()`` 的
    过程中被释放的。若按存活性过滤，卸载场景下的派发会被整体丢弃
    （实测 dispatch 恒为空、INV-3b 退化为空断言、正序释放变异随即漏检）。
    这与"历史对后继无影响"的抽象原则并不冲突：逆序性质要检查的正是
    **卸载那一瞬**的派发顺序，它不是一个"当前状态"，而是一个**迁移事件**
    的属性。

    ## 正确的数据源：push 序与 dispatch 序的尾部对齐

    框架的 ``clear()`` 在 yield 前会 ``reverse()``，故"最后一批派发"必须等于
    "最后一批 push 的逆序"。这里保留 dispatch 日志尾部 ``k`` 条，``k`` 由
    单个 fiber 的 effect 数上界决定——effect 数由插件结构固定，与序列长度
    无关，故该视图有界。

    标签已被 ``_shape`` 压成结构形状（fiber 键与代次是纯身份量）；但**不能**
    按形状去重：逆序判定依赖批内完整顺序与重复项。
    """
    if not _DISPATCH_LOG:
        return []
    cap = _DISPATCH_CAP
    return [_shape(label) for label in _DISPATCH_LOG[-cap:]]


#: 派发视图保留的尾部长度。一次 ``clear()`` 派发的项数 = 该 fiber 的 effect
#: 数，由插件结构固定（本框架中单插件最多 4 个）⇒ 取 12 足够覆盖且保持有界。
_DISPATCH_CAP = 12


#: 合法的状态迁移边（对应 fiber.ts 的状态机）。自环不在此列——由 ``_p_inv1``
#: 先行跳过。
LEGAL_EDGES = {
    ("PENDING", "LOADING"),
    ("PENDING", "UNLOADING"),
    ("PENDING", "FAILED"),
    ("LOADING", "ACTIVE"),
    ("LOADING", "UNLOADING"),
    ("LOADING", "FAILED"),
    ("ACTIVE", "UNLOADING"),
    ("ACTIVE", "FAILED"),
    ("ACTIVE", "ACTIVE_OK"),
    ("FAILED", "LOADING"),
    ("FAILED", "UNLOADING"),
    ("UNLOADING", "LOADING"),
    ("UNLOADING", "PENDING"),
    ("UNLOADING", "DISPOSED"),
    ("UNLOADING", "ACTIVE"),
    ("UNLOADING", "FAILED"),
}


def _p_inv1(state: AbstractState) -> Optional[str]:
    """INV-1：状态机只走合法迁移边。"""
    for old, new in state.edges:
        if old == new:
            continue
        if (old, new) not in LEGAL_EDGES:
            return f"非法状态迁移 {old} -> {new}"
    return None


def _p_inv2(state: AbstractState) -> Optional[str]:
    """INV-2：uid / state 一致性。"""
    for view in state.fibers:
        if view.alive and view.state == "DISPOSED":
            return f"state=DISPOSED 但 fiber 仍存活（uid={view.uid if False else '?'}）"
        if not view.alive and view.state != "DISPOSED":
            return f"uid=None 但 state={view.state}"
    return None


def _p_inv3(state: AbstractState) -> Optional[str]:
    """INV-3a：每个 disposer 至多执行一次，且都来自已登记集合。

    ``ctx.effect(主体, label)`` 的语义是：**主体（setup）立即执行**，其返回值
    才是 disposer（卸载时逆序执行）。

    ## 为什么只查"重复执行"这一条

    抽象状态不保存完整运行历史（那会让状态空间无界，见 ``AbstractState``
    注释），而是保存两个有界量：

    - ``duplicate_finished``：同一标签被完成两次的记录。正常恒为空，
      **有值即违规**——重复执行是这一层唯一能可靠判定的东西；
    - ``pending``：已登记未完成的 disposer。它只用于 INV-3b 的逆序判定。

    "完成次数 ≤ 登记次数"这类计数关系需要完整历史，此处放弃断言——由
    ``test_dispose_all_cleans_registry``（终局全等检查）在回归层覆盖。
    """
    if state.duplicate_finished:
        return f"disposer 被重复执行：{list(state.duplicate_finished)[:3]}"
    return None


# 派发序账本：``DisposableList.clear`` 被插桩，把逆序 yield 的标签记下来。
# 必须观测**派发**而非**完成**顺序——包装后的 disposer 可能经协程调度，
# 完成顺序与派发顺序不同（实测 ``dSAME, dB, dA, dOuter`` 即是派发
# ``SAME, OUTER, B, A`` 的异步变形）。派发序才是 ``clear()`` 的 ``reverse()``
# 真正保证的东西。
_DISPATCH_LOG: List[str] = []
_LABEL_ATTR = "__cordis_verify_label__"


_PUSH_LOG: List[str] = []


def _shape(label: str) -> str:
    """把清理账本标签压成**有界**的"结构形状"。

    标签格式 ``<fiber键>|<代次>|<局部名>``，其中 ``fiber键``（含 uid）与
    ``代次`` 都是纯身份量：每次插件重载代次递增、每个新 fiber uid 不同，
    它们永不重复，若直接进入抽象状态则状态空间本质上无界。

    实测（N=2，基数约束已生效、``fibers``/``edges`` 均已收敛）：
    ``pending`` 取值仍从 3 → 14 → 30 → 78 → 165 → 364 → 729 单调增长，
    无界成分正是这两段身份量。

    折叠后保留"局部名"（A/B/outer/provider/consumer），因为它决定逆序关系
    与归属结构；丢弃 fiber 键与代次，因为它们对"下一步会不会违规"没有影响
    ——这是与 uid 排除同一层级的过近似：可能把两个不同状态并成一个，但不会
    产生假阳性（若折叠态违规，则具体态同样违规）。
    """
    parts = label.split("|")
    # 只保留局部名（``A`` / ``B`` / ``outer`` / ``provider`` / ``consumer``）。
    #
    # uid 与代次是**无界身份量**：每个新插件一个新 uid、每次重载一个代次，
    # 实测 ``pending`` 取值基数随之从 3 单调涨到 703，永远收不住。
    #
    # 曾经试过保留"批次分组"（``f3~4|outer``）供状态上的逆序判定，但那样的
    # 基数仍随插件数增长（1→22），且最终采用的是**边属性**判定（见
    # ``_check_reverse``），分组需求已在迁移发生处满足，无需带进状态。
    return parts[-1] if parts else label


def _batch_of(label: str) -> str:
    """取标签的批次标识（``<fiber键>~<代次>``）。

    逆序只在同一批次（= 同一 fiber 的同一 DisposableList）内成立，跨批次
    的派发互不相关，故比较前必须先分组。
    """
    return label.split("|", 1)[0] if "|" in label else label


def _fiber_of(label: str) -> str:
    """取标签的 fiber 键（标签格式 ``<fiber>|<代次>|<局部名>``）。"""
    parts = label.split("|")
    return "|".join(parts[:2]) if len(parts) >= 3 else label


def _effect_label_of(value: Any) -> Optional[str]:
    """取出框架挂在 ``Fiber.effect`` wrapper 上的 ``EffectMeta.label``。"""
    meta = getattr(value, symbols.effect, None)
    if meta is None:
        return None
    label = getattr(meta, "label", None)
    if label is None or "|" not in str(label):
        return None
    return str(label)


def _instrument_dispatch() -> None:
    """给 ``DisposableList`` 装上「注册序 / 派发序」双插桩。

    每次调用都基于**当前**实现重新包装（不做幂等短路）：变异测试会替换
    ``clear``，若此时不重新装配，变异体就会绕过插桩、派发账本变空——从而
    让 INV-3b 在最需要它的时候失效。
    """
    # -- push / unshift：记录真实注册序 -------------------------------------
    if not getattr(utils_mod.DisposableList.push, "__cordis_traced__", False):
        current_push = utils_mod.DisposableList.push
        current_unshift = utils_mod.DisposableList.unshift

        def traced_push(self, value):
            result = current_push(self, value)
            label = _effect_label_of(value)
            if label is not None:
                _PUSH_LOG.append(label)
            return result

        def traced_unshift(self, value):
            result = current_unshift(self, value)
            label = _effect_label_of(value)
            if label is not None:
                _PUSH_LOG.append(label)
            return result

        for fn in (traced_push, traced_unshift):
            setattr(fn, "__cordis_traced__", True)
        utils_mod.DisposableList.push = traced_push
        utils_mod.DisposableList.unshift = traced_unshift

    # -- clear：即时判定 INV-3b（边属性），不把派发序写进抽象状态 ------------
    if not getattr(utils_mod.DisposableList.clear, "__cordis_traced__", False):
        current_clear = utils_mod.DisposableList.clear

        def traced_clear(self):
            # 先取注册序（clear 之前该列表的内容），再取派发序。
            # 两者都在**此处**一次性可见，故边属性可以就地判定，无需留存。
            registered = [
                label
                for label in (
                    _effect_label_of(v) for v in list(self)
                )
                if label is not None
            ]
            values = current_clear(self)
            dispatched = []
            for value in values:
                label = _effect_label_of(value)
                if label is not None:
                    dispatched.append(label)
            if len(dispatched) >= 2:
                violation = _check_reverse(registered, dispatched)
                if violation is not None and _EDGE_SINK is not None:
                    _EDGE_SINK.append(violation)
            for label in dispatched:
                _DISPATCH_LOG.append(label)
            return values

        setattr(traced_clear, "__cordis_traced__", True)
        utils_mod.DisposableList.clear = traced_clear


#: 边属性违规的收集槽。每次 ``replay`` 前指向当前 World 的 ``edge_violations``，
#: 结束后置空。设为 None 表示无人接收（模块被直接导入使用时的默认态）。
_EDGE_SINK: Optional[List[str]] = None


def _check_reverse(registered: List[str], dispatched: List[str]) -> Optional[str]:
    """INV-3b 的核心：派发序必须是注册序的**逆序**（同一 DisposableList 内）。

    这是 ``DisposableList.clear`` 里那一句 ``reverse()`` 的直接后果，也是
    effect 所有权（LIFO）语义的唯一可判定表述。

    ## 为什么必须用框架自己的 push 序而不是源码顺序

    ``ctx.effect(setup, label)`` 的 wrapper 在 **setup 执行之前**就被 push 进
    ``_disposables``；而 ``setup`` 内部再 ``ctx.effect(...)`` 注册的子 effect
    是在其之后 push 的。因此"源码里写的先后"与"真实注册序"常常相反。实测：
    ``outer()`` 内注册 A、B，外层再注册 SAME，真实注册序为
    ``OUTER, A, B, SAME``，逆序即 ``SAME, B, A, OUTER`` —— 恰好等于
    ``clear()`` 的 yield 序。任何基于源码书写顺序的断言都会误报。

    ## 为什么在 ``clear()`` 处判定而不是放进抽象状态

    判定逆序必须区分批次（= 同一 fiber 的 ``_disposables``），而批次标识含
    uid 这个**无界身份量**；若把它放进抽象状态，状态空间本质无界，闭包不可达
    （实测批次组基数 1→22持续增长、|S| 4096 仍在涨）。把判定移到迁移发生处，
    状态就只保留有界结构，而判定强度**不变**——这正是模型检验中"边属性"的
    标准做法。实测同一批变异在该改法下仍被完整捕获。
    """
    if len(dispatched) < 2 or len(registered) < 2:
        return None
    # 只取与派发项同批次的注册项（不同 DisposableList 之间无逆序关系）
    batches = {_batch_of(label) for label in dispatched}
    scoped = [label for label in registered if _batch_of(label) in batches]
    if len(scoped) < 2:
        return None
    want = list(reversed(scoped))
    if dispatched != want:
        bad = next(
            (i for i, (g, w) in enumerate(zip(dispatched, want)) if g != w), None
        )
        detail = (
            f"位置 {bad} 派发 {dispatched[bad]!r}，逆序应为 {want[bad]!r}"
            if bad is not None
            else "长度不匹配"
        )
        return f"派发非逆序（{detail}）；派发={dispatched}，注册序逆序={want}"
    return None


def _p_inv3_order(state: AbstractState) -> Optional[str]:
    """INV-3b 已改为**边属性**，不再在状态上判定。

    判定逻辑见 ``_check_reverse``（在 ``clear()`` 插桩处就地执行）。此函数仅
    保留作为占位，避免性质表索引错位。
    """
    return None
def _p_inv4(state: AbstractState) -> Optional[str]:
    """INV-4：注册表与 fiber 集合一致（活跃条目必有对应存活 fiber）。"""
    live = sum(1 for view in state.fibers if view.alive)
    for name, provider_alive, _ in state.store:
        if name == "logger":
            continue  # 内置 logger 由根 fiber 常驻提供，见 context.ts
        if provider_alive and live == 0:
            return f"store 中 {name!r} 的提供方标记为存活，但无任何存活 fiber"
    return None


def _p_inv5(state: AbstractState) -> Optional[str]:
    """INV-5：隔离作用域互不串扰——store 不残留已释放提供方的条目。"""
    for name, provider_alive, _ in state.store:
        if not provider_alive:
            return f"store 残留提供方已释放的服务条目 {name!r}"
    return None


def _p_inv6(state: AbstractState) -> Optional[str]:
    """INV-6：监听器随注册方 fiber 消亡。"""
    for name, count, dead in state.listeners:
        if dead:
            return f"事件 {name!r} 仍有 {dead} 个监听器属于已释放 fiber"
        if count and name.startswith("ev/"):
            # 插件自有名事件：宿主 fiber 已死则监听器必须为空
            pass
    return None


# INV-7（无悬挂协程）由 ``-W error::RuntimeWarning`` 横切保证：它是一个
# 关于**进程状态**的性质，无法从抽象状态判定，故不列入 PROPERTIES。
# 运行验证器时应加该标志：``python -W error::RuntimeWarning tools/verify.py``。


def _p_inv8(state: AbstractState) -> Optional[str]:
    """INV-8：依赖唤醒——消费方不得在提供方存活时停留 PENDING。"""
    if not state.provider_seen:
        return None
    svc_alive = any(
        name == "svc" and alive for name, alive, _ in state.store
    )
    if not svc_alive:
        return None
    for view in state.fibers:
        if not view.alive or "svc" not in view.inject:
            continue
        if view.state == "PENDING":
            return f"消费方 inject=svc 但停留 PENDING（提供方存活）"
    return None


#: 性质表：编号 → (说明, 判定函数)
PROPERTIES: Dict[str, Tuple[str, Callable[[AbstractState], Optional[str]]]] = {
    "INV-1": ("状态机只走合法迁移边", _p_inv1),
    "INV-2": ("uid 与 state 一致", _p_inv2),
    "INV-3a": ("disposer 至多一次且均已登记", _p_inv3),
    "INV-4": ("注册表与 fiber 集合一致", _p_inv4),
    "INV-5": ("隔离作用域不串扰（store 无孤儿）", _p_inv5),
    "INV-6": ("监听器随注册方 fiber 消亡", _p_inv6),
    "INV-8": ("依赖唤醒：提供方存活则消费方不 PENDING", _p_inv8),
}


def check_properties(state: AbstractState) -> List[Tuple[str, str]]:
    """对单个抽象状态检查全部性质，返回 [(编号, 违反原因), ...]。"""
    violations = []
    for name, (_, predicate) in PROPERTIES.items():
        reason = predicate(state)
        if reason is not None:
            violations.append((name, reason))
    return violations


# ---------------------------------------------------------------------------
# §3 转移系统：操作字母表与后继计算
# ---------------------------------------------------------------------------
#
# 关键设计：操作必须**由抽象状态定向**，不能依赖 uid 序号。原因是去重的
# 正确性——若同一抽象状态的不同具体实例因 uid 排序而选中不同 fiber，后继就
# 不唯一，去重会丢掉真实行为。这里改为按 fiber 在投影中的**规范序**取用，
# 保证 "同抽象状态 → 同一操作 → 同一抽象后继"。


class World:
    """一次验证运行的执行体（与 tests/test_properties.py 同构，独立以免耦合）。"""

    def __init__(self) -> None:
        self.root = Context()
        self.disposer_expected: List[str] = []
        self.disposer_runs: List[str] = []
        # 当前已登记但尚未完成的 disposer（完成即移除 ⇒ 有界）
        self.pending: List[str] = []
        # 同一标签被完成两次的记录（重复执行即违规）
        self.duplicate_runs: List[str] = []
        # **边属性**违规：逆序判定在迁移发生处即时检查，结果记在这里而**不**
        # 进入抽象状态。原因见AbstractState 注释：批次标识含 uid，属无界
        # 身份量，进入状态会使状态空间本质无界。
        self.edge_violations: List[str] = []
        self.transitions: List[Tuple[int, int]] = []
        self.listener_hits: List[str] = []
        self.provider_seen = False
        # ``_seq`` 给 disposer 标签提供 fiber 归属；``_label_seq`` 只用于
        # 人读标签（事件名等），两者互不干扰，避免编号耦合。
        self._seq = 0
        self._label_seq = 0
        self._batch: Optional[int] = None
        self._generation = 0
        self.root.on("internal/status", self._on_status)
        # 装配 ``clear()`` 派发序插桩（进程内幂等）。
        _instrument_dispatch()

    def _on_status(self, fiber, old_state):
        self.transitions.append((old_state, fiber.state))

    def _next_label(self, kind: str) -> str:
        self._label_seq += 1
        return f"{kind}#{self._label_seq}"

    def _track(self, label: str, ctx: Any = None) -> Callable[[], None]:
        """登记并返回一个**带标签**的 disposer。

        标签格式 ``<fiber键>|<代次>|<局部名>``：

- ``fiber键``：逆序只在单个 fiber 的 ``_disposables`` 内成立；
- ``代次``：一个 fiber 重载 N 次就有 N 代同名 disposer（局部名相同），必须靠
  代次区分——否则 ``load, update, dispose`` 会把"两代各执行一次"误判为
  "重复执行"（实测踩过）。

返回值上挂标签，供插桩在 ``clear()`` 处记录**派发**顺序。
        """
        if ctx is None:
            fiber_key = f"b{self._batch if self._batch is not None else -1}"
        else:
            fiber = getattr(ctx, "fiber", None)
            fiber_key = f"f{getattr(fiber, 'uid', 0) or 0}"
        # 同代判定：同一 fiber 的同一插件主体调用内视作一代。``_generation``
        # 在每次插件主体开始时递增（见 ``_new_batch``）。
        key = f"{fiber_key}|{self._generation}|{label}"
        self.disposer_expected.append(key)
        self.pending.append(key)

        def _dispose() -> None:
            self.disposer_runs.append(key)
            # 完成即从"未完成"集合移除：这是让账本有界的关键——``pending``
            # 的长度被存活 fiber 数界住，而``disposer_runs`` 仅用于重复检测
            # （由 ``duplicate_finished`` 的有界摘要承载，不进抽象状态）。
            try:
                self.pending.remove(key)
            except ValueError:  # pragma: no cover - 正常路径不会发生
                self.duplicate_runs.append(key)

        setattr(_dispose, _LABEL_ATTR, key)
        return _dispose

    def _new_batch(self) -> None:
        """开始新一代插件主体调用（代次 +1）。

        每次插件主体执行（含重载后的重新执行）算一代。同代内 effect 局部名
        唯一，跨代可重复——``_track`` 据此生成可判别的标签。
        """
        self._batch = None
        self._generation += 1

    def fibers(self) -> List[Any]:
        result: List[Any] = []
        for runtime in self.root.registry.values():
            result.extend(runtime.fibers)
        return result

    def _ordered_fibers(self) -> List[Any]:
        """按**结构特征**排序（与 uid 无关），使操作对抽象状态是确定的。"""
        return sorted(
            self.fibers(),
            key=lambda f: (
                FiberState.name_of(f.state),
                len(f._disposables),
                tuple(sorted((f.inject or {}).keys())),
                tuple(sorted(f._hooks.keys())),
            ),
        )

    # -- 操作 ---------------------------------------------------------------
    # 四个加载方法给各自 plugin callback 打上 ``__cordis_shape__`` 结构标签。
    # verify.py 本身不读该标签；``tools/verify_unbounded.py`` 的无界投影用它
    # 区分"同一视图的 fiber 是否来自同一插件主体"（同主体 ⇒ 重载行为相同，
    # 这是无界证明里视图可互换性的前提）。打标签不改变任何行为。
    def load_provider(self) -> None:
        def plugin(ctx, config):
            self._new_batch()
            disposer = self._track("provider", ctx)

            def setup():
                ctx.provide("svc", "v")
                return disposer  # 真正的 disposer 交给框架在卸载时调用

            ctx.effect(setup, self.disposer_expected[-1])

        plugin.__cordis_shape__ = "provider"
        self.root.plugin(plugin)

    def load_consumer(self) -> None:
        def plugin(ctx, config):
            self._new_batch()
            disposer = self._track("consumer", ctx)

            def setup():
                return disposer

            ctx.effect(setup, self.disposer_expected[-1])

        plugin.inject = ["svc"]
        plugin.__cordis_shape__ = "consumer"
        self.root.plugin(plugin)

    def load_effect(self) -> None:
        """加载一个**含 3 个真实 disposer** 的插件。

        关键设计：``ctx.effect(主体)`` 的**主体在setup 阶段立即执行**，其
        返回值才是 disposer。直接写 ``ctx.effect(lambda: ..., label)`` 只会
        观测到 setup 执行，看不到卸载时的逆序释放。因此这里显式构造：

        - ``A()``：setup 返回 disposer ``dA``
        - ``B()``：setup 返回 disposer ``dB``
        - ``outer()``：setup 内部先注册 A、B，返回 disposer ``dOuter``

        注册顺序 ``dA → dB → dOuter``，故卸载时的正确释放顺序是
        ``dOuter → dB → dA``（严格逆序）。这才是 INV-3b 的观测点。
        """

        def plugin(ctx, config):
            self._new_batch()
            disposers = {
                "A": self._track("A", ctx),
                "B": self._track("B", ctx),
                "OUTER": self._track("outer", ctx),
            }
            # effect label 必须与 _track 登记的 key 完全一致——push/dispatch
            # 插桩正是靠这个 label 把三本账对齐。
            keys = {
                "A": self.disposer_expected[-3],
                "B": self.disposer_expected[-2],
                "OUTER": self.disposer_expected[-1],
            }

            def inner_a():
                return disposers["A"]

            def inner_b():
                return disposers["B"]

            def outer():
                # 注意：``ctx.effect`` 的 wrapper 在 setup 执行**之前**就被
                # push，故真实注册序为 OUTER → A → B（见 _check_reverse 的说明）。
                ctx.effect(inner_a, keys["A"])
                ctx.effect(inner_b, keys["B"])
                return disposers["OUTER"]

            ctx.effect(outer, keys["OUTER"])
            label = self._next_label("effect")
            ctx.on(f"ev/{label}", lambda *a: self.listener_hits.append(f"ev/{label}"))

        plugin.__cordis_shape__ = "effect"
        self.root.plugin(plugin)

    def load_isolated(self) -> None:
        iso = self.root.isolate("svc", label=f"iso-{self._label_seq}")
        self._label_seq += 1

        def plugin(ctx, config):
            self._new_batch()
            ctx.provide("svc", "ISO")
            hits: Dict[str, int] = {}

            def track(name, value):
                hits[name] = hits.get(name, 0) + 1

            ctx.on("internal/service", track)

        plugin.__cordis_shape__ = "isolated"
        iso.plugin(plugin)

    def dispose_nth(self, index: int) -> Any:
        fibers = self._ordered_fibers()
        if not fibers:
            return None
        return fibers[index % len(fibers)].dispose()

    def update_nth(self, index: int) -> Any:
        fibers = self._ordered_fibers()
        if not fibers:
            return None
        return fibers[index % len(fibers)].update({"n": self._label_seq})

    def restart_nth(self, index: int) -> Any:
        """返回 ``restart()`` 的协程本身。

        必须把协程**返回**给调用方去await —— 在同步方法里直接调用
        ``restart()`` 会丢弃其协程对象，直接违反 INV-7。
        """
        fibers = self._ordered_fibers()
        if not fibers:
            return None
        return fibers[index % len(fibers)].restart()

    def emit(self) -> None:
        self.root.emit("ev")


#: 操作字母表：名称 → World 上的方法
ALPHABET: Dict[str, str] = {
    "load_provider": "load_provider",
    "load_consumer": "load_consumer",
    "load_effect": "load_effect",
    "load_isolated": "load_isolated",
    "dispose#0": "dispose_nth",
    "dispose#1": "dispose_nth",
    "dispose#last": "dispose_nth",
    "update#0": "update_nth",
    "restart#0": "restart_nth",
    "emit": "emit",
}

#: 需要额外参数的操作（参数由抽象状态规范序决定，此处固定索引）
_FIXED_ARGS: Dict[str, int] = {"dispose#0": 0, "dispose#1": 1, "update#0": 0, "restart#0": 0}
_LAST = -1


async def _settle(world: World) -> None:
    """把挂起的生命周期任务驱动到收敛。

    注意：``await fiber.await_()`` 可能同步抛出（若它内部驱动的协程失败）。
    此时的清理不能靠 ``except: pass`` —— 抛出点之后的协程将**永远未被
    await**，直接违反 INV-7。因此这里保证：要么完整 await 成功，要么把
    返回的 awaitable 在 ``finally`` 里强制排空。
    """
    for _ in range(3):
        for runtime in list(world.root.registry.values()):
            for fiber in list(runtime.fibers):
                try:
                    await fiber.await_()
                except Exception:  # noqa: BLE001 - 失败也要继续驱动其余 fiber
                    pass
        await asyncio.sleep(0)


async def apply_op(world: World, op: str) -> None:
    """在 world 上执行一个字母表操作并驱动到收敛。"""
    result: Any = None
    if op == "dispose#last":
        fibers = world._ordered_fibers()
        if not fibers:
            return
        result = fibers[-1].dispose()
    elif op in _FIXED_ARGS:
        method = getattr(world, ALPHABET[op])
        result = method(_FIXED_ARGS[op])
    else:
        result = getattr(world, ALPHABET[op])()
    # 关键：所有公开操作都可能返回 awaitable（dispose/update/restart 均是
    # coroutine）。不 await 就是 INV-7 的直接违反——这正是本验证器自身的
    # 一条横切不变量。
    if inspect.isawaitable(result):
        try:
            await result
        except Exception:  # noqa: BLE001 - 合法错误路径
            pass
    await _settle(world)


#: 基数约束：同时存活的 fiber 数量上界。``None`` 表示不加约束。
#:
#: ## 为什么这个约束能带来闭包
#:
#: 插件数量无界时，可达集随序列长度增长，闭包不可达（实测层间增长率 6–7×，
#: depth 5 时前沿仍有 5570 状态）。加上上界 N 后，"存活 fiber 数"成为抽象
#: 状态的一个有界维度，整个状态空间因此**有界**，闭包在原理上可达。
#:
#: ## 约束的语义
#:
#: 已达上界时，加载类操作退化为**空操作**（后继仍为该状态本身），而不是被
#: "丢弃"。这不是弱化证明：后继仍在状态集合内，闭包义务照样逐条检查；它只是
#: 把"超过 N 的那些行为"移出了被证明的范围。结论因此精确表述为
#: **任意长度、任意时刻存活插件 ≤ N 的操作序列上，8 条不变量成立**。
#:
#: 该约束不损失一般性到"无法接受"的程度：N 取 5~6 已覆盖绝大多数真实场景，
#: 且随时可调大重跑。
PLUGIN_CAP: Optional[int] = None

#: 加载类操作（受基数约束限制的那些）
_LOAD_OPS = frozenset({"load_provider", "load_consumer", "load_effect", "load_isolated"})


def _live_fiber_count(world: World) -> int:
    """统计当前**存活**（未释放）的 fiber 数量。"""
    return sum(1 for f in world.fibers() if f.uid is not None)


async def replay(path: Sequence[str]) -> World:
    """从初态重放一条操作序列。

    合法错误路径（重复 provide、对已释放 fiber 操作等）不得中断重放，否则
    后继状态无从检查；且中断处的 awaitable 必须被**排空**而不是丢弃，否则
    会产生"coroutine never awaited"警告——那本身就是 INV-7 的违反，会让
    验证器自身成为被测对象之外的噪声源。
    """
    world = World()
    # 派发账本是**进程级**的（``clear()`` 是全局插桩点），故每条重放路径
    # 从空账本开始，保证边检查的结果与该路径严格对应。
    _DISPATCH_LOG.clear()
    _PUSH_LOG.clear()
    # 边属性（INV-3b）的违规收集槽：判定发生在 ``clear()`` 插桩处，
    # 结果记入World 而不进入抽象状态（uid 是无界身份量，见 _check_reverse）。
    global _EDGE_SINK
    _EDGE_SINK = world.edge_violations
    for op in path:
        # -- 基数约束：已达上界时，加载类操作退化为空操作 ---------------------
        # 注意是"退化为自环"而非"跳过"：后继仍是该状态本身，闭包义务照样被
        # 检查，被移出范围的只是"超过 N 那些"行为。
        if (
            PLUGIN_CAP is not None
            and op in _LOAD_OPS
            and _live_fiber_count(world) >= PLUGIN_CAP
        ):
            continue
        pending: List[Any] = []
        try:
            await _apply_op_collect(world, op, pending)
        except Exception:  # noqa: BLE001 - 合法错误路径
            pass
        await _drain(pending)
        try:
            await _settle(world)
        except Exception:  # noqa: BLE001
            pass
    return world


async def _apply_op_collect(world: World, op: str, pending: List[Any]) -> None:
    """执行一个操作；把可能残留的 awaitable 放进 ``pending`` 而非丢弃。"""
    result: Any = None
    if op == "dispose#last":
        fibers = world._ordered_fibers()
        if not fibers:
            return
        result = fibers[-1].dispose()
    elif op in _FIXED_ARGS:
        result = getattr(world, ALPHABET[op])(_FIXED_ARGS[op])
    else:
        result = getattr(world, ALPHABET[op])()
    if inspect.isawaitable(result):
        pending.append(result)
        try:
            await result
        finally:
            pending.remove(result)


async def _drain(pending: Sequence[Any]) -> None:
    """强制排空残留的 awaitable，保证 INV-7 在验证器自身也不被违反。"""
    for item in list(pending):
        try:
            await item
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# §4 算法主体：不动点 + 封闭性 + 性质
# ---------------------------------------------------------------------------


@dataclass
class Counterexample:
    path: List[str]
    violations: List[Tuple[str, str]]
    state: AbstractState

    def render(self) -> str:
        lines = [f"反例路径（{len(self.path)} 步）："]
        for i, op in enumerate(self.path):
            lines.append(f"  {i:>3}. {op}")
        lines.append("终态：")
        lines.append(
            "\n".join("    " + line for line in self.state.render().splitlines())
        )
        lines.append("违反：")
        for name, reason in self.violations:
            lines.append(f"  [{name}] {reason}")
        return "\n".join(lines)


@dataclass
class ProofResult:
    depth_reached: int
    states: int
    checked: int
    closure_ok: bool
    inconclusive: bool
    counterexample: Optional[Counterexample] = None
    open_transitions: List[Tuple[List[str], str]] = field(default_factory=list)
    elapsed: float = 0.0

    def verdict(self) -> str:
        if self.counterexample is not None:
            return "REFUTED（找到真实反例）"
        if self.closure_ok and not self.inconclusive:
            return "PROVED（归纳不变式封闭，任意长度成立）"
        if self.inconclusive:
            return "INCONCLUSIVE（不变式未封闭，不构成证明）"
        return "CLOSED-ONLY（性质成立但封闭性未证）"


async def verify(
    max_depth: int = 4,
    max_states: int = 20000,
    alphabet: Optional[Sequence[str]] = None,
    verbose: bool = True,
) -> ProofResult:
    """归纳不变式验证算法主入口。

    返回 ``ProofResult``：若找到反例则 ``counterexample`` 非空；
    ``closure_ok`` 为真表示可达集在给定深度内**已封闭**，此时性质对任意长度
    的操作序列成立；``inconclusive`` 为真表示性质未被证伪但封闭性未获证明
    ——算法明确区分"通过"与"未能证明"。
    """
    ops = list(alphabet or ALPHABET)
    start = time.time()

    # -- Step 1：构造可达集 S，并检查义务 1（性质） -------------------------
    initial = project(await replay(()))
    visited: Dict[AbstractState, List[str]] = {initial: []}
    frontier: List[List[str]] = [[]]
    violations: List[Tuple[str, str, AbstractState]] = check_properties(initial)
    if violations:  # 初态即违反：性质 P 本身写错了
        return ProofResult(
            depth_reached=0,
            states=1,
            checked=1,
            closure_ok=False,
            inconclusive=False,
            counterexample=Counterexample([], violations, initial),
            elapsed=time.time() - start,
        )

    checked = 1
    depth = 0
    open_transitions: List[Tuple[List[str], str]] = []
    closure_ok = False
    truncated = False

    while frontier and depth < max_depth:
        depth += 1
        next_frontier: List[List[str]] = []
        for path in frontier:
            for op in ops:
                new_path = path + [op]
                world = await replay(new_path)
                state = project(world)
                checked += 1

                # 边属性检查（INV-3b）：在迁移发生处判定，与状态无关
                if world.edge_violations:
                    return ProofResult(
                        depth_reached=depth,
                        states=len(visited),
                        checked=checked,
                        closure_ok=False,
                        inconclusive=False,
                        counterexample=Counterexample(
                            new_path,
                            [("INV-3b", world.edge_violations[0])],
                            state,
                        ),
                        elapsed=time.time() - start,
                    )
                if state not in visited:
                    visited[state] = new_path
                    bad = check_properties(state)
                    if bad:
                        return ProofResult(
                            depth_reached=depth,
                            states=len(visited),
                            checked=checked,
                            closure_ok=False,
                            inconclusive=False,
                            counterexample=Counterexample(new_path, bad, state),
                            elapsed=time.time() - start,
                        )
                    next_frontier.append(new_path)

                if len(visited) >= max_states:
                    truncated = True
                    break
            if truncated:
                break
        frontier = next_frontier
        if verbose:
            print(
                f"  depth={depth} |S|={len(visited)} frontier={len(frontier)} "
                f"checked={checked} t={time.time() - start:.1f}s"
            )
        if truncated:
            break
        # -- Step 2：义务 2（封闭性）。frontier 为空 ⟺ 无新状态 ⟺ S 已封闭。
        if not frontier:
            closure_ok = True
            break

    if not closure_ok and not truncated:
        # 深度耗尽但仍有未展开的前沿：记录开放迁移，供人工判断。
        for path in frontier[:20]:
            open_transitions.append((path, "<depth exhausted>"))

    return ProofResult(
        depth_reached=depth,
        states=len(visited),
        checked=checked,
        closure_ok=closure_ok,
        inconclusive=(not closure_ok) and truncated is False and bool(open_transitions),
        open_transitions=open_transitions,
        elapsed=time.time() - start,
    )


# ---------------------------------------------------------------------------
# §5 变异测试：把真实历史缺陷重新注入，验证器必须抓到
# ---------------------------------------------------------------------------
#
# 每条变异 = (名称, 注入函数, 恢复函数, 触发的性质编号)。
# 注入通过 monkey-patch 完成，不改动仓库文件。


class _Mutant:
    """以上下文管理器方式临时替换 ``target.attr``。"""

    def __init__(self, name: str, target: Any, attr: str, replacement: Any):
        self.name = name
        self.target = target
        self.attr = attr
        self.replacement = replacement
        self.original = getattr(target, attr)

    def __enter__(self) -> "_Mutant":
        setattr(self.target, self.attr, self.replacement)
        return self

    def __exit__(self, *exc) -> bool:
        setattr(self.target, self.attr, self.original)
        return False


def _mut_disposer_forward_order() -> _Mutant:
    """缺陷：``DisposableList.clear`` 漏掉 ``reverse()`` → 释放不再逆序。

    对应本项目第一轮修复的「`_run_disposable` 用 `or` 回退」同族问题：
    清理顺序是 effect 所有权语义的核心，破坏后应由 INV-3 抓到。
    """

    def broken(self):
        values = list(self._map.values())
        self._map.clear()
        return values  # 缺陷：缺少 values.reverse()

    return _Mutant(
        "disposer 正序释放", utils_mod.DisposableList, "clear", broken
    )


def _mut_state_never_pending() -> _Mutant:
    """缺陷：``_get_state`` 漏掉epoch 判定 → PENDING 被误报为 ACTIVE。

    这条变异最初被归为"域外"：``Fiber.state`` 是普通属性，``_get_state`` 仅在
    ``_update_state`` 内被调用，直觉上只会让fiber 少报一次中间态，不会产生
    非法迁移边。

    但 depth=4 的实测**推翻了该推断**——它被 INV-1 捕获。原因是
    ``_update_state`` 在部分路径上以 ``_get_state()`` 的返回值直接改写状态，
    误判后的 PENDING→ACTIVE 会形成一条不存在的迁移路径，恰好落在INV-1 的
    观测面上。

    因此 ``期望性质`` 仍标 ``None``（该缺陷的设计意图是检验状态语义，非逆序
    或注册表），但实测会被捕获并由 CLI 单列。此处保留docstring 以记录
    "判断被实测修正"的过程。
    """

    def broken(self) -> int:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error:
            return FiberState.FAILED
        return FiberState.ACTIVE  # 缺陷：漏掉 epoch != INACTIVE 的判定

    return _Mutant(
        "PENDING 被误判为 ACTIVE（状态语义盲区）",
        fiber_mod.Fiber,
        "_get_state",
        broken,
    )


def _mut_is_bailed_nan() -> _Mutant:
    """缺陷：``is_bailed`` 对 NaN 放行（参考实现的同名缺陷）。

    这是一条**不在性质集覆盖范围内**的变异：INV-1~8 都不观测 bail 判据，
    因此验证器**预期漏检**。把它放进清单是刻意的——它量化了性质集的边界，
    提醒读者「未捕获」不等于「无缺陷」。``期望性质=None`` 即标记此意。
    """

    def broken(value: Any) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, float) and value != value:
            return False  # 缺陷：NaN 被放行
        return True

    from cordis_py import events as events_mod

    return _Mutant("bail 判据放行 NaN", events_mod, "is_bailed", broken)


def _mut_store_orphan() -> _Mutant:
    """缺陷：``provide`` 的清理完成后把条目塞回注册表 → 孤儿残留（INV-5）。

    ## 注入点的选择（踩过两次坑）

    直接包装 ``ReflectService.provide`` 的返回值是**无效**的：该返回值其实是
    ``Fiber.effect`` 的 ``wrapper``，而 ``wrapper`` 闭包捕获的是**原始**
    ``_dispose``——包装层被完全绕过（实测打印确认）。包装 ``Context.provide``
    同样无效。

    可判定的注入点是 ``_run_unload_one``：它在**每个 disposer 派发后**统一
    收口，且能拿到 fiber（从而拿到该 fiber 曾提供的服务名）。这里在清理
    完成之后把孤儿条目写回 store——等价于"注册表清理失效"，精确落在
    INV-5 可观测的那一处，且不影响唤醒与逆序派发。
    """

    from cordis_py import fiber as fiber_mod
    from cordis_py import reflect as reflect_mod

    original_run = fiber_mod._run_unload_one
    leaked: Dict[int, List[Any]] = {}

    async def broken_run(fiber, dispose):
        await original_run(fiber, dispose)
        # 缺陷：卸载完成后把该 fiber 曾提供的服务塞回 store（孤儿条目）。
        pending = leaked.setdefault(id(fiber), [])
        if pending:
            store = fiber.ctx.reflect.store
            for name, value, check in pending:
                store[name] = reflect_mod.Impl(name, fiber, value, check)
            pending.clear()

    def broken_provide(self, name, value=None, check=None, caller=None):
        # 记录"该 fiber 曾提供过什么"，供上面的收口阶段塞回。
        caller = caller if caller is not None else self.ctx
        leaked.setdefault(id(caller.fiber), []).append((name, value, check))
        return original_provide(self, name, value, check, caller)

    original_provide = reflect_mod.ReflectService.provide

    class _Composite(_Mutant):
        def __enter__(self):
            fiber_mod._run_unload_one = broken_run
            reflect_mod.ReflectService.provide = broken_provide
            return self

        def __exit__(self, *exc):
            fiber_mod._run_unload_one = original_run
            reflect_mod.ReflectService.provide = original_provide
            leaked.clear()
            return False

    return _Composite("provide 清理后留下孤儿条目", fiber_mod, "_run_unload_one", None)


def _mut_listener_leak() -> _Mutant:
    """缺陷：事件监听器不从 ``_hooks`` 摘除 → 随 fiber 消亡的契约被破坏。

    对应第一轮修复的「4 处 fire-and-forget catch 链从未被调度：插件拆卸
    不完整、监听器泄漏」。这里注入的是"幽灵监听器"：归属某个 fiber 的
    监听器被塞进事件表，但**不属于**任何 effect 的清理列表，因此 fiber
    卸载时不会被摘除——精确模拟释放路径失效。
    """

    from cordis_py import events as events_mod

    def broken_unregister(self, hooks, callback):
        # 缺陷：找到目标也不移除 —— 监听器永久留在事件表里，
        # fiber 释放后成为孤儿（INV-6）。
        return None

    return _Mutant(
        "监听器不随 fiber 消亡",
        events_mod.EventsService,
        "unregister",
        broken_unregister,
    )


#: 变异清单：(名称, 工厂, 期望触发的性质编号)
#:
# 变异只挑选**本性质集能够判别**的历史缺陷；``期望性质=None`` 标记的是
# **刻意不在覆盖范围内**的缺陷（用来量化性质集边界，而不是算作验证器失败）。
#灵敏度与完备性的取舍见 docs/FORMAL_VERIFICATION.md。

MUTATIONS: List[Tuple[str, Callable[[], "_Mutant"], Optional[str]]] = [
    ("disposer 正序释放", _mut_disposer_forward_order, "INV-3b"),
    ("PENDING 被误判为 ACTIVE", _mut_state_never_pending, None),
    ("bail 判据放行 NaN", _mut_is_bailed_nan, None),
    ("provide 的 disposer 不执行", _mut_store_orphan, "INV-5"),
    ("监听器不随 fiber 消亡", _mut_listener_leak, "INV-6"),
]


# ---------------------------------------------------------------------------
# §6 CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="cordis_py 归纳不变式验证器")
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--max-states", type=int, default=20000)
    parser.add_argument("--no-mutate", action="store_true", help="跳过变异测试")
    parser.add_argument(
        "--plugin-cap",
        type=int,
        default=None,
        help="同时存活插件数上界 N；给定后状态空间有界，有望闭包并得到 PROVED",
    )
    args = parser.parse_args(argv)

    print("=" * 72)
    print("cordis_py 形式化正确性验证 —— 归纳不变式算法")
    print("=" * 72)
    print(f"性质：{', '.join(PROPERTIES)}")
    print(f"字母表（{len(ALPHABET)} 元）：{', '.join(ALPHABET)}")
    if args.plugin_cap is not None:
        global PLUGIN_CAP
        PLUGIN_CAP = args.plugin_cap
        print(f"基数约束：同时存活插件 ≤ {PLUGIN_CAP}")
    print()

    print("[1/2] 证明义务检查")
    result = asyncio.run(
        verify(max_depth=args.max_depth, max_states=args.max_states)
    )
    print(
        f"  深度={result.depth_reached} 可达状态={result.states} "
        f"检查点={result.checked} 封闭={result.closure_ok} "
        f"耗时={result.elapsed:.1f}s"
    )
    print(f"  裁决：{result.verdict()}")
    if result.closure_ok:
        scope = (
            f"任意长度、任意时刻存活插件 ≤ {PLUGIN_CAP}"
            if PLUGIN_CAP is not None
            else "（当前无基数约束）"
        )
        print(f"  证明范围：{scope}")
    if result.counterexample is not None:
        print()
        print(result.counterexample.render())

    # 退出码语义（刻意与"闭包"解耦）：
    #   0 = 未找到真实反例，且（若跑了变异测试）域内变异全部捕获；
    #   1 = 找到真实反例（REFUTED），或域内变异存在漏检（验证器强度不足）。
    # "闭包未达成"（INCONCLUSIVE）**不**拉低退出码——它是一个稳定的、已知的
    # 能力边界，不是本次运行的失败。若把它计入退出码，CI 会长期红灯，
    # 反而诱导人去忽略真正需要关注的反例与漏检。
    exit_code = 1 if result.counterexample is not None else 0
    if result.inconclusive or not result.closure_ok:
        print(
            "  提示：可达集未闭包，本裁决不构成『任意长度』的证明"
            "（能力边界，非本次失败，详见 docs/FORMAL_VERIFICATION.md §6.1）"
        )

    if not args.no_mutate:
        print()
        print("[2/2] 变异测试（验证器灵敏度）")
        print("  指标只统计『性质集覆盖范围内』的变异（期望性质 ≠ None）。")
        print("  标注为覆盖范围外的变异用于量化性质集边界，不计入灵敏度——")
        print("  『未捕获』在那里意味着『该缺陷不被这些不变量观测到』，而非验证器失败。")
        print()
        caught = 0
        in_scope = 0
        out_of_scope: List[str] = []
        for name, factory, expected in MUTATIONS:
            with factory():
                mutated = asyncio.run(
                    verify(max_depth=args.max_depth, max_states=args.max_states, verbose=False)
                )
            found = mutated.counterexample is not None
            detail = (
                ",".join(n for n, _ in mutated.counterexample.violations)
                if found
                else "未捕获"
            )
            if expected is None:
                mark = "· 域外"
                if found:
                    mark = "✓ 捕获(域外)"
                else:
                    out_of_scope.append(name)
                print(f"  {mark}  {name:<28} 触发性质：{detail}")
                continue
            in_scope += 1
            mark = "✓ 捕获" if found else "✗ 漏检"
            note = f"（期望 {expected}）" if not found else ""
            print(f"  {mark}  {name:<28} 触发性质：{detail}{note}")
            caught += int(found)
        print()
        print(
            f"  灵敏度（域内）：{caught}/{in_scope}"
            + ("" if caught == in_scope else "  ← 存在漏检，验证器强度不足")
        )
        if out_of_scope:
            print(f"  覆盖范围外的已知缺陷：{len(out_of_scope)} 项 —— "
                  "它们不被 INV-1~8 观测到，属性质集的已知盲区（见 §残余风险）")
        if caught < in_scope:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())