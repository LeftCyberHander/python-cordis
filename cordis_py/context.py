"""插件的根上下文与子上下文容器。

``src/context.ts`` 的 Python 移植。JavaScript 版本是一个 ``Proxy``，其
``get`` / ``set`` / ``has`` 陷阱委托给 :class:`~cordis_py.reflect.ReflectService`。
Python 没有代理层，因此 :class:`Context` 通过 ``__getattr__`` / ``__setattr__`` /
``__contains__`` 实现同样的三种操作，同时保持完全一致的解析算法——包括
"全上下文树只有一个 Proxy" 这一不变量，在此以 ``__cordis_proto__`` 原型链表达。

``isolate()`` / ``intercept()`` 返回的子对象，其 ``__cordis_proto__`` 指向原始
对象（绝不回指携带 shadow 的子对象），这与 ``context.ts:106`` 处 ``extend()``
对 shadow 的重新包装完全一致。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from . import reflect as _reflect_module
from .reflect import ReflectService
from .utils import define_property, get_traceable, symbols

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .events import EventsService
    from .fiber import Fiber
    from .logger import LoggerService
    from .registry import RegistryService

__all__ = ["Context"]

_MISSING = object()

# 允许经由 ``__getattr__`` 解析的内部符号键：它们的名字同样以 ``__`` 开头，
# 但必须沿原型链读取（例如 ``notify()`` 派发时携带的 ``symbols.filter``）。
_SYMBOL_KEYS = frozenset(
    {
        symbols.effect,
        symbols.filter,
        symbols.isolate,
        symbols.intercept,
        symbols.shadow,
        symbols.receiver,
        "__cordis_proto__",
    }
)


def _fiber_of(ctx: "Context") -> Any:
    """沿 ``__cordis_proto__`` 链取最近的 ``fiber``。

    ``context.ts`` 的子上下文是 ``Object.create(this)``，``fiber`` 通过原型链
    自然可读；Python 的实例属性不做原型查找，因此这里显式走一遍。
    """
    current: Any = ctx
    while current is not None:
        namespace = object.__getattribute__(current, "__dict__")
        if "fiber" in namespace:
            return namespace["fiber"]
        current = namespace.get("__cordis_proto__")
    raise AttributeError("fiber")



class Context:
    """Cordis 插件的根上下文与子上下文容器。"""

    # 镜像 JS 类上 `static readonly` 字段的符号键。
    EFFECT = symbols.effect
    FILTER = symbols.filter
    ISOLATE = symbols.isolate
    INTERCEPT = symbols.intercept

    # Python 用 dunder 名表达反射钩子，使其永不与用户服务名冲突；
    # JS 类则把它们收集在 `ReflectService.handler` 中。
    _HOOK_NAMES = frozenset({"__getattr__", "__setattr__", "__contains__", "__cordis_proto__", "fiber"})

    # 支撑 `isolate()` 默认分配全新 label 的单调计数器。
    _isolate_seq = 0

    def __init__(self) -> None:
        object.__setattr__(self, "_cordis_ready", False)
        object.__setattr__(self, "__cordis_proto__", None)
        object.__setattr__(self, "_shadow_exec", None)
        object.__setattr__(self, symbols.isolate, {})
        object.__setattr__(self, symbols.intercept, {})

        # `Context <fiber-name>` 展示用的诊断钩子。
        from .fiber import Fiber

        self.root = self
        self.base_url: Optional[str] = None
        self.fiber = Fiber(self, {}, {}, None, lambda: [])
        self.reflect = ReflectService(self)
        self.registry = self._make_registry()
        self.events = self._make_events()
        self.logger = self._make_logger()
        # 诊断句柄：工具链（tools/verify*.py）需要在不经过调用方重绑的前提下
        # 直接检查 exporter 数量与日志缓冲。
        object.__setattr__(self, "_logger_service", object.__getattribute__(self, "logger"))
        # 注意：内置 buffer exporter 的 disposer 仍然保留在根 fiber 上——
        # 它是 logger 的常驻资源，根 fiber 的 effect 元数据必须如实反映它。

        object.__setattr__(self, "_cordis_ready", True)

    # -- 内置服务装配 ---------------------------------------------------------
    def _make_registry(self) -> "RegistryService":
        from .registry import RegistryService

        return RegistryService(self)

    def _make_events(self) -> "EventsService":
        from .events import EventsService

        return EventsService(self)

    def _make_logger(self) -> "LoggerService":
        from .logger import LoggerService

        return LoggerService(self)

    @staticmethod
    def is_context(value: Any) -> bool:
        """上下文与上下文原型返回真。"""
        return isinstance(value, Context)

    # -- 镜像解析钩子 ---------------------------------------------------------
    # 这四个内置服务挂在实例字典上，普通属性查找不会经过 ``__getattr__``；
    # 但 JS 版的 Proxy 连自有属性也拦截，因此这里补上同样的重绑：读到的服务
    # 必须带上「调用方上下文」，否则 ``ctx.logger()`` 会一直拿到根 fiber。
    _TRACEABLE = frozenset({"reflect", "registry", "events", "logger"})

    def __getattribute__(self, prop: str) -> Any:
        value = object.__getattribute__(self, prop)
        if prop in Context._TRACEABLE:
            tracker = getattr(value, symbols.tracker, None)
            if tracker is not None:
                return get_traceable(self, value)
        return value

    def __getattr__(self, prop: str) -> Any:
        if not object.__getattribute__(self, "_cordis_ready"):
            raise AttributeError(prop)
        if prop.startswith("__") and prop not in _SYMBOL_KEYS:
            raise AttributeError(prop)
        reflect: ReflectService = object.__getattribute__(self, "reflect")
        return reflect._resolve(prop, self)

    def __setattr__(self, prop: str, value: Any) -> None:
        if prop.startswith("__") or prop in ("_cordis_ready",) or not object.__getattribute__(self, "_cordis_ready"):
            object.__setattr__(self, prop, value)
            return
        if reflect_is_special(prop):
            object.__setattr__(self, prop, value)
            return
        reflect: ReflectService = object.__getattribute__(self, "reflect")
        reflect._assign(prop, value, self)

    def __contains__(self, prop: object) -> bool:
        if not isinstance(prop, str) or reflect_is_special(prop):
            return object.__getattribute__(self, "__dict__").__contains__(prop)
        # ``Reflect.has`` 沿原型链判定，因此 Context 类自身的成员
        # （``extend`` / ``isolate`` / ``get`` ...）同样算「存在」。
        if hasattr(type(self), prop):
            return True
        if object.__getattribute__(self, "__dict__").__contains__(prop):
            return True
        proto = object.__getattribute__(self, "__cordis_proto__")
        if proto is not None and prop in proto.__dict__:
            return True
        reflect: ReflectService = object.__getattribute__(self, "reflect")
        return reflect._has(prop)

    # -- 作用域 ---------------------------------------------------------------
    def extend(self, meta: Optional[Dict[str, Any]] = None) -> "Context":
        """在本作用域之上创建携带额外元数据的子上下文。

        子上下文通过 ``__cordis_proto__`` 链继承本上下文的全部属性；``meta``
        中的自有属性会遮蔽继承来的同名属性。父上下文不会被改动。
        """
        shadow = object.__getattribute__(self, "__dict__").get("_shadow_exec")
        meta = meta or {}
        child = Context.__new__(Context)
        object.__setattr__(child, "_cordis_ready", False)
        object.__setattr__(child, "__cordis_proto__", self)
        object.__setattr__(child, "_shadow_exec", None)
        # `meta` 中调用方传入的 isolate / intercept 映射优先；否则子上下文继承
        # 本上下文自有映射的一份链式拷贝。
        object.__setattr__(
            child,
            symbols.isolate,
            meta.get(symbols.isolate, self._inherit_isolate_map()),
        )
        object.__setattr__(
            child,
            symbols.intercept,
            meta.get(symbols.intercept, getattr(self, symbols.intercept)),
        )
        for key, value in meta.items():
            if key in (symbols.isolate, symbols.intercept):
                continue
            object.__setattr__(child, key, value)
        # 与父上下文共享内置服务（它们挂在根上下文的 `reflect.store` 上），
        # 这样 `ctx.events` / `ctx.registry` 从子上下文解析的结果，与经由 JS
        # 原型链解析完全一致。
        for service in ("reflect", "registry", "events", "logger"):
            if not hasattr(child, service):
                try:
                    value = object.__getattribute__(self, service)
                except AttributeError:
                    # 根上下文构造到一半（logger 尚未赋值）时派生上下文：
                    # 留空即可，原型链随后会自动解析到已就绪的那个。
                    continue
                object.__setattr__(child, service, value)
        object.__setattr__(child, "_cordis_ready", True)
        if not shadow:
            return child
        outer = Context.__new__(Context)
        object.__setattr__(outer, "_cordis_ready", False)
        object.__setattr__(outer, "__cordis_proto__", child)
        object.__setattr__(outer, "_shadow_exec", shadow)
        object.__setattr__(outer, symbols.isolate, object.__getattribute__(child, symbols.isolate))
        object.__setattr__(outer, symbols.intercept, object.__getattribute__(child, symbols.intercept))
        for key, value in meta.items():
            if key in (symbols.isolate, symbols.intercept):
                continue
            object.__setattr__(outer, key, value)
        for service in ("reflect", "registry", "events", "logger"):
            if not hasattr(outer, service):
                try:
                    value = object.__getattribute__(self, service)
                except AttributeError:
                    continue
                object.__setattr__(outer, service, value)
        object.__setattr__(outer, "_cordis_ready", True)
        return outer

    def _inherit_isolate_map(self) -> Dict[str, Any]:
        """isolate 映射的自有拷贝，链接到父层。"""
        current = object.__getattribute__(self, symbols.isolate)
        parent_layer = {k: v for k, v in current.items() if k != "__cordis_parent_isolate__"}
        layer = {k: v for k, v in current.items() if k != "__cordis_parent_isolate__"}
        layer["__cordis_parent_isolate__"] = parent_layer
        return layer

    def isolate(self, name: str, label: Optional[str] = None) -> "Context":
        """为 ``name`` 创建带独立服务作用域的子上下文。

        新 label 会遮蔽 ``name`` 继承来的任何条目；把同一个 ``label`` 传给两次
        ``isolate()`` 调用即合并它们的作用域。省略 ``label`` 时会分配一个
        **全新的唯一** label——对应 ``context.ts:123`` 的 ``Symbol(name)``，
        它绝不会与 ``ctx.provide()`` 自动创建的 label 发生碰撞。
        """
        current = object.__getattribute__(self, symbols.isolate)
        shadow = {k: v for k, v in current.items() if k != "__cordis_parent_isolate__"}
        if label is None:
            Context._isolate_seq += 1
            label = f"{name}#{Context._isolate_seq}"
        shadow[name] = label
        return self.extend({symbols.isolate: shadow})

    def intercept(self, name: str, config: Any) -> "Context":
        """为在此之下启动的插件添加服务专属 intercept 配置。

        与 ``isolate`` 同构：新层自带 ``name`` 的配置，并把上一层整体挂到
        ``__cordis_parent_intercept__`` 下。解析时逐层向上合并（近者胜），
        因此祖先条目的**其它** key 不会被子孙覆盖丢失。
        """
        current = object.__getattribute__(self, symbols.intercept)
        parent = {k: v for k, v in current.items() if k != "__cordis_parent_intercept__"}
        layer = dict(parent)
        layer["__cordis_parent_intercept__"] = parent
        layer[name] = config
        return self.extend({symbols.intercept: layer})

    # -- 便捷方法 -------------------------------------------------------------
    def get(self, name: str, strict: bool = True) -> Any:
        return object.__getattribute__(self, "reflect").get(name, strict, self)

    def set(self, name: str, value: Any) -> bool:
        return object.__getattribute__(self, "reflect").set(name, value, None, self)

    def provide(self, name: str, value: Any = None, check: Optional[Callable[[], bool]] = None) -> Callable[[], Any]:
        return object.__getattribute__(self, "reflect").provide(name, value, check, caller=self)

    def accessor(self, name: str, options: Any) -> Callable[[], Any]:
        return object.__getattribute__(self, "reflect").accessor(name, options, caller=self)

    def mixin(self, source: Any, mixins: Any) -> Callable[[], Any]:
        return object.__getattribute__(self, "reflect").mixin(source, mixins, caller=self)

    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Callable[..., Any]:
        return _fiber_of(self).effect(execute, label)

    def plugin(self, plugin: Any, config: Any = _MISSING, *extra: Any) -> Any:
        args: List[Any] = []
        if config is not _MISSING:
            args.append(config)
        args.extend(extra)
        return object.__getattribute__(self, "registry").plugin(plugin, *args, _caller=self)

    def inject(self, deps: Any, callback: Callable[..., Any]) -> Any:
        return object.__getattribute__(self, "registry").inject(deps, callback, _caller=self)

    # -- 事件门面 -------------------------------------------------------------
    def emit(self, *args: Any) -> None:
        object.__getattribute__(self, "events").emit(*args)

    def on(self, name: Any, listener: Callable[..., Any], options: Any = None) -> Callable[[], Any]:
        return object.__getattribute__(self, "events").on(name, listener, options, _caller=self)

    def once(self, name: Any, listener: Callable[..., Any], options: Any = None) -> Callable[[], Any]:
        return object.__getattribute__(self, "events").once(name, listener, options, _caller=self)

    def bail(self, *args: Any) -> Any:
        return object.__getattribute__(self, "events").bail(*args)

    async def serial(self, *args: Any) -> Any:
        return await object.__getattribute__(self, "events").serial(*args)

    async def parallel(self, *args: Any) -> None:
        return await object.__getattribute__(self, "events").parallel(*args)

    def waterfall(self, *args: Any) -> Any:
        return object.__getattribute__(self, "events").waterfall(*args)


def reflect_is_special(prop: str) -> bool:
    """本地别名，避免在模块作用域产生循环导入。"""
    return _reflect_module.is_special_property(prop)
