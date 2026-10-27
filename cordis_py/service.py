"""在 ``ctx`` 上以具名 API 形式暴露服务的基类。

``src/service.ts`` 的 Python 移植。子类调用 ``super().__init__(ctx, name)``；
服务会立即完成注册，并随所属 fiber 一同移除。

TypeScript 中的 ``symbols.associate`` / ``symbols.filter`` / ``symbols.extend``
装饰标记，在这里映射为 Python 方法（``_cordis_filter`` / ``_cordis_extend``）
以及 ``<name>.<property>`` 访问器约定，由可追踪包装经 ``reflect.props`` 查询。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from .utils import Tracker, create_callable, define_property, join_prototype, symbols

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context

__all__ = ["Service"]


class Service:
    """在上下文上以某个名字注册的服务的基类。"""

    # 以 Symbol 为键的类属性，对应 TS 中的 `static readonly` 字段。
    init = symbols.init
    check = symbols.check
    config = symbols.config
    invoke = symbols.invoke
    extend = symbols.extend
    tracker = symbols.tracker
    resolve_config = symbols.resolve_config

    def __init__(self, ctx: "Context", name: Optional[str] = None) -> None:
        name = name or getattr(type(self), "provide", None)
        ctx_holder = {"ctx": ctx}

        tracker = Tracker(associate=name, property="ctx")
        self.__dict__.setdefault("_cordis_name", name)
        instance: Any = self
        if getattr(self, symbols.invoke, None) is not None:
            proto = join_prototype(type(self), object)
            instance = create_callable(name, proto, tracker)
            instance.__dict__.update(self.__dict__)
        instance.name = name
        instance.ctx_holder = ctx_holder
        define_property(instance, symbols.tracker, tracker)
        self._cordis_instance = instance

        check = getattr(self, symbols.check, None)
        # 必须把 `ctx` 作为 caller 传下去：reflect 是根上的单例，若让它按自身的
        # `ctx` 注册，隔离作用域里的服务会全部落到根上（第二个实例直接撞名）。
        ctx.reflect.provide(name, instance, check if callable(check) else None, caller=ctx)

    # -- 可覆写钩子 ----------------------------------------------------------
    def _cordis_filter(self, ctx: "Context") -> bool:
        """默认隔离过滤器：本服务名在该上下文中的 label 相同即通过。"""
        from .reflect import _chain_isolate
        return _chain_isolate(ctx).get(self.name) == _chain_isolate(self.ctx_holder["ctx"]).get(self.name)

    def _cordis_extend(self, props: Optional[Dict[str, Any]] = None) -> Any:
        """派生一个扩展后的服务实例，可选地叠加 ``props``。"""
        if getattr(self, symbols.invoke, None) is not None:
            base = create_callable(self.name, type(self), Tracker(associate=self.name, property="ctx"))
        else:
            base = self
        if props:
            for key, value in props.items():
                setattr(base, key, value)
        return base

    def resolve_config(self, base: Any = None, head: Any = None) -> Dict[str, Any]:
        """合并祖先链上的 intercept 配置，并可选地叠加 base 与 head。"""
        ctx = self.ctx_holder["ctx"]
        intercept = getattr(ctx, symbols.intercept)
        configs: list = []
        # 从最外层（最靠近根）向内遍历，使更近的 intercept 覆盖更远的。
        walk = intercept
        while isinstance(walk, dict) and self.name in walk:
            configs.insert(0, walk[self.name])
            walk = walk.get("__cordis_parent_intercept__")
        if base:
            configs.insert(0, base)
        if head:
            configs.append(head)
        merge = getattr(self, "Config", None)
        merge_fn = getattr(merge, "merge", None)
        if callable(merge_fn):
            return merge_fn(*configs)
        result: Dict[str, Any] = {}
        for config in configs:
            if isinstance(config, dict):
                result.update(config)
        return result

    @property
    def ctx(self) -> "Context":
        return self.ctx_holder["ctx"]

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Service {self.name}>"

    def __class_getitem__(cls, item: Any) -> Any:  # pragma: no cover - 类型标注垫片
        return cls
