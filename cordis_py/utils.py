"""上下文、服务与插件 fiber 共用的内部辅助函数。

``src/utils.ts`` 的 Python 移植。JavaScript 原版依赖 ``Symbol``、``WeakMap``、
``Proxy`` 与 ``Reflect``；这里依次用「驻留字符串」（interned string）、
:class:`~weakref.WeakKeyDictionary`（对不可弱引用的键回退为普通 dict），以及
显式的 ``__getattr__`` / ``__setattr__`` 钩子替代代理陷阱。
"""

from __future__ import annotations

import inspect
import time
import traceback
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

__all__ = [
    "symbols",
    "DisposableList",
    "Tracker",
    "is_object",
    "is_nullable",
    "is_callable",
    "is_constructor",
    "join_prototype",
    "get_property_descriptor",
    "get_traceable",
    "TraceableValue",
    "ServiceView",
    "with_props",
    "create_callable",
    "compose_error",
    "build_outer_stack",
    "build_stack",
    "define_property",
]


class _Symbols:
    """用驻留字符串充当 JavaScript 符号注册表的替身。

    ``Symbol.for(key)`` 映射为字面量 ``key`` 字符串，因此相等性与哈希行为
    与符号身份完全一致，同时在 Python 工具链中保持可读。每个键都刻意加上
    ``__cordis_`` 前缀，使其永不与用户服务名冲突。
    """

    # 内部符号
    shadow = "__cordis_shadow__"
    receiver = "__cordis_receiver__"
    original = "__cordis_original__"
    metadata = "__cordis_metadata__"
    init_hooks = "__cordis_initHooks__"
    check_proto = "__cordis_checkProto__"
    # 上下文符号
    effect = "__cordis_effect__"
    filter = "__cordis_filter__"
    isolate = "__cordis_isolate__"
    intercept = "__cordis_intercept__"
    # 服务符号
    init = "__cordis_init__"
    check = "__cordis_check__"
    config = "__cordis_config__"
    invoke = "__cordis_invoke__"
    extend = "__cordis_extend__"
    tracker = "__cordis_tracker__"
    resolve_config = "__cordis_resolveConfig__"

    # -- 为与 TypeScript 拼写保持一致的别名 -----------------------------------
    @property
    def initHooks(self) -> str:  # noqa: N802 - 对齐 JS 标识符
        return self.init_hooks

    @property
    def checkProto(self) -> str:  # noqa: N802 - 对齐 JS 标识符
        return self.check_proto

    @property
    def resolveConfig(self) -> str:  # noqa: N802 - 对齐 JS 标识符
        return self.resolve_config

    def name_of(self, key: str) -> str:
        """从驻留字符串还原出原始的 ``Symbol.for`` 键。"""
        if key.startswith("__cordis_") and key.endswith("__"):
            return "cordis." + key[len("__cordis_") : -2]
        return key


symbols = _Symbols()


class DisposableList:
    """有序的可释放值集合，支持按值 O(1) 删除。

    ``utils.ts`` 中 ``DisposableList`` 的移植。``clear()`` 按**逆序**（后注册者
    在前）返回所有值——这是 :meth:`Fiber._unload` 所用停机顺序的来源。
    """

    __slots__ = ("_sn", "_map", "_weak")

    def __init__(self) -> None:
        self._sn = 0
        self._map: Dict[int, Any] = {}
        self._weak: Any
        try:
            from weakref import WeakKeyDictionary

            self._weak = WeakKeyDictionary()
        except Exception:  # pragma: no cover - WeakKeyDictionary 总是存在
            self._weak = {}

    def __len__(self) -> int:
        return len(self._map)

    @property
    def length(self) -> int:
        return len(self._map)

    def push(self, value: Any) -> Callable[[], bool]:
        """追加 ``value`` 并返回一个移除闭包（幂等）。"""
        self._sn += 1
        sn = self._sn
        self._map[sn] = value
        try:
            self._weak[value] = sn
        except TypeError:
            # value 不可弱引用（例如普通 int）；按值删除退化为线性扫描。
            pass
        return lambda: self._map.pop(sn, None) is not None

    def unshift(self, value: Any) -> Callable[[], bool]:
        """把 ``value`` 插到最前并返回移除闭包（幂等）。

        对应 ``events.ts:143`` 的 ``hooks.unshift(listener)``。``_map`` 按插入
        顺序迭代，因此重建映射时先放入新序号即可保持「新值在最前」。
        """
        self._sn += 1
        sn = self._sn
        items = list(self._map.items())
        self._map.clear()
        self._map[sn] = value
        for key, item in items:
            self._map[key] = item
        try:
            self._weak[value] = sn
        except TypeError:
            pass
        return lambda: self._map.pop(sn, None) is not None

    def delete(self, value: Any) -> bool:
        try:
            sn = self._weak.get(value)
        except TypeError:
            sn = None
        if not sn:
            for key, candidate in self._map.items():
                if candidate is value:
                    del self._map[key]
                    return True
            return False
        return self._map.pop(sn, None) is not None

    def clear(self) -> List[Any]:
        """清空列表，并按逆序返回此前保存的所有值。"""
        values = list(self._map.values())
        self._map.clear()
        values.reverse()
        return values

    def __iter__(self) -> Iterator[Any]:
        return iter(list(self._map.values()))

    def __contains__(self, value: Any) -> bool:
        return any(candidate is value for candidate in self._map.values())

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"DisposableList({list(self)})"


class Tracker:
    """可追踪包装器用于重绑 ``ctx`` 与服务的元数据。"""

    __slots__ = ("associate", "property", "no_shadow")

    def __init__(
        self,
        associate: Optional[str] = None,
        property: Optional[str] = None,
        no_shadow: bool = False,
    ) -> None:
        self.associate = associate
        self.property = property
        self.no_shadow = no_shadow

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"Tracker(associate={self.associate!r}, property={self.property!r}, "
            f"no_shadow={self.no_shadow!r})"
        )


def define_property(obj: Any, key: str, value: Any) -> None:
    """把 ``obj[key] = value`` 定义为**可枚举**属性。

    注意与 ``cosmokit.defineProperty`` 的差异（后者写入的是不可枚举、可写的
    属性）：Python 的内省依赖该值可见，而不可枚举性在此并不可观察。
    """

    try:
        setattr(obj, key, value)
    except AttributeError:
        object.__setattr__(obj, key, value)
    if isinstance(obj, dict):
        obj[key] = value


def is_object(value: Any) -> bool:
    """非 null 的对象与函数（可调用对象）返回真。"""
    return value is not None and (isinstance(value, (dict, list, tuple, set)) or hasattr(value, "__dict__") or callable(value))


def is_nullable(value: Any) -> bool:
    return value is None


def is_callable(value: Any) -> bool:
    return callable(value)


def is_constructor(func: Any) -> bool:
    """插件回调需要用 ``()`` 实例化时返回真。

    Python 的类正是 JavaScript 构造函数的对应物：原版排除了 async / 箭头 /
    生成器函数，它们在 Python 中没有对应形态。因此类这里是唯一的 ``True`` 情形。
    """
    return isinstance(func, type)


def join_prototype(proto1: Any, proto2: Any) -> Any:
    """合并两条原型链，同时保留 ``proto1`` 的自有成员。

    构造一个 ``proto2`` 的子类来承载 ``proto1`` 的属性，这等价于遍历
    ``Object.getPrototypeOf`` 链并把每个自有描述符重新定义到合并结果上。
    """
    if proto1 is object or proto1 is None:
        return proto2
    attributes = dict(getattr(proto1, "__dict__", {}) or {})
    attributes.pop("__dict__", None)
    attributes.pop("__weakref__", None)
    return type("JoinPrototype", (proto2,), attributes)


def get_property_descriptor(target: Any, prop: str) -> Optional[Any]:
    """沿 MRO 查找类级成员，模拟自有描述符。"""
    for klass in getattr(type(target), "__mro__", [type(target)]):
        if prop in getattr(klass, "__dict__", {}):
            return getattr(klass, "__dict__")[prop]
    if isinstance(target, dict) and prop in target:
        return target[prop]
    return None


def get_traceable(ctx: Any, value: Any) -> Any:
    """包装服务，使方法调用看到调用方的活动上下文。"""
    if not is_object(value):
        return value
    # 已经是可追踪包装器：解包到底层值。
    if isinstance(value, TraceableValue):
        return value.original
    tracker = getattr(value, symbols.tracker, None)
    if not tracker:
        return value
    return TraceableValue(ctx, value, tracker)


class ServiceView:
    """以「调用方上下文」视角访问同一个服务实例。

    对应 ``utils.ts`` 的 ``createShadow`` + ``createShadowMethod``： JavaScript
    里服务代理把 ``this.ctx`` 重绑到调用方，方法体读取的 ``this.ctx`` 因此是
    **调用方**而非提供方。Python 没有可拦截方法调用的代理，这里用视图对象
    替代 ``self``：

    - 读属性时把方法重新绑定到视图上（方法体里的 ``self`` 就是视图）；
    - 写属性时透写回原实例（``self._sn_exporter += 1`` 必须生效）；
    - ``ctx`` 返回调用方上下文，其余成员全量转发。

    这样服务无需知道「是谁在调用」，也不需要为每次调用复制实例。
    """

    __slots__ = ("_original", "_ctx")

    def __init__(self, original: Any, ctx: Any) -> None:
        object.__setattr__(self, "_original", original)
        object.__setattr__(self, "_ctx", ctx)

    @property
    def ctx(self) -> Any:
        return object.__getattribute__(self, "_ctx")

    @property
    def ctx_holder(self) -> Dict[str, Any]:
        return {"ctx": object.__getattribute__(self, "_ctx")}

    def __getattr__(self, prop: str) -> Any:
        original = object.__getattribute__(self, "_original")
        attr = getattr(original, prop)
        if inspect.ismethod(attr):
            return attr.__func__.__get__(self, type(original))
        return attr

    def __setattr__(self, prop: str, value: Any) -> None:
        if prop in ("_original", "_ctx"):
            object.__setattr__(self, prop, value)
            return
        setattr(object.__getattribute__(self, "_original"), prop, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        original = object.__getattribute__(self, "_original")
        call = getattr(type(original), "__call__", None)
        if call is None:
            return original(*args, **kwargs)
        return call(self, *args, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"ServiceView({self._original!r})"


def _shadow_of(ctx: Any) -> Any:
    """只读 ``__dict__`` 链取 ``symbols.shadow``，绝不触发服务解析。

    走 ``getattr`` 会进入 ``Context.__getattr__`` → ``reflect._resolve`` →
    再次构造 TraceableValue，直接递归爆栈。
    """
    current = ctx
    while current is not None:
        namespace = getattr(current, "__dict__", None)
        if not isinstance(namespace, dict):
            return None
        if symbols.shadow in namespace:
            return namespace[symbols.shadow]
        current = namespace.get("__cordis_proto__")
    return None


class TraceableValue:
    """``utils.ts`` 中 ``createTraceable`` 的值级对应物。

    重绑 ``ctx``（以及设置 ``associate`` 时的各类 ``<service>.<property>``
    访问器），使经由包装器进行的读写针对**调用方**的上下文解析，而非提供方。
    """

    __slots__ = ("ctx", "original", "tracker", "__weakref__")

    def __init__(self, ctx: Any, value: Any, tracker: Tracker) -> None:
        self.tracker = tracker
        # 对应 `createTraceable`：noShadow 服务具有身份语义（logger 从origin
        # fiber 派生其名称），因此保留 shadow ctx；其余服务则剥离它，使其副作用
        # 绑定到调用方。
        if ctx is not None and _shadow_of(ctx) and not tracker.no_shadow:
            ctx = _strip_shadow(ctx)
        self.ctx = ctx
        self.original = value

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"TraceableValue({self.original!r})"

    def __getattr__(self, prop: str) -> Any:
        if prop == symbols.original:
            return self.original
        tracker = self.tracker
        ctx = self.ctx
        if prop == tracker.property:
            return ctx
        target = self.original
        if (
            tracker.associate
            and getattr(ctx, "reflect", None) is not None
            and f"{tracker.associate}.{prop}" in ctx.reflect.props
        ):
            return getattr(ctx, f"{tracker.associate}.{prop}")
        inner = getattr(target, prop, None)
        if inner is None and not hasattr(target, prop):
            raise AttributeError(prop)
        inner_tracker = getattr(inner, symbols.tracker, None)
        if inner_tracker:
            return TraceableValue(ctx, inner, inner_tracker)
        if callable(inner):
            # 与服务方法一样，方法体里的 `self.ctx` 必须是调用方上下文。
            return getattr(ServiceView(target, ctx), prop)
        return inner

    def __setattr__(self, prop: str, value: Any) -> None:
        if prop in ("ctx", "original", "tracker"):
            object.__setattr__(self, prop, value)
            return
        setattr(self.original, prop, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        view = ServiceView(self.original, self.ctx)
        return view(*args, **kwargs)


def _strip_shadow(ctx: Any) -> Any:
    """返回被 shadow 上下文的原型上下文（即原始 ctx）。"""
    return getattr(ctx, "__cordis_proto__", None) or ctx


def with_props(target: Any, props: Optional[Dict[str, Any]]) -> Any:
    """把 ``props`` 叠加到 ``target`` 上，用于属性读写。"""
    if not props:
        return target
    return OverlaidObject(target, props)


class OverlaidObject:
    """``withProps`` 的显式替身（对 target 的局部 ``Proxy``）。"""

    __slots__ = ("_target", "_props", "__weakref__")

    def __init__(self, target: Any, props: Dict[str, Any]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_props", props)

    def __getattr__(self, prop: str) -> Any:
        props = object.__getattribute__(self, "_props")
        if prop in props and prop != "constructor":
            return props[prop]
        return getattr(object.__getattribute__(self, "_target"), prop)

    def __setattr__(self, prop: str, value: Any) -> None:
        props = object.__getattribute__(self, "_props")
        if prop in props and prop != "constructor":
            props[prop] = value
        else:
            setattr(object.__getattribute__(self, "_target"), prop, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return object.__getattribute__(self, "_target")(*args, **kwargs)


def create_callable(name: str, proto: Any, tracker: Tracker) -> Any:
    """创建经由 ``[invoke]`` 派发的可调用服务对象。"""

    def _call(*args: Any, **kwargs: Any) -> Any:
        invoke = getattr(_call, symbols.invoke, None)
        if invoke is None:
            raise TypeError(f"service {name!r} is not callable")
        proxy = TraceableValue(_call.ctx, _call, tracker)
        return invoke(proxy, *args, **kwargs)

    _call.__name__ = name
    _call.ctx = None
    setattr(_call, symbols.tracker, tracker)
    # 挂上原型的成员，使 `service.method(...)` 继续可用。
    for key in dir(proto):
        if key.startswith("__"):
            continue
        try:
            if not hasattr(_call, key):
                setattr(_call, key, getattr(proto, key))
        except Exception:  # pragma: no cover - 防御性
            pass
    return _call


def build_stack() -> str:
    """把当前 Python 调用栈捕获为以换行拼接的字符串。"""
    return "".join(traceback.format_stack()[:-1])


def build_outer_stack(offset: int = 0) -> Callable[[], List[str]]:
    """捕获一个延迟求值的栈帧供应器，供后续错误合成使用。"""
    stack = build_stack()
    lines = stack.split("\n")

    def _supplier() -> List[str]:
        return lines[3 + offset :]

    return _supplier


def compose_error(callback: Callable[[Dict[str, Any]], Any], get_outer_stack: Callable[[], List[str]] = build_outer_stack) -> Any:
    """执行 ``callback``，并把外层调用点栈帧拼进抛出的错误。

    Python 没有对 ``await`` 透明的 ``then``；异步 effect 返回的是由调用方处理的
    awaitable，因此这里只需对同步路径做栈修补。
    """
    info: Dict[str, Any] = {"offset": 1, "error": _new_stack_error()}
    try:
        return callback(info)
    except BaseException as reason:  # noqa: BLE001 - 在下方重新抛出
        _handle_error(info, reason, get_outer_stack)
        raise


def _new_stack_error() -> BaseException:
    return RuntimeError("effect")


def _handle_error(info: Dict[str, Any], reason: BaseException, get_outer_stack: Callable[[], List[str]]) -> None:
    """``handleError`` 的尽力而为对应物：前置外层调用帧。"""
    outer_lines = get_outer_stack()
    existing = getattr(reason, "__cordis_outer__", None)
    if existing:
        return
    try:
        reason.__cordis_outer__ = outer_lines  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - 某些异常不可写
        pass


def now_ms() -> int:
    """当前 epoch 毫秒数，对应 ``Date.now()``。"""
    return int(time.time() * 1000)


def hyphenate(name: str) -> str:
    """logger 默认名称所用的最小 ``paramCase`` 实现。"""
    out: List[str] = []
    for index, char in enumerate(name):
        if char.isupper():
            if index and (not name[index - 1].isupper() or (index + 1 < len(name) and name[index + 1].islower())):
                out.append("-")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out).replace("_", "-").strip("-")
