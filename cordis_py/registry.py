"""插件注册表、依赖注入与插件入口类型。

``src/registry.ts`` 的 Python 移植。用 Python 装饰器替代 TypeScript 的
``@Inject`` 类/方法装饰器，``Plugin.Runtime`` 变为一个小型数据类；注册表
语义（按解析后的 callback 判等、每个 callback 一个 runtime、每次 ``plugin()``
调用一个 fiber）保持不变。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .fiber import Fiber
from .utils import DisposableList, Tracker, define_property, symbols

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context

__all__ = ["Inject", "inject_decorator", "resolve_inject", "Plugin", "PluginRuntime", "RegistryService"]


class Inject:
    """插件依赖声明的归一化表示。"""

    __slots__ = ("names", "configs")

    def __init__(self, names: Optional[List[str]] = None, configs: Optional[Dict[str, Any]] = None) -> None:
        self.names = names or []
        self.configs = configs or {}

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {name: self.configs.get(name) for name in self.names}
        result.update({k: v for k, v in self.configs.items() if k not in result})
        return result

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Inject({self.as_dict()!r})"


def resolve_inject(inject: Any, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把数组/对象/类继承得来的 inject 元数据统一转成普通映射。"""
    if result is None:
        result = {}
    if not inject:
        return result
    if isinstance(inject, Inject):
        for name in inject.names:
            result[name] = inject.configs.get(name, None)
        for name, config in inject.configs.items():
            result.setdefault(name, config)
        return result
    if isinstance(inject, (list, tuple, set)):
        for name in inject:
            result[name] = None
        return result
    if isinstance(inject, dict):
        for name, config in inject.items():
            result[name] = None if config is None else config
        return result
    return result


def inject_decorator(*deps: Any, **configs: Any) -> Callable[[Any], Any]:
    """``@Inject`` 在 Python 插件可调用对象/类上的装饰器形式。

    接受与 TypeScript 装饰器相同的形态：位置参数给出的服务名（数组形式），
    以及/或者「服务名 → intercept 配置」的关键字参数对（对象形式）。
    """

    def _wrap(target: Any) -> Any:
        current = resolve_inject(getattr(target, "inject", None))
        inject = Inject(names=list(deps), configs=dict(configs))
        for name, config in resolve_inject(inject).items():
            current.setdefault(name, config)
        try:
            setattr(target, "inject", current)
        except (AttributeError, TypeError):  # pragma: no cover - 内置对象
            setattr(target, "__cordis_inject__", current)
        return target

    return _wrap


class PluginRuntime:
    """可变的注册表记录，由同一个插件 callback 的所有 fiber 共享。"""

    __slots__ = ("name", "fibers", "callback", "Config")

    def __init__(self, name: Optional[str], callback: Callable[..., Any], Config: Any = None) -> None:
        self.name = name
        self.callback = callback
        self.fibers: DisposableList = DisposableList()
        self.Config = Config

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"PluginRuntime({self.name!r}, fibers={len(self.fibers)})"


class Plugin:
    """受支持的各类插件入口形态的命名空间。"""

    Runtime = PluginRuntime


def is_applicable(obj: Any) -> bool:
    """``{ apply }`` 形态的插件（dict 与对象都算）。

    ``registry.ts:8`` 的判据是 ``typeof object === 'object' && typeof object.apply
    === 'function'``；Python 里 dict 没有 ``.apply`` 属性，必须单独取键判断，
    否则 ``ctx.inject()`` 内部构造的 ``{inject, apply}`` 插件会被判为非法。
    """
    if obj is None or isinstance(obj, type):
        return False
    if isinstance(obj, dict):
        return callable(obj.get("apply"))
    return hasattr(obj, "apply") and callable(getattr(obj, "apply"))


def plugin_attr(plugin: Any, key: str) -> Any:
    """读取插件入口上的元数据（``inject`` / ``Config`` / ``name``）。

    dict 形态取键，对象形态取属性——两种入口在所有读取点都必须一致。
    """
    if isinstance(plugin, dict):
        return plugin.get(key)
    return getattr(plugin, key, None)


class RegistryService:
    """作为 ``ctx.registry`` 安装、并混入 ``ctx`` 的插件注册表。"""

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._counter = 0
        self._internal: Dict[Any, PluginRuntime] = {}
        define_property(self, symbols.tracker, Tracker(property="ctx", no_shadow=True))

    @property
    def counter(self) -> int:
        """分配下一个 fiber uid（每次读取都会自增）。"""
        self._counter += 1
        return self._counter

    @property
    def size(self) -> int:
        return len(self._internal)

    def resolve(self, plugin: Any) -> Optional[Callable[..., Any]]:
        """把受支持的插件形态解析为可执行的 callback。"""
        try:
            if callable(plugin) or isinstance(plugin, type):
                return plugin
            if is_applicable(plugin):
                return plugin.get("apply") if isinstance(plugin, dict) else plugin.apply
        except Exception:  # noqa: BLE001 - 对应原实现里的空 catch
            return None
        return None

    def get(self, plugin: Any) -> Optional[PluginRuntime]:
        key = self.resolve(plugin)
        return self._internal.get(key) if key else None

    def has(self, plugin: Any) -> bool:
        key = self.resolve(plugin)
        return bool(key) and key in self._internal

    def delete(self, plugin: Any) -> Optional[PluginRuntime]:
        """释放某个插件的所有运行中 fiber，并移除它的 runtime。

        对应 ``registry.ts:258``：先丢弃 runtime 记录，再逐个释放 fiber。
        disposer 返回的可 await 拆卸结果会在有事件循环时被驱动至完成
        （JS 路径会直接丢弃未 await 的 promise，这里保留可观察性以便测试）。
        """
        key = self.resolve(plugin)
        runtime = self._internal.get(key) if key else None
        if runtime is None:
            return None
        del self._internal[key]
        for fiber in list(runtime.fibers):
            result = fiber.dispose()
            _drive(result)
        return runtime

    def keys(self):
        return self._internal.keys()

    def values(self):
        return self._internal.values()

    def entries(self):
        return self._internal.items()

    def for_each(self, callback: Callable[[PluginRuntime, Callable[..., Any]], None]) -> None:
        for key, value in self._internal.items():
            callback(value, key)

    def inject(self, inject: Any, callback: Callable[..., Any], _caller: Any = None) -> Fiber:
        """在所请求的依赖就绪后启动一个 callback。"""

        def _apply(ctx: "Context", config: Any) -> Any:
            return callback(ctx, config)

        _apply.inject = resolve_inject(inject)
        _apply.name = getattr(callback, "__name__", "inject")
        return self.plugin(
            {"inject": resolve_inject(inject), "apply": _apply, "name": _apply.name},
            None,
            _caller=_caller,
        )

    def plugin(self, plugin: Any, config: Any = None, *extra: Any, _caller: Any = None) -> Any:
        """在当前上下文中启动一个插件，并返回其 fiber。

        ``_caller`` 是调用 ``ctx.plugin()`` 的那个上下文。内置注册表是
        根作用域的单例，因此必须显式把调用方传进来——否则每个插件都会被
        挂到根上下文下，隔离作用域会静默失效。
        """
        callback = self.resolve(plugin)
        if callback is None:
            raise TypeError(
                'invalid plugin, expect a callable or an object with an "apply" method, received '
                + type(plugin).__name__
            )
        parent = _caller if _caller is not None else self.ctx
        parent.fiber.assert_active()

        runtime = self._internal.get(callback)
        if runtime is None:
            name = plugin_attr(plugin, "name")
            if name is None:
                name = getattr(plugin, "__name__", None)
            if name == "apply":
                name = None
            runtime = PluginRuntime(name, callback, plugin_attr(plugin, "Config"))
            self._internal[callback] = runtime

        inject = plugin_attr(plugin, "inject")
        if inject is None:
            inject = plugin_attr(plugin, "__cordis_inject__")
        fiber = Fiber(parent, config, resolve_inject(inject), runtime, _outer_stack)
        return _FiberHandle(fiber)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"RegistryService(size={self.size})"


class _FiberHandle:
    """同时可 await 的 fiber —— 对应 ``Fiber & PromiseLike<Fiber>``。

    原实现中的 ``Object.create(fiber)`` 通过原型链暴露 fiber 的自有属性；
    这里通过把属性访问转发给被包裹的 fiber 达到同样效果。
    """

    __slots__ = ("_fiber",)

    def __init__(self, fiber: Fiber) -> None:
        object.__setattr__(self, "_fiber", fiber)

    def __getattr__(self, prop: str) -> Any:
        return getattr(object.__getattribute__(self, "_fiber"), prop)

    def __setattr__(self, prop: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_fiber"), prop, value)

    def __await__(self):
        return object.__getattribute__(self, "_fiber").await_().__await__()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"FiberHandle({object.__getattribute__(self, '_fiber')!r})"


def _outer_stack() -> List[str]:
    import traceback

    return traceback.format_stack()[:-2]


def _drive(value: Any) -> Any:
    """驱动可 await 的 disposer 结果，而不要求调用方 await。

    JS 的 ``fiber.dispose()`` 返回一个注册表有意忽略的 promise。未被 await
    的协程永远不会执行其主体，因此这里把拆卸驱动至完成：有运行中的事件
    循环时调度到该循环上，否则在一个私有循环上步进至完成，使同步调用方
    （以及测试）能观察到清理确实发生。
    """
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_as_coroutine(value))
    return asyncio.ensure_future(_as_coroutine(value))


async def _as_coroutine(value: Any) -> Any:
    return await value
