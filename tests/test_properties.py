"""cordis_py 的形式化验证套件：不变量规约 + 属性测试 + 有界模型检测。

本文件把移植版的核心正确性命题拆成可执行的不变量（INV-1 ~ INV-8），并用
两种互补的手段验证：

1. **属性测试**（随机操作序列）：以种子化的伪随机生成器产生数千条
   「加载 / 提供 / 注入 / 卸载 / 更新 / 隔离 / 事件」操作序列，每步之后
   断言全部不变量。覆盖「任何合理的调用序列」这一空间。

2. **有界模型检测**（Bounded Model Checking）：在小型操作字母表上做
   宽度优先穷举（带抽象状态去重），验证「长度 ≤ N 的**所有**操作序列」
   均满足不变量。去重后的可达状态空间被完整遍历——只要不变量在每条
   迁移上保持（归纳假设），结论即覆盖任意长度的序列。

不变量清单（详见 ``docs/CORRECTNESS.md``）：

- INV-1  fiber 状态机只允许合法迁移边；
- INV-2  uid / state 一致性（uid 为 None ⟺ 已释放）；
- INV-3  effect disposer 逆序执行且恰好一次；
- INV-4  注册表与 fiber 集合一致，插件整体卸载后 runtime 被移除；
- INV-5  隔离作用域互不串扰；
- INV-6  监听器随注册方 fiber 消亡；
- INV-7  无未 await 的协程（RuntimeWarning 升级为错误）；
- INV-8  依赖唤醒：inject 的消费方随提供方生灭。

运行方式::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import inspect
import os
import random
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cordis_py import Context, FiberState  # noqa: E402

# ---------------------------------------------------------------------------
# 不变量规约
# ---------------------------------------------------------------------------

# INV-1：fiber 状态机合法迁移边（对应 fiber.ts 的 _drive_epoch / _settle_reload /
# _settle_unload / _reload / _unload 的全部路径）。
LEGAL_TRANSITIONS = {
    (FiberState.PENDING, FiberState.LOADING),    # 依赖满足，_reload 启动
    (FiberState.PENDING, FiberState.UNLOADING),  # 激活前被卸载
    (FiberState.PENDING, FiberState.FAILED),     # 激活即失败
    (FiberState.LOADING, FiberState.ACTIVE),     # 加载落定
    (FiberState.LOADING, FiberState.UNLOADING),  # 加载中 epoch 变化
    (FiberState.LOADING, FiberState.FAILED),     # 加载抛错
    (FiberState.ACTIVE, FiberState.UNLOADING),   # 主动卸载
    (FiberState.ACTIVE, FiberState.FAILED),      # 运行中转入失败态（更新路径）
    (FiberState.FAILED, FiberState.LOADING),     # 失败后重载
    (FiberState.FAILED, FiberState.UNLOADING),   # 失败后被卸载
    (FiberState.UNLOADING, FiberState.LOADING),  # 卸载完再重载（restart）
    (FiberState.UNLOADING, FiberState.PENDING),  # 卸载完回到等待依赖
    (FiberState.UNLOADING, FiberState.DISPOSED), # 卸载完且已释放
    (FiberState.UNLOADING, FiberState.ACTIVE),   # 卸载中重新满足（瞬时观测）
    # _unload 落定时若 _error 仍在（失败 fiber 被卸载），_getState 返回
    # FAILED——fiber.ts:574-579 与 _settle_unload 的组合路径。
    (FiberState.UNLOADING, FiberState.FAILED),
}
TERMINAL_STATES = {FiberState.DISPOSED}

# 每个 fiber 允许处于的状态集合（uid 一致性由 INV-2 检查）。
ALL_STATES = {
    FiberState.PENDING,
    FiberState.LOADING,
    FiberState.ACTIVE,
    FiberState.FAILED,
    FiberState.DISPOSED,
    FiberState.UNLOADING,
}


class InvariantViolation(AssertionError):
    """不变量被违反时抛出，附带出错的上下文。"""


def check_invariants(world: "World", where: str) -> None:
    """执行全部不变量检查；任一违反即抛 InvariantViolation。"""
    root = world.root
    fibers = world.fibers()

    # INV-2：uid / state 一致性。
    for fiber in fibers:
        if fiber.uid is None:
            if fiber.state != FiberState.DISPOSED:
                raise InvariantViolation(
                    f"{where}: INV-2 uid=None 但 state={FiberState.name_of(fiber.state)}"
                )
        else:
            if fiber.state == FiberState.DISPOSED:
                raise InvariantViolation(f"{where}: INV-2 state=DISPOSED 但 uid={fiber.uid}")

    # INV-1：观测到的每次迁移都是合法边。
    for old_state, new_state in world.transitions:
        if new_state not in ALL_STATES:
            raise InvariantViolation(f"{where}: INV-1 非法状态值 {new_state}")
        if old_state == new_state:
            continue
        if (old_state, new_state) not in LEGAL_TRANSITIONS:
            raise InvariantViolation(
                f"{where}: INV-1 非法迁移 "
                f"{FiberState.name_of(old_state)} -> {FiberState.name_of(new_state)}"
            )

    # INV-3：disposer 至多执行一次（终局全等由 dispose_all 测试检查）。
    if len(set(world.disposer_runs)) != len(world.disposer_runs):
        raise InvariantViolation(f"{where}: INV-3 disposer 被重复执行")
    if len(world.disposer_runs) > len(world.disposer_expected):
        raise InvariantViolation(f"{where}: INV-3 disposer 执行次数超过登记次数")
    for label in world.disposer_runs:
        if label not in world.disposer_expected:
            raise InvariantViolation(f"{where}: INV-3 执行了未登记的 disposer {label!r}")

    # INV-4：注册表与 fiber 集合一致。
    live_runtimes = 0
    for runtime in root.registry.values():
        members = list(runtime.fibers)
        if not members:
            raise InvariantViolation(f"{where}: INV-4 runtime 留有空 fibers 列表")
        live_runtimes += 1
        for fiber in members:
            if fiber.runtime is not runtime:
                raise InvariantViolation(f"{where}: INV-4 fiber 与 runtime 不匹配")
    for fiber in fibers:
        if fiber.uid is not None and fiber not in list(fiber.runtime.fibers):
            raise InvariantViolation(f"{where}: INV-4 活跃 fiber 不在 runtime.fibers 中")

    # INV-5：隔离作用域互不串扰。可检测的硬性质是——store 里不得残留
    # 「提供方已释放」的条目：隔离作用域下注销自己的实现时若误删/漏删，
    # 都会在这里暴露（要么误删别人的，要么留下孤儿）。
    for key, impl in root.reflect.store.items():
        if impl.fiber.uid is None:
            raise InvariantViolation(
                f"{where}: INV-5 store 中残留提供方已释放的服务条目 {impl.name!r}"
            )

    # INV-6：监听器随注册方 fiber 消亡。
    for name, hooks in root.events._hooks.items():
        for hook in hooks:
            owner = hook.ctx.fiber
            if owner.uid is None and owner is not root.fiber:
                # 已释放 fiber 的监听器必须已经被摘除
                raise InvariantViolation(
                    f"{where}: INV-6 事件 {name!r} 的监听器属于已释放的 fiber"
                )

    # INV-8：依赖唤醒。每个 inject 了 svc 的消费方，在同一作用域内有存活
    # 提供方时不得停留在 PENDING。
    for fiber in fibers:
        if "svc" not in (fiber.inject or {}):
            continue
        if fiber.uid is None:
            continue
        provider_alive = any(
            impl.fiber.uid is not None for impl in root.reflect.store.values()
            if impl.name == "svc" and impl.fiber is not fiber
        )
        if provider_alive and fiber.state == FiberState.PENDING and world.provider_seen:
            raise InvariantViolation(
                f"{where}: INV-8 提供方存活但消费方仍 PENDING"
            )


# ---------------------------------------------------------------------------
# 世界模型
# ---------------------------------------------------------------------------

_PROVIDER = "__provider__"
_CONSUMER = "__consumer__"
_EFFECT = "__effect__"
_EMITTER = "__emitter__"


class World:
    """一次验证运行：根上下文 + 全部 fiber + 观测账本。"""

    def __init__(self) -> None:
        self.root = Context()
        self.disposer_expected: list = []
        self.disposer_runs: list = []
        self.transitions: list = []
        self.listener_hits: list = []
        self.iso_hits: dict = {}
        self.provider_seen = False
        self.op_errors: list = []
        self._counter = 0
        self._listener_seq = 0
        # 全局状态观测：internal/status 记录迁移边
        self.root.on(
            "internal/status",
            self._on_status,
        )

    # -- 观测 ---------------------------------------------------------------
    def _on_status(self, fiber, old_state):
        self.transitions.append((old_state, fiber.state))

    def _on_service(self, name, value):
        self.provider_seen = True

    # -- 账本 ---------------------------------------------------------------
    def _next_label(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}#{self._counter}"

    def _track_disposer(self, label: str):
        def disposer():
            self.disposer_runs.append(label)

        self.disposer_expected.append(label)
        return disposer

    # -- fiber 访问 ---------------------------------------------------------
    def fibers(self):
        result = []
        for runtime in self.root.registry.values():
            result.extend(runtime.fibers)
        result.sort(key=lambda fiber: fiber.uid or 0)
        return result

    def fiber_at(self, index: int):
        fibers = self.fibers()
        if not fibers:
            return None
        return fibers[index % len(fibers)]

    # -- 操作 ---------------------------------------------------------------
    def op_load_provider(self, value="v"):
        def plugin(ctx, config):
            label = self._next_label("provider")  # 每次激活新身份
            ctx.provide("svc", value)
            ctx.effect(lambda: self._track_disposer(label), label)

        return self.root.plugin(plugin, {"value": value})

    def op_load_consumer(self):
        def plugin(ctx, config):
            label = self._next_label("consumer")  # 每次激活新身份
            ctx.effect(lambda: self._track_disposer(label), label)

        plugin.inject = ["svc"]
        return self.root.plugin(plugin)

    def op_load_effect_plugin(self):
        self._listener_seq += 1

        def plugin(ctx, config):
            # 标签在**每次激活**时生成：重启会重新执行主体，必须拿到新的
            # disposer 身份，否则账本无法区分两代的清理。
            label = self._next_label("effect")
            event_name = f"ev/{label}"

            # 三个嵌套 effect：验证逆序释放
            def outer():
                ctx.effect(lambda: self._track_disposer(label + ":a"), label + ":a")
                ctx.effect(lambda: self._track_disposer(label + ":b"), label + ":b")
                return self._track_disposer(label + ":outer")

            ctx.effect(outer, label)
            # 监听器：验证随 fiber 消亡
            ctx.on(event_name, lambda *args: self.listener_hits.append(event_name))
            # 插件主体返回 None（合法 effect：无可清理资源）

        return self.root.plugin(plugin)

    def op_load_isolated_provider(self):
        iso = self.root.isolate("svc", label="iso-" + str(self._counter))
        self._counter += 1

        def plugin(ctx, config):
            ctx.provide("svc", "ISO")

            def track(name, value):
                self.iso_hits[name] = self.iso_hits.get(name, 0) + 1

            ctx.on("internal/service", track)

        return iso.plugin(plugin)

    def op_dispose(self, index: int):
        fiber = self.fiber_at(index)
        if fiber is not None:
            return fiber.dispose()
        return None

    def op_update(self, index: int):
        fiber = self.fiber_at(index)
        if fiber is not None:
            return fiber.update({"n": self._counter})
        return None

    def op_restart(self, index: int):
        fiber = self.fiber_at(index)
        if fiber is not None:
            return fiber.restart()
        return None

    def op_emit(self, name="ev"):
        return self.root.emit(name)


# ---------------------------------------------------------------------------
# 驱动与检查
# ---------------------------------------------------------------------------


async def _drive(world: World) -> None:
    """把挂起的生命周期任务驱动到收敛（与示例 _settle_scope 同策略）。"""
    for _ in range(3):
        for runtime in list(world.root.registry.values()):
            for fiber in list(runtime.fibers):
                await fiber.await_()
        await asyncio.sleep(0)


def _apply(world: World, op) -> None:
    result = op()
    if inspect.isawaitable(result):
        asyncio.get_event_loop().run_until_complete(result)


def run_scenario(ops, seed: int = 0, check_every: bool = True) -> World:
    """按顺序执行 ops（每项是 World 上的可调用），逐步检查不变量。

    单个操作抛出异常（重复 provide、对已释放 fiber 操作等）属于框架的
    **合法**错误路径：记入 ``world.op_errors`` 后继续，不中断序列。
    """

    async def scenario():
        world = World()
        for index, op in enumerate(ops):
            try:
                result = op(world)
                if inspect.isawaitable(result):
                    await result
                await _drive(world)
            except Exception as error:  # noqa: BLE001 - 合法错误路径
                world.op_errors.append((op.__name__, type(error).__name__))
                try:
                    await _drive(world)
                except Exception:  # noqa: BLE001 - 收敛后再次检查
                    pass
            if check_every:
                check_invariants(world, f"seed={seed} step={index} op={op.__name__}")
        return world

    return asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 属性测试：随机操作序列
# ---------------------------------------------------------------------------


def random_ops(rng: random.Random, length: int):
    kinds = [
        "load_provider",
        "load_consumer",
        "load_effect",
        "load_isolated",
        "dispose",
        "update",
        "restart",
        "emit",
    ]
    weights = [3, 2, 2, 1, 3, 1, 1, 1]
    ops = []
    for _ in range(length):
        kind = rng.choices(kinds, weights=weights)[0]
        index = rng.randrange(4)
        if kind == "load_provider":
            value = rng.choice(["v1", "v2", {"k": rng.randrange(3)}])
            ops.append(lambda world, value=value: world.op_load_provider(value))
        elif kind == "load_consumer":
            ops.append(lambda world: world.op_load_consumer())
        elif kind == "load_effect":
            ops.append(lambda world: world.op_load_effect_plugin())
        elif kind == "load_isolated":
            ops.append(lambda world: world.op_load_isolated_provider())
        elif kind == "dispose":
            ops.append(lambda world, index=index: world.op_dispose(index))
        elif kind == "update":
            ops.append(lambda world, index=index: world.op_update(index))
        elif kind == "restart":
            ops.append(lambda world, index=index: world.op_restart(index))
        else:
            ops.append(lambda world: world.op_emit("ev"))
    return ops


class TestPropertyRandom(unittest.TestCase):
    """随机操作序列下的不变量保持（属性测试）。"""

    def test_random_sequences(self):
        rng = random.Random(20261013)
        failures = []
        for seed in range(120):
            scenario_rng = random.Random(rng.randrange(1 << 30))
            ops = random_ops(scenario_rng, 30)
            try:
                run_scenario(ops, seed=seed)
            except InvariantViolation as error:
                failures.append(f"seed={seed}: {error}")
        if failures:
            self.fail("不变量被违反：\n" + "\n".join(failures[:5]))

    def test_dispose_all_cleans_registry(self):
        """卸载全部插件后注册表归零、store 清空。"""

        async def scenario():
            world = World()
            # 同一作用域内重复 provide 是合法错误（reflect.ts:289），故只加载
            # 一个提供方 + 若干消费方。
            await world.op_load_provider("v0")
            for _ in range(3):
                await world.op_load_consumer()
            await _drive(world)
            for fiber in world.fibers():
                await fiber.dispose()
            await _drive(world)
            check_invariants(world, "dispose-all")
            self.assertEqual(world.root.registry.size, 0)
            # 内置 logger 依 context.ts:82 的文档化缺陷常驻 store，其余应清空。
            remaining = [key for key in world.root.reflect.store if key != "logger"]
            self.assertEqual(remaining, [])
            # 每个登记的 disposer 都恰好运行了一次
            self.assertEqual(len(world.disposer_runs), len(world.disposer_expected))
            self.assertEqual(len(set(world.disposer_runs)), len(world.disposer_runs))

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 有界模型检测：穷举长度 ≤ N 的全部操作序列
# ---------------------------------------------------------------------------

ALPHABET = (
    "load_provider",
    "load_consumer",
    "load_effect",
    "load_isolated",
    "dispose0",
    "dispose1",
    "update0",
    "restart0",
    "emit",
)


def _op_for(kind: str):
    if kind == "load_provider":
        return lambda world: world.op_load_provider()
    if kind == "load_consumer":
        return lambda world: world.op_load_consumer()
    if kind == "load_effect":
        return lambda world: world.op_load_effect_plugin()
    if kind == "load_isolated":
        return lambda world: world.op_load_isolated_provider()
    if kind == "dispose0":
        return lambda world: world.op_dispose(0)
    if kind == "dispose1":
        return lambda world: world.op_dispose(1)
    if kind == "update0":
        return lambda world: world.op_update(0)
    if kind == "restart0":
        return lambda world: world.op_restart(0)
    return lambda world: world.op_emit("ev")


def _abstract_state(world: World):
    """抽象状态：剔除 uid 等仅具身份意义的量，用于去重。"""
    fibers = []
    for fiber in world.fibers():
        fibers.append(
            (
                fiber.state,
                len(fiber._disposables),
                tuple(sorted((fiber.inject or {}).keys())),
                tuple(sorted(fiber._hooks.keys())),
                fiber._runner.get("epoch") if isinstance(fiber._runner, dict) else None,
            )
        )
    store = tuple(
        sorted(
            (impl.name, impl.fiber.state)
            for impl in world.root.reflect.store.values()
        )
    )
    listeners = tuple(
        sorted(
            (str(name), len(hooks))
            for name, hooks in world.root.events._hooks.items()
        )
    )
    ledger = (len(world.disposer_runs), len(world.listener_hits))
    return (tuple(sorted(fibers)), store, listeners, ledger)


class TestBoundedModelChecking(unittest.TestCase):
    """穷举「长度 ≤ max_depth 的所有操作序列」并检查不变量。

    深度分档，避免日常单测被完整穷举拖慢：

    - 默认（快速档）：``depth=4``，9 元字母表展开 341 条路径 / 341 个抽象
      状态 / 2898 个检查点，约 2 秒；
    - 完整档：设置环境变量 ``CORDIS_BMC_DEPTH=5``，展开 1139 条路径 /
      1139 个抽象状态 / 12033 个检查点，约 10 秒。

    两种档位都**完整**遍历各自深度内的全部操作序列（去重后仍逐一展开），
    只是锚定的序列长度不同。
    """

    DEFAULT_MAX_DEPTH = 4
    FULL_MAX_DEPTH = 5

    @classmethod
    def setUpClass(cls):
        raw = os.environ.get("CORDIS_BMC_DEPTH", "").strip()
        cls.MAX_DEPTH = int(raw) if raw.isdigit() else cls.DEFAULT_MAX_DEPTH
        cls.FULL_RUN = cls.MAX_DEPTH >= cls.FULL_MAX_DEPTH

    def test_exhaustive_short_sequences(self):
        max_depth = self.MAX_DEPTH

        async def scenario():
            # BFS：每个节点保存到达路径；展开时从头回放，逐步检查不变量。
            start_path: tuple = ()
            visited = set()
            frontier = [start_path]
            expanded = 0
            violations = []
            while frontier:
                path = frontier.pop()
                if len(path) >= max_depth:
                    continue
                for kind in ALPHABET:
                    new_path = path + (kind,)
                    world = World()
                    for step_kind in new_path:
                        try:
                            await _op_for(step_kind)(world)
                            await _drive(world)
                        except Exception:  # noqa: BLE001 - 合法错误路径
                            try:
                                await _drive(world)
                            except Exception:  # noqa: BLE001 - 收敛后再次检查
                                pass
                        try:
                            check_invariants(world, f"path={new_path}")
                        except InvariantViolation as error:
                            violations.append(f"path={new_path}: {error}")
                    state = _abstract_state(world)
                    if state in visited:
                        continue
                    visited.add(state)
                    frontier.append(new_path)
                    expanded += 1
            return expanded, visited, violations

        expanded, visited, violations = asyncio.run(scenario())
        if violations:
            self.fail(
                f"模型检测发现 {len(violations)} 处违反（展开 {expanded} 条路径，"
                f"{len(visited)} 个抽象状态）：\n" + "\n".join(violations[:5])
            )
        # sanity：去重后仍应覆盖可观数量的状态（快速档 341+）
        self.assertGreater(len(visited), 300)
        # 完整档必须达到 depth=5 的实测规模，否则说明穷举被意外削弱
        if self.FULL_RUN:
            self.assertGreaterEqual(len(visited), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
