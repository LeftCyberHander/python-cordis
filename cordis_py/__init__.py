"""cordis_py —— Cordis 插件框架的 Python 移植版。

公开接口对齐 ``src/index.ts``：:class:`~cordis_py.context.Context` 根上下文、
事件总线、fiber 生命周期、logger、registry，以及 ``Service`` 基类。
TypeScript 构建产物未导出的两个模块（``reflect`` 与内部移植辅助函数）在
这里同样可导入，但不在此处重新导出，与 ``index.ts`` 保持一致。
"""

from .context import Context
from .events import (
    DispatchMode,
    EventOptions,
    Events,
    EventsService,
    Hook,
    is_bailed,
)
from .fiber import (
    CordisError,
    Disposable,
    Effect,
    EffectMeta,
    Fiber,
    FiberState,
    ValidationError,
    resolve_config,
)
from .logger import (
    Exporter,
    Logger,
    LoggerLevel,
    LoggerService,
    Message,
    c16,
    c256,
    default_formatters,
)
from .registry import Inject, Plugin, PluginRuntime, RegistryService, inject_decorator
from .reflect import Accessor, Impl, Property, ReflectService
from .service import Service
from .utils import DisposableList, Tracker, symbols

__version__ = "4.0.2-py"

__all__ = [
    "Context",
    "EventsService",
    "EventOptions",
    "Hook",
    "DispatchMode",
    "Events",
    "is_bailed",
    "Fiber",
    "FiberState",
    "EffectMeta",
    "CordisError",
    "ValidationError",
    "resolve_config",
    "Disposable",
    "Effect",
    "Logger",
    "LoggerService",
    "LoggerLevel",
    "Message",
    "Exporter",
    "default_formatters",
    "c16",
    "c256",
    "RegistryService",
    "Inject",
    "Plugin",
    "PluginRuntime",
    "inject_decorator",
    "Service",
    "DisposableList",
    "Tracker",
    "symbols",
    "Accessor",
    "Property",
    "Impl",
    "ReflectService",
    "__version__",
]
