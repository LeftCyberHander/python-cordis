"""事件总线、派发模式与生命周期事件。

``src/events.ts`` 的 Python 移植。五种派发策略被完整保留，包括两处有意复刻的
已知缺陷：``parallel()`` 向 ``internal/dispatch`` 上报的模式是 ``'emit'``；
以及 ``_hooks`` 是普通 dict，因此由原型链派生的名字（``constructor``、
``toString``、``__proto__``）会命中原生 dict 的 dunder 成员。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .utils import DisposableList, Tracker, define_property, symbols

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context

__all__ = [
    "is_bailed",
    "DispatchMode",
    "EventOptions",
    "Hook",
    "EventsService",
    "Events",
]

DispatchMode = str


def is_bailed(value: Any) -> bool:
    """判断某个事件返回值是否应中止 bail 风格派发。

    判据是「监听器**有没有返回值**」，而不是「返回值真不真」：``None`` 与
    ``False`` 表示没有返回值，其余一律中止——包括 ``0`` / ``''`` / ``NaN``
    这类在 Python 里为假、但确实是监听器产出的值。
    """
    return value is not None and value is not False


class EventOptions:
    """``ctx.on()`` 与 ``ctx.once()`` 接受的选项。"""

    __slots__ = ("prepend", "global_")

    def __init__(self, prepend: bool = False, global_: bool = False) -> None:
        self.prepend = prepend
        self.global_ = global_

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"EventOptions(prepend={self.prepend}, global={self.global_})"


class Hook:
    """事件服务保存的监听器注册记录。"""

    __slots__ = ("ctx", "callback", "prepend", "global_")

    def __init__(self, ctx: "Context", callback: Callable[..., Any], prepend: bool = False, global_: bool = False) -> None:
        self.ctx = ctx
        self.callback = callback
        self.prepend = prepend
        self.global_ = global_

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Hook(ctx={self.ctx!r}, callback={self.callback!r})"


class _HookMap:
    """事件名 → 监听器列表的映射。

    所有访问都走显式方法，事件名（用户数据）永远不会命中映射自身的成员：
    ``"items"`` / ``"keys"`` / ``"constructor"`` 这类名字只是普通的键，
    不会被当成 ``dict`` 的方法。这对应 JS 里必须用 ``Object.create(null)``
    才能避免的原型链污染。
    """

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: Dict[Any, List["Hook"]] = {}

    def get(self, key: Any, default: Any = None) -> Any:
        return self._store.get(key, default)

    def __getitem__(self, key: Any) -> Any:
        return self._store[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._store[key] = value

    def __contains__(self, key: Any) -> bool:
        return key in self._store

    def __len__(self) -> int:
        return len(self._store)

    def items(self) -> Any:
        return self._store.items()

    def clear(self) -> None:
        self._store.clear()

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"_HookMap({list(self._store)!r})"


class EventsService:
    """事件总线，安装为 ``ctx.events`` 并混入每个上下文。

    支持并发、同步、串行、bail 与 waterfall 派发，并随所属 fiber 自动释放监听器。
    """

    def __init__(self, ctx: "Context") -> None:
        # 事件名是用户数据：用专用映射，`constructor` / `items` 这类名字
        # 不得命中映射自身的成员（对应 events.ts:132 的已知缺陷，本轮修正）。
        self._hooks: _HookMap = _HookMap()
        self.ctx = ctx
        define_property(self, symbols.tracker, Tracker(property="ctx", no_shadow=True))

        self.on("internal/listener", self._on_internal_listener)
        self.on("internal/update", self._on_internal_update, {"global": True, "prepend": True})

    # -- 内部处理器 -----------------------------------------------------------
    def _on_internal_listener(self, name: Any, listener: Any, options: EventOptions, caller: Any = None) -> Any:
        """对应 __init__ 中注册的 `internal/listener` bail 监听器。

        ``caller`` 是发起 ``ctx.on(...)`` 的上下文；钩子必须挂在它的 fiber 上，
        否则插件卸载后监听器仍然存活（把别人的 update 也拦下来）。
        """
        if name == "internal/update" and not options.global_:
            owner = caller if caller is not None else self.ctx
            fiber = owner.fiber
            hooks = fiber._hooks.get("internal/update")
            if hooks is None:
                hooks = DisposableList()
                fiber._hooks["internal/update"] = hooks
            return hooks.unshift(listener) if options.prepend else hooks.push(listener)
        return None

    def _on_internal_update(self, config: Any, no_save: bool, next_fn: Callable[[], Any]) -> Any:
        """对应 __init__ 中注册的 `internal/update` waterfall 监听器。"""
        cbs = list(self.ctx.fiber._hooks.get("internal/update") or [])

        # 参见 `waterfall`：JS 契约总是把 `(config, noSave, next)` 传给续体，
        # 因此这里接受并丢弃多余的位置参数。
        def _next(*_ignored: Any) -> Any:
            cb = cbs.pop(0) if cbs else next_fn
            return cb(config, no_save, _next)

        return _next()

    # -- 派发 -----------------------------------------------------------------
    def dispatch(self, type: str, args: List[Any]) -> List[Callable[..., Any]]:
        """为一次派发解析监听器并施加上下文过滤。

        ``args`` 会被就地消费：先视情况弹出可选的前导 ``this`` 对象（dict /
        对象 / 可调用对象），随后弹出事件名。
        """
        this_arg: Any = None
        # 对应 ``events.ts:166``：TS 用 ``typeof value === 'object'`` 判定前导
        # thisArg，因此 null、数组、普通对象、可调用对象都算——只有字符串/数字
        # 之类的原始值才不是。
        if args and (
            args[0] is None
            or isinstance(args[0], (dict, list, tuple, set))
            or hasattr(args[0], "__dict__")
            or callable(args[0])
        ):
            this_arg = args.pop(0)
        name: Any = args.pop(0)
        if not (isinstance(name, str) and name.startswith("internal/")):
            self.emit("internal/dispatch", type, name, args, this_arg)
        # 只读自有属性：`getattr` 会触发上下文的服务解析，而解析本身又要派发
        # `internal/get` —— 在 dispatch 里读过滤器会造成递归。
        filter_fn = None
        if this_arg is not None:
            namespace = getattr(this_arg, "__dict__", None)
            if isinstance(namespace, dict):
                filter_fn = namespace.get(symbols.filter)
            else:
                filter_fn = getattr(this_arg, symbols.filter, None)
        hooks = self._hooks.get(name) or []
        result: List[Callable[..., Any]] = []
        for hook in hooks:
            if hook.global_ or filter_fn is None or filter_fn(hook.ctx):
                result.append(hook.callback)
        return result

    async def parallel(self, *args: Any) -> None:
        """并发运行监听器并等待全部完成。

        派发诊断如实上报 ``'parallel'``——诊断若报错策略，排障时会误导。
        """
        arg_list = list(args)
        callbacks = self.dispatch("parallel", arg_list)
        results = await asyncio.gather(
            *(self._invoke_async(cb, arg_list) for cb in callbacks), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise ExceptionGroup("parallel dispatch failed", errors)  # type: ignore[arg-type]

    def emit(self, *args: Any) -> None:
        """同步运行监听器，不等待其返回的 awaitable。"""
        arg_list = list(args)
        for cb in self.dispatch("emit", arg_list):
            result = cb(*arg_list)
            if inspect.isawaitable(result):
                _fire_and_forget(result, self.ctx)
    async def serial(self, *args: Any) -> Any:
        """按序运行监听器并逐个等待，遇到 bail 值即停止。"""
        arg_list = list(args)
        for cb in self.dispatch("serial", arg_list):
            result = cb(*arg_list)
            if inspect.isawaitable(result):
                result = await result
            if is_bailed(result):
                return result
        return None

    def bail(self, *args: Any) -> Any:
        """同步运行监听器，遇到 bail 值即停止。"""
        arg_list = list(args)
        for cb in self.dispatch("bail", arg_list):
            result = cb(*arg_list)
            if is_bailed(result):
                return result
        return None

    def waterfall(self, *args: Any) -> Any:
        """把监听器围绕最内层 ``next`` 回调组合起来。

        派发参数中的最后一个视为最内层 ``next``。监听器由外向内运行，并携带其余
        派发参数以及各自的续体被调用；若不调用该续体，则否决链上其余部分，包括
        内置行为。
        """
        arg_list = list(args)
        cbs = self.dispatch("waterfall", arg_list)
        # `dispatch` 已消费掉前导的 `this` 与事件名。剩余的最后一个元素即调用方
        # 传入的最内层 `next`；在它之前的全部内容都会传给每个监听器。
        inner = arg_list.pop()
        call_args = list(arg_list)

        # 每一层监听器拿到**自己的**续体；续体首次调用后即锁定结果，
        # 因此重复调用既不会跳过后续监听器，也不会让内置行为执行两次。
        def _make_next(index: int) -> Callable[..., Any]:
            state: Dict[str, Any] = {"done": False, "value": None}

            def _next(*_ignored: Any) -> Any:
                if state["done"]:
                    return state["value"]
                state["done"] = True
                if index < len(cbs):
                    state["value"] = cbs[index](*(call_args + [_make_next(index + 1)]))
                else:
                    state["value"] = inner()
                return state["value"]

            return _next

        return _make_next(0)()

    async def _invoke_async(self, cb: Callable[..., Any], args: List[Any]) -> Any:
        result = cb(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    # -- 注册 -----------------------------------------------------------------
    def register(
        self,
        label: str,
        hooks: List[Hook],
        callback: Any,
        options: EventOptions,
        caller: Any = None,
    ) -> Callable[[], Any]:
        """把一条监听器记录作为 effect 存储在当前 fiber 上。"""

        def _setup() -> Callable[[], Any]:
            record = Hook(caller if caller is not None else self.ctx, callback, options.prepend, options.global_)
            if options.prepend:
                hooks.insert(0, record)
            else:
                hooks.append(record)
            return lambda: self.unregister(hooks, callback)

        return (caller if caller is not None else self.ctx).fiber.effect(_setup, label)

    def unregister(self, hooks: List[Hook], callback: Any) -> Optional[bool]:
        """移除一条已保存的监听器记录；找到并移除时返回 ``True``。"""
        for index, hook in enumerate(hooks):
            if hook.callback is callback:
                hooks.pop(index)
                return True
        return None

    def on(self, name: Any, listener: Callable[..., Any], options: Any = None, _caller: Any = None) -> Callable[[], Any]:
        """注册一个由当前 fiber 持有的监听器。"""
        if not isinstance(options, EventOptions):
            if isinstance(options, dict):
                options = EventOptions(prepend=bool(options.get("prepend")), global_=bool(options.get("global") or options.get("global_")))
            else:
                options = EventOptions(prepend=bool(options))

        caller = _caller if _caller is not None else self.ctx
        caller.fiber.assert_active()
        bound = caller.reflect.bind(listener)
        result = self.bail(caller, "internal/listener", name, bound, options, caller)
        if result:
            return result

        hooks = self._hooks.get(name)
        if hooks is None:
            hooks = []
            self._hooks[name] = hooks
        label_name = name if isinstance(name, str) else str(name)
        label = f"ctx.on({label_name!r})"
        return self.register(label, hooks, bound, options, caller)

    def once(self, name: Any, listener: Callable[..., Any], options: Any = None, _caller: Any = None) -> Callable[[], Any]:
        """注册一个在首次调用后自动释放自身的事件监听器。"""
        holder: Dict[str, Any] = {}

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            holder["dispose"]()
            return listener(*args, **kwargs)

        dispose = self.on(name, wrapper, options, _caller=_caller)
        holder["dispose"] = dispose
        return dispose


def _bind(callback: Callable[..., Any], this_arg: Any) -> Callable[..., Any]:
    """把派发的 ``this`` 绑定为首个位置参数；否则直接裸调用。"""
    if this_arg is None:
        return callback

    def _wrapped(*args: Any) -> Any:
        return callback(this_arg, *args)

    return _wrapped


def _fire_and_forget(awaitable: Any, ctx: "Context") -> None:
    async def _runner() -> None:
        try:
            await awaitable
        except Exception as error:  # noqa: BLE001 - 经由 logger 上报
            try:
                ctx.logger.error(error)
            except Exception:  # pragma: no cover - logger 失败即终止
                pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_runner())


class Events:
    """仅作文档用途的内置框架事件签名登记表。

    以数据类的形式保留，使移植后的事件名依然可被 grep，并与 ``events.ts`` 中的
    ``Events`` 接口保持同步。
    """

    INTERNAL_PLUGIN = "internal/plugin"
    INTERNAL_STATUS = "internal/status"
    INTERNAL_CONFIG = "internal/config"
    INTERNAL_SERVICE = "internal/service"
    INTERNAL_UPDATE = "internal/update"
    INTERNAL_GET = "internal/get"
    INTERNAL_SET = "internal/set"
    INTERNAL_LISTENER = "internal/listener"
    INTERNAL_DISPATCH = "internal/dispatch"
