"""插件 fiber 的生命周期、effect 与配置校验。

``src/fiber.ts`` 的 Python 移植。状态机、epoch/inertia 并发控制以及
effect 归属规则都逐行照搬参考实现；每一处 ``_setEpoch`` 提前返回和
每一次 ``inertia`` 交接的位置都有意义，均原样保留。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Dict, Iterable, List, Optional, TYPE_CHECKING

from .utils import DisposableList, Tracker, define_property, is_nullable, is_object, symbols

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context
    from .registry import Plugin

__all__ = [
    "ValidationError",
    "resolve_config",
    "Disposable",
    "Effect",
    "EffectMeta",
    "FiberState",
    "CordisError",
    "INACTIVE",
    "Fiber",
]


class ValidationError(TypeError):
    """当插件配置未通过 standard-schema 校验时抛出的错误。"""

    name = "ValidationError"

    def __init__(self, issues: Iterable[Any]) -> None:
        lines = ["invalid config:"]
        for issue in issues:
            message = getattr(issue, "message", None) or (issue.get("message") if isinstance(issue, dict) else str(issue))
            path = getattr(issue, "path", None) or (issue.get("path") if isinstance(issue, dict) else None)
            if path:
                lines.append(f"  - {message} (at {'.'.join(str(p) for p in path)})")
            else:
                lines.append(f"  - {message}")
        super().__init__("\n".join(lines))


ValidationError.__cordis_validation_error__ = True  # type: ignore[attr-defined]


def resolve_config(runtime: "Plugin.Runtime", config: Any) -> Any:
    """在插件运行时启动之前，对其配置做校验与归一化。"""
    if getattr(runtime, "Config", None) is None:
        return config
    result = runtime.Config(config)
    if inspect.isawaitable(result):
        raise TypeError("Async config validation is not supported")
    if isinstance(result, dict) and "issues" in result and result["issues"]:
        raise ValidationError(result["issues"])
    if isinstance(result, dict) and "value" in result:
        return result["value"]
    return result


Disposable = Callable[[], Any]
Effect = Any


class EffectMeta:
    """用于对外暴露嵌套 effect 标签的树节点，服务于诊断。"""

    __slots__ = ("label", "children")

    def __init__(self, label: str, children: Optional[List["EffectMeta"]] = None) -> None:
        self.label = label
        self.children = children if children is not None else []

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"EffectMeta({self.label!r}, children={len(self.children)})"


# 单个插件 fiber 的生命周期状态。
PENDING = 0
LOADING = 1
ACTIVE = 2
FAILED = 3
DISPOSED = 4
UNLOADING = 5


class FiberState:
    """与 ``fiber.ts`` 中 ``FiberState`` 枚举对应的命名空间。"""

    PENDING = PENDING
    LOADING = LOADING
    ACTIVE = ACTIVE
    FAILED = FAILED
    DISPOSED = DISPOSED
    UNLOADING = UNLOADING

    NAMES = {
        PENDING: "PENDING",
        LOADING: "LOADING",
        ACTIVE: "ACTIVE",
        FAILED: "FAILED",
        DISPOSED: "DISPOSED",
        UNLOADING: "UNLOADING",
    }

    @classmethod
    def name_of(cls, state: int) -> str:
        return cls.NAMES.get(state, str(state))


class CordisError(Exception):
    """带稳定机器可读 code 的框架错误。"""

    Code = {
        "INACTIVE_EFFECT": "cannot create effect on inactive context",
    }

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        self.code = code
        super().__init__(message or CordisError.Code.get(code, code))


# 哨兵：`INACTIVE` 表示「依赖未满足」的 epoch；`''` 表示「没有 inject」。
INACTIVE = "__INACTIVE__"

# 结构持有者与外部 effect 仍必须能够加入其他调用方已启动的清理。Python 没有
# 可用于任意闭包的 WeakMap，因此 inertia 表以对象 id 为键，并用哨兵属性做
# 守卫，确保垃圾回收后 id 被复用时绝不会错配查找结果。
_EFFECT_INERTIA_ATTR = "__cordis_effect_inertia__"
_effect_inertia: Dict[int, Callable[[], Any]] = {}


def _run_disposable(dispose: Callable[[], Any]) -> Any:
    # 忠实于 `runDisposable`（fiber.ts:114）：disposer 总是会执行；inertia
    # promise 只在其后才被查询，这样结构持有者才能加入其他调用方已经启动的
    # 清理。``_effect_inertia`` 以 ``id`` 为键，并用哨兵属性做守卫，确保垃圾
    # 回收后 id 被复用时绝不会错配查找结果。
    result = dispose()
    if getattr(dispose, _EFFECT_INERTIA_ATTR, None):
        inertia = _effect_inertia.get(id(dispose))
        if inertia is not None:
            # 对应 `inertia() ?? result`：只有 null/undefined 才回退，
            # 0 / '' / False 都是真实结果，不能被吞掉。
            value = inertia()
            return result if value is None else value
    return result


class Fiber:
    """单个插件应用的运行时实例。"""

    def __init__(
        self,
        parent: "Context",
        config: Any,
        inject: Dict[str, Any],
        runtime: Optional["Plugin.Runtime"],
        get_outer_stack: Callable[[], List[str]],
    ) -> None:
        self.parent = parent
        self.inject = inject
        self.runtime = runtime
        self._config = config
        self.config = None
        self.state = FiberState.PENDING
        self.store: Optional[Dict[str, Any]] = None
        self.inertia: Optional[Any] = None
        self._hooks: Dict[str, DisposableList] = {}
        self._disposables: DisposableList = DisposableList()
        self._error: Any = None
        self._store: Dict[str, Any] = {}
        self._pending: List[Any] = []
        self.uid: Optional[int] = None
        self.ctx: "Context"
        self.context: "Context"
        self.dispose: Callable[[], Any]
        self._runner: Dict[str, Any]

        if runtime is not None:
            self.uid = parent.registry.counter
            self.ctx = self.context = parent.extend({"fiber": self})

            inject_entries = list(self.inject.items()) if self.inject else []
            if inject_entries:
                from .context import Context

                setattr(self.ctx, Context.INTERCEPT, dict(getattr(parent, Context.INTERCEPT)))
                intercept_map = getattr(self.ctx, Context.INTERCEPT)
                for name, icfg in inject_entries:
                    if icfg is None:
                        continue
                    intercept_map[name] = icfg

            def _collect(dispose: Disposable) -> None:
                self._disposables.push(dispose)

            fiber_ref = self

            def _execute() -> Any:
                callback = runtime.callback
                if isinstance(callback, type):
                    instance = callback(fiber_ref.ctx, fiber_ref.config)
                    for hook in getattr(instance, symbols.init_hooks, None) or []:
                        hook()
                    init = getattr(instance, symbols.init, None)
                    return init() if init is not None else None
                return callback(fiber_ref.ctx, fiber_ref.config)

            self._runner = {
                "epoch": INACTIVE,
                "get_outer_stack": get_outer_stack,
                "execute": _execute,
                "collect": _collect,
            }

            self.dispose = parent.fiber.effect(self._make_disposer(), "ctx.plugin()")

            try:
                self.context.emit("internal/plugin", self)
            except Exception as error:
                # 观察者抛错也必须把已启动的 fiber 完整拆卸掉：catch 链必须被
                # 真正驱动，否则拆卸协程永不执行、fiber 停在半释放状态。
                _drive_catch(self.dispose, lambda reason: self.ctx.logger.error(reason))
                raise error

            if self.uid is not None and parent.fiber.state != FiberState.UNLOADING:
                for name in list(self.inject.keys()):
                    self._check_impl(name)
                self._refresh()
        else:
            self.uid = 0
            self.ctx = self.context = parent
            self.state = FiberState.ACTIVE
            self.store = {}
            self._runner = {
                "epoch": "",
                "get_outer_stack": get_outer_stack,
                "execute": lambda: None,
                "collect": lambda dispose: self._disposables.push(dispose),
            }
            self.dispose = self.restart

    # -- 释放 ----------------------------------------------------------------
    def _make_disposer(self) -> Callable[[], Any]:
        """构造持有本 fiber 生命周期的 ``parent.fiber.effect`` 主体。"""
        fiber = self

        def _setup() -> Callable[[], Any]:
            remove = fiber.runtime.fibers.push(fiber)

            async def _teardown() -> None:
                fiber.uid = None
                _emit_plugin_disposed(fiber.context, fiber)
                if fiber.ctx.registry.has(fiber.runtime.callback):
                    remove()
                    if not len(fiber.runtime.fibers):
                        fiber.ctx.registry.delete(fiber.runtime.callback)
                fiber._set_epoch(INACTIVE)
                # 一个 PENDING 状态的 fiber 可能已经持有由 internal/plugin
                # 观察者注册的 effect。此时它的 epoch 仍是 INACTIVE，
                # _set_epoch 没有可驱动的状态迁移；因此在报告释放完成之前，
                # 显式卸载这些在激活之前产生的工作。
                if not fiber.inertia:
                    fiber._update_state(lambda: _start_unload(fiber))
                # `inertia` 按构造约定从不 reject；如果连 logger 本身都失败了，
                # 也就没有可用于恢复的手段了，直接让它抛出。
                while fiber.inertia:
                    await _settle(fiber.inertia)

            return _teardown

        return _setup

    # -- 命名 ----------------------------------------------------------------
    @property
    def name(self) -> str:
        """插件的展示名；对根 fiber 返回 ``'root'``。"""
        fiber: Fiber = self
        while True:
            if fiber.runtime is not None and getattr(fiber.runtime, "name", None):
                return fiber.runtime.name
            if fiber.parent is None or fiber.parent.fiber is fiber:
                break
            fiber = fiber.parent.fiber
        return "root"

    def assert_active(self) -> None:
        if self.uid is not None:
            return
        raise CordisError("INACTIVE_EFFECT")

    # -- effect 机制 ---------------------------------------------------------
    def _execute(self, runner: Dict[str, Any]) -> Any:
        old_epoch = runner["epoch"]

        def _callback(info: Dict[str, Any]) -> Any:
            def _safe_collect(dispose: Any) -> None:
                if callable(dispose):
                    runner["collect"](dispose)
                elif not is_nullable(dispose):
                    raise TypeError("Invalid effect")

            effect = runner["execute"]()
            if callable(effect) and not inspect.isgenerator(effect) and not inspect.isasyncgen(effect):
                return runner["collect"](effect)
            if is_nullable(effect):
                return None
            # 生成器是 JS 中「可迭代/异步可迭代」effect 形态的 Python 对应物：
            # 每产出一个 disposer 就立即注册。
            if inspect.isgenerator(effect):
                info["error"] = RuntimeError("effect")
                for item in effect:
                    _safe_collect(item)
                return None
            if inspect.isasyncgen(effect):
                return _drain_async_gen(effect, _safe_collect, runner, old_epoch)
            if not is_object(effect) and not inspect.isawaitable(effect):
                raise TypeError("Invalid effect")
            if inspect.isawaitable(effect):
                return _await_and_collect(effect, _safe_collect)
            if hasattr(effect, "__iter__"):
                info["error"] = RuntimeError("effect")
                for item in effect:
                    _safe_collect(item)
                return None
            if hasattr(effect, "__aiter__"):
                return _drain_async_gen(effect.__aiter__(), _safe_collect, runner, old_epoch)
            raise TypeError("Invalid effect")

        from .utils import compose_error

        return compose_error(_callback, runner["get_outer_stack"])

    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Callable[..., Any]:
        """在本 fiber 上注册一个可感知清理的 effect。

        ``execute`` 会立即执行；它产出的 disposer 会被收集起来，并在返回的
        disposer 被调用时、或 fiber 卸载时（以先发生者为准）按逆序执行。
        """
        self.assert_active()
        if self.state == FiberState.UNLOADING:
            raise CordisError("INACTIVE_EFFECT")

        disposables: List[Disposable] = []
        disposing = False
        disposal_task: Any = None

        def dispose() -> Any:
            nonlocal disposing, disposal_task
            if disposing:
                return disposal_task
            disposing = True
            task: Any = None
            for disposable in list(reversed(disposables[:])):
                if task is not None:
                    task = _chain(task, disposable)
                else:
                    result = _run_disposable(disposable)
                    # 只有 thenable 结果才会成为链尾，对应 fiber.ts:435 中的
                    # `isObject(result) && 'then' in result`。
                    if inspect.isawaitable(result):
                        task = result
            disposables.clear()
            disposal_task = task
            return task

        meta = EffectMeta(label)
        epoch_state = {"value": True}
        in_flight: Dict[str, Any] = {"value": None}
        executing = True
        setup_failed = False
        setup_state: Dict[str, Any] = {"barrier": None, "resolve": None, "reject": None}
        remove_wrapper: Dict[str, Any] = {"value": lambda: False}

        def _collect(disposable: Disposable) -> None:
            disposables.append(disposable)
            self._disposables.delete(disposable)
            nested = getattr(disposable, symbols.effect, None)
            if nested is not None:
                meta.children.append(nested)

        runner: Dict[str, Any] = {
            "execute": execute,
            "epoch": True,
            "collect": _collect,
            "get_outer_stack": _noop_stack,
        }

        def _wait_for_setup() -> Any:
            """effect 主体的 setup 一落定就 resolve。"""
            if setup_state["barrier"] is None:
                setup_state["barrier"] = asyncio.get_event_loop().create_future()
                setup_state["resolve"] = setup_state["barrier"].set_result
                setup_state["reject"] = setup_state["barrier"].set_exception
                if not executing:
                    setup_state["resolve"](None)
            return setup_state["barrier"]

        def _dispose_after(setup: Any) -> Any:
            """JS 的 ``disposeAfter``：等 ``setup`` 落定后再释放。

            当 ``setup`` 已经落定时，释放**同步**执行，与参考实现中的
            ``Disposable<Promise<void>>`` 完全一致：无需 await 返回的
            thenable 就能观察到清理已发生。
            """
            if not inspect.isawaitable(setup):
                return dispose()

            async def _runner() -> Any:
                try:
                    await setup
                except Exception as reason:
                    outcome = dispose()
                    if inspect.isawaitable(outcome):
                        await outcome
                    raise reason
                outcome = dispose()
                if inspect.isawaitable(outcome):
                    await outcome
                return None

            return _runner()

        def _finalize(callback: Callable[[], Any]) -> Any:
            try:
                result = callback()
            except Exception:
                remove_wrapper["value"]()
                raise
            if inspect.isawaitable(result):
                # 始终交回一个可 await 的包装，这样调用方可以统一写成
                # `await dispose()` / `asyncio.run(dispose())`，对应 JS
                # effect disposer 的 thenable 契约（fiber.ts:415）。
                pending = _settle(result)
                in_flight["value"] = pending
                return pending
            # 同步拆卸：JS 的 `finalizeDisposal` 会移除包装并返回裸结果（不带
            # promise），因此调用方立刻就能观察到该 effect 已消失。
            remove_wrapper["value"]()
            return result

        def wrapper() -> Any:
            # 同步的 setup 失败可能与「已经捕获该 wrapper 但尚未调用」的所有者
            # 卸载发生竞态。当 effect 已被释放时，JS disposer 返回
            # `undefined`；这里用 `None` 对应，而不是分配一个一次性协程。
            if not epoch_state["value"]:
                return in_flight["value"] if setup_failed else None
            epoch_state["value"] = False
            if executing:
                return _finalize(lambda: _dispose_after(_wait_for_setup()))
            # `task` 是尚在途中的 setup 任务；只有它本身可 await 时才需要走
            # 异步的 dispose-after 路径。同步落定的情形立即释放。
            if task is not None and inspect.isawaitable(task):
                return _finalize(lambda: _dispose_after(task))
            return _finalize(dispose)

        setattr(wrapper, symbols.effect, meta)
        setattr(wrapper, _EFFECT_INERTIA_ATTR, True)
        _effect_inertia[id(wrapper)] = lambda: in_flight["value"]

        _push_remove = self._disposables.push(wrapper)

        def _remove() -> bool:
            """注销 wrapper 并清除它的 inertia 表项。"""
            removed = _push_remove()
            if removed:
                _effect_inertia.pop(id(wrapper), None)
            return removed

        remove_wrapper["value"] = _remove
        try:
            task = self._execute(runner)
        except Exception as reason:
            executing = False
            setup_failed = True
            epoch_state["value"] = False
            try:
                cleanup = _finalize(dispose)
            finally:
                if setup_state["reject"] is not None:
                    setup_state["reject"](reason)
            if inspect.isawaitable(cleanup):
                _spawn(_settle_catch(cleanup, lambda error: self.ctx.logger.error(error)))
            raise reason

        executing = False
        if setup_state["barrier"] is not None:
            if setup_state["resolve"] is not None:
                setup_state["resolve"](None)

        def _on_task_failure(_error: Any) -> Any:
            if not epoch_state["value"]:
                return None
            return _finalize(dispose)

        if inspect.isawaitable(task):

            async def _guard() -> None:
                try:
                    await task
                except Exception as error:  # noqa: BLE001 - 卸载 effect 后仅记录
                    try:
                        outcome = _on_task_failure(error)
                        if inspect.isawaitable(outcome):
                            await outcome
                    except Exception as cleanup_error:  # pragma: no cover - 防御性
                        self.ctx.logger.error(cleanup_error)
                    self.ctx.logger.error(error)

            spawned = _spawn(_guard())
            if spawned is not None:
                self._pending.append(spawned)

        def _dispose_async() -> Any:
            if not epoch_state["value"]:
                return None
            epoch_state["value"] = False
            return _finalize(dispose)

        async def _then(on_fulfilled: Any = None, on_rejected: Any = None) -> Any:
            try:
                if inspect.isawaitable(task):
                    await task
                result = _dispose_async()
                if inspect.isawaitable(result):
                    await result
                return on_fulfilled(result) if on_fulfilled else None
            except Exception as error:  # noqa: BLE001 - forwarded to the caller
                if on_rejected:
                    return on_rejected(error)
                raise

        setattr(wrapper, "then", _then)
        return wrapper

    def get_effects(self) -> List[EffectMeta]:
        """返回当前已注册 effect 的元信息。"""
        result: List[EffectMeta] = []
        for dispose in self._disposables:
            meta = getattr(dispose, symbols.effect, None)
            if meta is not None:
                result.append(meta)
        return result

    # -- 状态机 --------------------------------------------------------------
    def _get_state(self) -> int:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error:
            return FiberState.FAILED
        if self._runner["epoch"] != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback: Callable[[], Any]) -> None:
        old_state = self.state
        result = callback()
        self.state = result if result is not None else self._get_state()
        if old_state == self.state:
            return
        self.context.emit("internal/status", self, old_state)
        # 只在 ACTIVE 与非 ACTIVE 之间变化时才通知。
        if old_state != FiberState.ACTIVE and self.state != FiberState.ACTIVE:
            return
        for key in list(self.ctx.reflect.store.keys()):
            impl = self.ctx.reflect.store[key]
            if impl.fiber is not self:
                continue
            self.ctx.reflect.notify([impl.name], scope_ctx=self.ctx)

    def _check_impl(self, name: str) -> None:
        impl = self.ctx.reflect._get_impl(name, True, self.ctx)
        if impl is None:
            self._store.pop(name, None)
            return
        if impl.check is not None:
            try:
                if not impl.check():
                    self._store.pop(name, None)
                    return
            except Exception as error:  # noqa: BLE001 - delegated to the provider log
                impl.fiber.ctx.logger.error(error)
                self._store.pop(name, None)
                return
        self._store[name] = impl

    def _refresh(self) -> None:
        # 注意：fiber.ts:612-613 里 `epoch = false; epoch = ''` 那套写法在原始
        # 实现中是死代码；实际生效的初始值就是空字符串。
        epoch: str = ""
        for name in list(self.inject.keys()):
            impl = self._store.get(name)
            if impl is None:
                epoch = INACTIVE
                break
            epoch += ":" + str(impl.fiber.uid)
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: str) -> None:
        old_epoch = self._runner["epoch"]
        if epoch == old_epoch:
            return
        self._runner["epoch"] = epoch
        # 全框架唯一的并发闸门：迁移在途时只写 epoch，由在途任务在收尾时
        # 统一裁决下一步。
        if self.inertia:
            return
        self._update_state(lambda: _drive_epoch(self, epoch, old_epoch))

    async def _reload(self) -> None:
        self.store = dict(self._store)
        old_epoch = self._runner["epoch"]
        try:
            await asyncio.sleep(0)
            if self._runner["epoch"] == old_epoch:
                self.config = self._resolve_config(self._config)
                result = self._execute(self._runner)
                if inspect.isawaitable(result):
                    await result
                self._error = None
        except Exception as reason:  # noqa: BLE001 - 经 logger 上报
            self.ctx.logger.error(reason)
            self._error = reason
            self._runner["epoch"] = INACTIVE
        self._update_state(lambda: _settle_reload(self, old_epoch))

    async def _unload(self) -> None:
        values = self._disposables.clear()
        await asyncio.gather(*[_run_unload_one(self, dispose) for dispose in values])
        self.store = None
        self._update_state(lambda: _settle_unload(self))

    async def await_(self) -> "Fiber":
        """等待当前生命周期工作完成，并重新抛出启动阶段的错误。"""
        while self.inertia:
            try:
                await self.inertia
            except Exception:  # noqa: BLE001 - `_reload`/`_unload` 会吞掉任务错误
                break
        if self._pending:
            pending, self._pending = self._pending, []
            await asyncio.gather(*pending, return_exceptions=True)
        if self._error:
            raise self._error
        return self

    async def restart(self) -> None:
        """用当前配置释放并立即重新加载本插件。"""
        self.assert_active()
        self._set_epoch(INACTIVE)
        self._refresh()
        await self.await_()

    def update(self, config: Any, no_save: bool = False) -> Any:
        """校验并应用新配置，然后重启插件。

        返回**始终可 await**：PENDING/UNLOADING 等非 ACTIVE 状态下解析配置并
        重算依赖，ACTIVE 状态下先跑完本 fiber 的 ``internal/update`` 钩子再
        ``restart()``。已释放的 fiber 明确拒绝。
        """
        self.assert_active()
        self._config = config
        if self.state != FiberState.ACTIVE:
            self._error = None
            self._set_epoch(INACTIVE)
            self._refresh()
            return _settle(None)
        resolved = self._resolve_config(config)
        return _settle(self._apply_update(resolved, no_save))

    async def _apply_update(self, config: Any, no_save: bool) -> Any:
        hooks = list(self._hooks.get("internal/update") or [])

        def _next(index: int) -> Any:
            if index < len(hooks):
                return hooks[index](config, no_save, lambda *_: _next(index + 1))
            self.config = config
            self._error = None
            return self.restart()

        result = _next(0)
        if inspect.isawaitable(result):
            return await result
        return result

    # -- 内部实现 ------------------------------------------------------------
    def _resolve_config(self, config: Any) -> Any:
        config = self.context.waterfall(self, "internal/config", config, lambda: config)
        return resolve_config(self.runtime, config) if self.runtime else config

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Fiber(uid={self.uid}, name={self.name!r}, state={FiberState.name_of(self.state)})"


# -- 模块级辅助函数 ----------------------------------------------------------


def _start_unload(fiber: Fiber) -> int:
    fiber.inertia = _schedule(fiber._unload())
    return FiberState.UNLOADING


def _drive_epoch(fiber: Fiber, epoch: str, old_epoch: str) -> int:
    if epoch != INACTIVE and old_epoch == INACTIVE:
        fiber.inertia = _schedule(fiber._reload())
        return FiberState.LOADING
    fiber.inertia = _schedule(fiber._unload())
    return FiberState.UNLOADING


def _settle_reload(fiber: Fiber, old_epoch: str) -> Optional[int]:
    if fiber._runner["epoch"] == old_epoch:
        fiber.inertia = None
        return None
    fiber.inertia = _schedule(fiber._unload())
    return FiberState.UNLOADING


def _settle_unload(fiber: Fiber) -> Optional[int]:
    if fiber._runner["epoch"] == INACTIVE:
        fiber.inertia = None
        return None
    fiber.inertia = _schedule(fiber._reload())
    return FiberState.LOADING


def _schedule(awaitable: Any) -> Any:
    """有可用事件循环时，把协程转成正在运行的任务。

    在没有运行中的事件循环时（例如纯同步的构造路径），协程会原样返回，
    由 :meth:`Fiber.await_` 直接驱动它。
    """
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _settle(awaitable)
    return loop.create_task(awaitable)


def _drive_catch(dispose: Callable[[], Any], report: Callable[[BaseException], Any]) -> Any:
    """执行 ``dispose()``，失败时交给 ``report``，并把异步收尾真正驱动完。

    这是 JS 里 ``dispose().catch(report)`` 的对应物：Python 中丢弃一个协程
    不会有任何效果，因此必须显式调度。
    """
    return _spawn(_settle_catch(dispose(), report))


def _spawn(awaitable: Any) -> Any:
    """把协程真正驱动起来（有循环则建任务，否则就地跑完）。

    对应 JS 中「丢弃一个 promise」不会阻止它执行这一事实：Python 里未 await
    的协程**一行都不会执行**，因此 fire-and-forget 的路径必须显式调度。
    """
    if not inspect.isawaitable(awaitable):
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_settle(awaitable))
        return None
    return asyncio.ensure_future(awaitable)


async def _run_unload_one(fiber: Fiber, dispose: Any) -> None:
    try:
        await asyncio.sleep(0)
        result = _run_disposable(dispose)
        if inspect.isawaitable(result):
            await result
    except Exception as reason:  # noqa: BLE001 - 经 logger 上报
        try:
            fiber.ctx.logger.error(reason)
        except Exception:  # pragma: no cover - logger 失败即终局
            pass


def _emit_plugin_disposed(context: "Context", fiber: Fiber) -> None:
    """通知插件拆卸，且不因失败而破坏归属清理。"""
    args = ["internal/plugin", fiber]
    try:
        callbacks = context.events.dispatch("emit", args)
    except Exception as error:  # noqa: BLE001
        context.logger.error(error)
        return
    for callback in callbacks:
        try:
            returned = callback(*args)
            _settle_maybe(returned, context)
        except Exception as error:  # noqa: BLE001
            context.logger.error(error)


def _settle_maybe(returned: Any, context: "Context") -> None:
    if inspect.isawaitable(returned):
        _settle_catch(returned, lambda error: context.logger.error(error))


_CATCH_ATTR = "__cordis_catch__"


def _attach_catch(coro: Any, handler: Callable[[BaseException], Any]) -> Any:
    """给协程挂上 reject 处理器，并把它原样返回以便链式调用。"""
    try:
        setattr(coro, _CATCH_ATTR, handler)
    except AttributeError:  # pragma: no cover - 原生协程允许挂属性
        pass
    return coro


async def _await_with_catch(value: Any) -> Any:
    """await ``value``；失败时执行一次其挂载的 ``catch`` 处理器。"""
    handler: Optional[Callable[[BaseException], Any]] = None
    if inspect.iscoroutine(value):
        handler = getattr(value, _CATCH_ATTR, None)
    try:
        if inspect.isawaitable(value):
            return await value
        return value
    except Exception as error:  # noqa: BLE001 - 转交给处理器
        if handler is None:
            raise
        return handler(error)


def _settle(value: Any) -> Any:
    """把一个值或可 await 对象包装成真正的协程。

    ``asyncio.run`` / ``asyncio.gather`` 只接受真正的协程对象，因此这里
    始终返回协程，绝不用自定义的 awaitable 类。返回对象上的 ``catch()``
    只是兼容用空操作；请使用 :func:`_settle_catch`。
    """
    return _settle_coro(value)


async def _settle_coro(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _settle_catch(value: Any, handler: Callable[[BaseException], Any]) -> Any:
    """把 reject 处理器串接到已落定的值上（对应 JS 的 ``.catch``）。"""
    return _await_with_catch(_attach_catch(_settle_coro(value), handler))


def _chain(task: Any, disposable: Disposable) -> Any:
    async def _runner() -> Any:
        result = await _settle(task)
        outcome = _run_disposable(disposable)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome

    return _runner()


async def _await_and_collect(effect: Any, collect: Callable[[Any], None]) -> Any:
    result = await effect
    collect(result)
    return None


async def _drain_async_gen(iterator: Any, collect: Callable[[Any], None], runner: Dict[str, Any], old_epoch: Any) -> None:
    """排空一个异步生成器形态的 effect；若 epoch 已推进则中止。"""
    await asyncio.sleep(0)
    async for item in iterator:
        if runner["epoch"] != old_epoch:
            return
        collect(item)


def _noop_stack() -> List[str]:
    return []
