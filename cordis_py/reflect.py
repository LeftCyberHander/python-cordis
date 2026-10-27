"""安装为 ``ctx.reflect`` 的反射与服务解析层。

``src/reflect.ts`` 的 Python 移植。JavaScript 原版在 ``Proxy`` 陷阱内部完成全部
解析；这里把同样的三种操作暴露为显式方法（``_resolve`` / ``_assign`` / ``_has``），
由 ``Context`` 从其 ``__getattr__`` / ``__setattr__`` / ``__contains__`` 钩子调用。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .utils import TraceableValue, Tracker, define_property, get_traceable, symbols

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context
    from .fiber import Fiber

__all__ = ["Property", "Accessor", "Impl", "ReflectService", "is_special_property"]

# 必须完全绕过服务解析的保留字。
RESERVED_WORDS = ("prototype", "then")


def _chain_isolate(ctx: Any) -> Dict[str, Any]:
    """解析 ``ctx`` 的有效 isolate 映射。

    沿 ``__cordis_proto__`` 链由外向内行走，使祖先上后来创建的 label（例如由
    根作用域的 ``provide()`` 创建）对更早创建的后代仍然可见。若不做这一步，一个
    在根默认值出现之前构建的插件上下文会算出 ``None`` 键，从而丢掉自己的服务注册。
    """
    layers: List[Dict[str, Any]] = []
    current = ctx
    seen = 0
    while current is not None and seen < 512:
        namespace = getattr(current, "__dict__", None)
        if isinstance(namespace, dict) and symbols.isolate in namespace:
            layers.append(namespace[symbols.isolate])
        current = namespace.get("__cordis_proto__") if isinstance(namespace, dict) else None
        seen += 1
    merged: Dict[str, Any] = {}
    # `layers` 由外向内排列；最内层（ctx 自身的）层必须胜出。
    for layer in reversed(layers):
        for key, value in layer.items():
            if key == "__cordis_parent_isolate__":
                continue
            merged[key] = value
    return merged


# 用于区分「属性不存在」与「属性存在且为 None」的哨兵。
_ABSENT = object()


def _fiber_of(ctx: Any) -> Any:
    """沿 ``__cordis_proto__`` 链取最近的 ``fiber``（对应 JS 的原型查找）。"""
    current = ctx
    while current is not None:
        namespace = getattr(current, "__dict__", None)
        if isinstance(namespace, dict) and "fiber" in namespace:
            return namespace["fiber"]
        current = namespace.get("__cordis_proto__") if isinstance(namespace, dict) else None
    raise AttributeError("fiber")



def _lookup_own(obj: Any, prop: str) -> Any:
    """沿 ``__cordis_proto__`` 链查找自有属性。

    在不触发 ``Context.__getattr__`` 的前提下模拟 ``Reflect.has`` 语义：绕过
    Python 自身的 ``object.__getattribute__``，改为直接扫描 ``__dict__``，
    使反射层保持非递归。
    """
    current = obj
    seen = 0
    while current is not None and seen < 512:
        namespace = getattr(current, "__dict__", None)
        if isinstance(namespace, dict) and prop in namespace:
            return namespace[prop]
        current = namespace.get("__cordis_proto__") if isinstance(namespace, dict) else None
        seen += 1
    return _ABSENT


def _assign_own(obj: Any, prop: str, value: Any) -> None:
    """把 ``prop`` 写到原型链中拥有它的那个对象上。"""
    current = obj
    seen = 0
    while current is not None and seen < 512:
        namespace = getattr(current, "__dict__", None)
        if isinstance(namespace, dict):
            if prop in namespace or namespace.get("__cordis_proto__") is None:
                namespace[prop] = value
                return
            current = namespace.get("__cordis_proto__")
        else:
            current = None
        seen += 1
    object.__setattr__(obj, prop, value)


def is_special_property(prop: Any) -> bool:
    """反射层绝不拦截的属性名返回真。

    对应 ``reflect.ts:86`` 的 ``isSpecialProperty``：dunder 名、保留字
    ``prototype`` / ``then``、纯数字字符串，以及任何以下划线开头的名字。
    """
    if not isinstance(prop, str):
        return True
    if prop in RESERVED_WORDS:
        return True
    if prop.isdigit():
        return True
    if prop.startswith("_"):
        return True
    return False


class Property:
    """反射服务已知的上下文属性定义。"""

    SERVICE = "service"
    ACCESSOR = "accessor"


class Accessor:
    """由自定义 get / set 钩子支撑的计算型上下文属性。"""

    __slots__ = ("get", "set")

    def __init__(self, get: Callable[..., Any], set: Optional[Callable[..., bool]] = None) -> None:
        self.get = get
        self.set = set


class Impl:
    """存储在根 reflect 服务中的具体服务实现记录。"""

    __slots__ = ("name", "fiber", "value", "check")

    def __init__(self, name: str, fiber: "Fiber", value: Any = None, check: Optional[Callable[[], bool]] = None) -> None:
        self.name = name
        self.fiber = fiber
        self.value = value
        self.check = check

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"Impl(name={self.name!r}, fiber=uid{self.fiber.uid})"


class ReflectService:
    """上下文代理的支撑服务（``ctx.get`` / ``provide`` / ...）。"""

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        # 服务实现，按隔离 label 作键。
        self.store: Dict[Any, Impl] = {}
        # 已声明的上下文属性（服务与访问器），按名字作键。
        self.props: Dict[str, Any] = {}
        define_property(self, symbols.tracker, Tracker(property="ctx", no_shadow=True))

        self.mixin("reflect", ["get", "set", "provide", "accessor", "mixin"])
        self.mixin("fiber", ["runtime", "effect"])
        self.mixin("registry", ["inject", "plugin"])
        self.mixin("events", ["on", "once", "parallel", "emit", "serial", "bail", "waterfall"])

    # -- 代理操作 -------------------------------------------------------------
    def _resolve(self, prop: str, receiver: Any) -> Any:
        """为 ``receiver`` 解析 ``prop``（对应 ``reflect.ts`` 的 ``get`` 陷阱）。"""
        target = receiver
        existing = _lookup_own(target, prop)
        if existing is not _ABSENT:
            return get_traceable(receiver, existing)

        error = AttributeError(f'cannot get property "{prop}" without inject')

        try:
            definition = self.props.get(prop)
            if definition is not None and not isinstance(definition, str) and definition.get is not None:
                return definition.get(receiver, error)

            fiber = getattr(receiver, "fiber", None)
            if fiber is None or fiber.runtime is None:
                return self.get(prop, False, receiver)

            def _next() -> Any:
                key = _chain_isolate(target).get(prop)
                shadow = _lookup_own(receiver, symbols.shadow)
                current = ((shadow if shadow is not _ABSENT else receiver)).fiber
                while True:
                    impl = (current.store or {}).get(prop) if current.store else None
                    if impl is not None:
                        return get_traceable(receiver, impl.value)
                    if current.inject is not None and prop in current.inject:
                        raise AttributeError(f'cannot get required service "{prop}" in inactive context')
                    if current.runtime is None:
                        raise error
                    parent_isolate = _chain_isolate(current.parent)
                    if parent_isolate.get(prop) != key:
                        raise error
                    current = current.parent.fiber

            return receiver.events.waterfall(receiver, "internal/get", receiver, prop, error, _next)
        except AttributeError:
            raise

    def _assign(self, prop: str, value: Any, receiver: Any) -> Any:
        """在 ``receiver`` 上赋值 ``prop``（对应 ``reflect.ts`` 的 ``set`` 陷阱）。"""
        error = AttributeError(f'cannot set property "{prop}" without provide')
        definition = self.props.get(prop)
        if definition is None:
            if _lookup_own(receiver, prop) is not _ABSENT:
                _assign_own(receiver, prop, value)
                return True
            fid = getattr(receiver, "fiber", None)
            if fid is None or fid.runtime is None:
                _assign_own(receiver, prop, value)
                return True
            raise error

        if not isinstance(definition, str):
            if definition.set is None:
                return False
            return definition.set(value, receiver, error)

        def _next() -> Any:
            return self.set(prop, value, error, receiver)

        return receiver.events.waterfall(receiver, "internal/set", receiver, prop, value, error, _next)

    def _has(self, prop: str) -> bool:
        """``reflect.ts`` 中的 ``has`` 陷阱（以根上下文调用）。"""
        if _lookup_own(self.ctx, prop) is not _ABSENT:
            return True
        return prop in self.props

    # -- 服务辅助 -------------------------------------------------------------
    def get(self, name: str, strict: bool = True, ctx: Any = None) -> Any:
        """从 store 读取服务，不要求 inject。"""
        impl = self._get_impl(name, strict, ctx)
        return get_traceable(ctx or self.ctx, impl.value) if impl else None

    def _get_impl(self, name: str, strict: bool = True, ctx: Any = None) -> Optional[Impl]:
        scope_ctx = ctx if ctx is not None else self.ctx
        key = _chain_isolate(scope_ctx).get(name)
        impl = self.store.get(key) if key else None
        if impl is None:
            return None
        from .fiber import FiberState

        if strict and impl.fiber.state != FiberState.ACTIVE:
            return None
        return impl

    def set(self, name: str, value: Any, error: Optional[Exception] = None, ctx: Any = None) -> bool:
        """覆盖某个已提供服务的值（仅限提供方 fiber）。"""
        scope_ctx = ctx if ctx is not None else self.ctx
        key = _chain_isolate(scope_ctx).get(name)
        impl = self.store.get(key) if key else None
        if impl is None:
            raise RuntimeError(f'cannot set property "{name}" without provide')
        if impl.fiber is not _fiber_of(scope_ctx):
            raise RuntimeError(f'cannot set property "{name}" in multiple fibers')
        impl.value = value
        return True

    def provide(
        self,
        name: str,
        value: Any = None,
        check: Optional[Callable[[], bool]] = None,
        caller: Any = None,
    ) -> Callable[[], Any]:
        """注册一个由当前 fiber 持有的服务实现。"""

        def _setup() -> Callable[..., Any]:
            # `self.ctx` 是拥有这个单例服务的 *根* 上下文；作用域必须从 *提供方*
            # 上下文读取，否则被隔离的提供方会与根作用域发生碰撞。
            scope = caller if caller is not None else self.ctx
            owner = _fiber_of(scope)
            existing = self.props.get(name)
            if existing is None:
                self.props[name] = Property.SERVICE
            elif existing != Property.SERVICE:
                raise RuntimeError(f'property "{name}" is already declared as {existing}')
            self.props[name] = Property.SERVICE

            root_isolate = getattr(self.ctx.root, symbols.isolate)
            if name not in _chain_isolate(scope):
                if root_isolate.get(name) is None:
                    root_isolate[name] = name
            key = _chain_isolate(scope).get(name)
            if key is None:
                # 调用方的链上尚未携带该名字（例如在根默认值存在之前创建的全新
                # 插件 ctx）；采用根声明的 label，使该条目保持可寻址。
                root_isolate[name] = name
                key = name
            impl = Impl(name, owner, value, check)
            if key in self.store:
                raise RuntimeError(f'service "{name}" has been registered at <{self.store[key].fiber.name}>')
            self.store[key] = impl
            if owner.store is not None:
                owner.store[name] = impl
            from .fiber import FiberState

            if owner.state == FiberState.ACTIVE:
                self.notify([name], scope_ctx=scope)

            async def _dispose() -> None:
                self.store.pop(key, None)
                fibers = self.notify([name], scope_ctx=scope)
                for fiber in fibers:
                    try:
                        await fiber.await_()
                    except Exception:  # noqa: BLE001 - settled by allSettled
                        pass
                if owner.store is not None:
                    owner.store.pop(name, None)

            return _dispose

        return _fiber_of(caller if caller is not None else self.ctx).effect(
            _setup, f"ctx.provide({name!r})"
        )

    def notify(
        self,
        names: List[str],
        filter_fn: Optional[Callable[["Context", str], bool]] = None,
        scope_ctx: Any = None,
    ) -> List["Fiber"]:
        """重新求值每一个依赖了给定服务之一的 fiber。"""
        origin = scope_ctx if scope_ctx is not None else self.ctx
        if filter_fn is None:

            def filter_fn(ctx: "Context", name: str) -> bool:  # type: ignore[misc]
                return _chain_isolate(ctx).get(name) == _chain_isolate(origin).get(name)

        fibers: List["Fiber"] = []
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
                if not has_update:
                    continue
                fiber._refresh()
                fibers.append(fiber)
        for name in names:
            impl = self._get_impl(name, False, origin)
            # 派发时携带作用域过滤器：只有同一隔离 label 的监听器才收得到，
            # 对应 reflect.ts:330-333 的 `self[symbols.filter] = ...`。
            scoped = self.ctx.extend(
                {symbols.filter: lambda target, _name=name, _f=filter_fn: _f(target, _name)}
            )
            self.ctx.events.emit(scoped, "internal/service", name, impl.value if impl else None)
        return fibers

    def accessor(self, name: str, options: Accessor, caller: Any = None) -> Callable[[], Any]:
        """定义一个由 get/set 钩子支撑的计算型上下文属性。"""

        def _setup() -> Callable[[], None]:
            if name in self.props:
                existing = self.props[name]
                kind = existing if isinstance(existing, str) else Property.ACCESSOR
                raise RuntimeError(f'property "{name}" is already declared as {kind}')
            self.props[name] = options
            return lambda: self.props.pop(name, None)

        return _fiber_of(caller if caller is not None else self.ctx).effect(
            _setup, f"ctx.accessor({name!r})"
        )

    def mixin(self, source: Any, mixins: Any, caller: Any = None) -> Callable[[], Any]:
        """把某个服务的若干成员直接暴露到 ``ctx`` 上。"""

        def _setup() -> Any:
            entries = (
                [(key, key) for key in mixins]
                if isinstance(mixins, (list, tuple))
                else list(mixins.items())
            )
            disposers: List[Callable[[], Any]] = []

            def get_target(ctx: "Context", error: Exception) -> Any:
                return getattr(ctx, source) if isinstance(source, str) else source

            def _member(service: Any, key: str) -> Any:
                # 服务值可以是对象，也可以是普通 dict；两种都要能取到成员。
                if isinstance(service, dict):
                    return service.get(key)
                return getattr(service, key)

            for key, value in entries:
                def _get(this: Any, error: Exception, _key: str = key) -> Any:
                    service = get_target(this, error)
                    if service is None:
                        return service
                    return _member(service, _key)

                def _set(new_value: Any, this: Any, error: Exception, _key: str = key) -> bool:
                    service = get_target(this, error)
                    if isinstance(service, dict):
                        service[_key] = new_value
                    else:
                        setattr(service, _key, new_value)
                    return True

                disposers.append(self.accessor(value, Accessor(get=_get, set=_set), caller=caller))
            return _make_iterator(disposers)

        return _fiber_of(caller if caller is not None else self.ctx).effect(
            _setup, f"ctx.mixin({_short(source)!r})"
        )

    # -- 追踪 -----------------------------------------------------------------
    def trace(self, value: Any) -> Any:
        """把本上下文的追踪包装挂到某个值上。"""
        return get_traceable(self.ctx, value)

    def bind(self, callback: Callable[..., Any]) -> Callable[..., Any]:
        """包装一个回调，使调用时对其参数做追踪。

        Python 没有 ``this`` 绑定，因此与 ``reflect.ts`` 不同，这里只追踪
        参数；派发上下文由事件服务在监听器显式选择接收时另行传入。
        """
        service = self

        def _bound(*args: Any, **kwargs: Any) -> Any:
            return callback(*[service.trace(arg) for arg in args])

        return _bound


def _make_iterator(items: List[Any]) -> Any:
    """逐个产出 disposer，让 effect 主体按产出顺序注册它们。"""
    for item in items:
        yield item


def _short(value: Any) -> str:
    if isinstance(value, str):
        return value
    return getattr(value, "name", None) or getattr(value, "__name__", None) or type(value).__name__
