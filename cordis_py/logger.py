"""日志门面、日志服务、消息、导出器与格式化。

``src/logger.ts`` 的 Python 移植。可调用的 ``ctx.logger`` 服务、printf 风格
格式化器、ANSI 名称着色与级别阈值都原样保留——包括 ``logger.ts:234`` 处的
exporter 释放缺陷。

该缺陷是刻意复刻的：disposer 在**执行时**读取 ``_sn_exporter``，而不是在
注册时捕获序号，因此在存在多个 exporter 时只有最后一个能被移除（且会被
反复移除），被撤回的 exporter 会常驻；单 exporter 路径是正确的。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .utils import Tracker, create_callable, define_property, hyphenate, join_prototype, now_ms, symbols

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context

__all__ = [
    "LoggerType",
    "LoggerLevel",
    "Message",
    "Exporter",
    "default_formatters",
    "Logger",
    "LoggerService",
    "c16",
    "c256",
]

LoggerType = str

ERROR = 0
INFO = 1
WARN = 2
DEBUG = 3


class LoggerLevel:
    """数值化的严重级别，供 exporter 判断是否输出某条消息。"""

    ERROR = ERROR
    INFO = INFO
    WARN = WARN
    DEBUG = DEBUG


class Message:
    """投递给 exporter 的结构化日志记录。"""

    __slots__ = ("sn", "ts", "name", "type", "level", "args", "fiber")

    def __init__(self, sn: int, ts: int, name: str, type: str, level: int, args: List[Any], fiber: Any = None) -> None:
        self.sn = sn
        self.ts = ts
        self.name = name
        self.type = type
        self.level = level
        self.args = args
        self.fiber = fiber

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Message(#{self.sn} {self.name} {self.type} {self.args!r})"


class Exporter:
    """接收结构化日志消息的输出端。"""

    __slots__ = ("colors", "max_length", "levels", "formatters", "export")

    def __init__(
        self,
        export: Callable[[Message], None],
        colors: Any = False,
        max_length: int = 10240,
        levels: Optional[Dict[str, int]] = None,
        formatters: Optional[Dict[str, Callable[..., Any]]] = None,
    ) -> None:
        self.export = export
        self.colors = colors
        self.max_length = max_length
        self.levels = levels
        self.formatters = formatters


def _formatter_s(value: Any, exporter: Exporter, message: Message) -> str:
    return str(value)


def _formatter_int(value: Any, exporter: Exporter, message: Message) -> int:
    return int(float(value))


def _formatter_o(value: Any, exporter: Exporter, message: Message) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 - 不可序列化的值退化为 str
        return str(value)


def _formatter_c(value: Any, exporter: Exporter, message: Message) -> str:
    return Logger.color(exporter, Logger.code(message.name, exporter.colors), value)


default_formatters: Dict[str, Callable[..., Any]] = {
    "s": _formatter_s,
    "d": _formatter_int,
    "i": _formatter_int,
    "f": lambda value, exporter, message: float(value),
    "o": _formatter_o,
    "O": _formatter_o,
    "c": lambda value, exporter, message: "",
    "C": _formatter_c,
}


# 用于给 logger 名称着色的 ANSI 调色板。
c16 = [6, 2, 3, 4, 5, 1]
c256 = [
    20, 21, 26, 27, 32, 33, 38, 39, 40, 41, 42, 43, 44, 45, 56, 57, 62,
    63, 68, 69, 74, 75, 76, 77, 78, 79, 80, 81, 92, 93, 98, 99, 112, 113,
    129, 134, 135, 148, 149, 160, 161, 162, 163, 164, 165, 166, 167, 168,
    169, 170, 171, 172, 173, 178, 179, 184, 185, 196, 197, 198, 199, 200,
    201, 202, 203, 204, 205, 206, 207, 208, 209, 214, 215, 220, 221,
]


class Logger:
    """某个具名子系统的日志门面。"""

    _METHOD_LEVELS = {"error": ERROR, "info": INFO, "warn": WARN, "debug": DEBUG}

    def __init__(self, options: Dict[str, Any], service: "LoggerService") -> None:
        self.name: str = options["name"]
        self.level: Optional[int] = options.get("level")
        self.meta: Dict[str, Any] = options.get("meta") or {}
        self._service = service

    # -- 静态辅助 ------------------------------------------------------------
    @staticmethod
    def color(exporter: Exporter, code: int, value: Any, decoration: str = "") -> str:
        if not exporter.colors:
            return f"{value}"
        suffix = f"{code}" if code < 8 else f"8;5;{code}"
        deco = decoration if (isinstance(exporter.colors, int) and exporter.colors >= 2) else ""
        return f"\u001b[3{suffix}{deco}m{value}\u001b[0m"

    @staticmethod
    def code(name: str, level: Any = None) -> int:
        hash_value = 0
        for char in name:
            hash_value = ((hash_value << 3) - hash_value) + ord(char) + 13
            hash_value |= 0
            # 模拟 JavaScript 的 32 位整数溢出。
            if hash_value >= 2 ** 31:
                hash_value -= 2 ** 32
            elif hash_value < -(2 ** 31):
                hash_value += 2 ** 32
        palette = c256 if isinstance(level, int) and level >= 2 else c16
        return palette[abs(hash_value) % len(palette)]

    @staticmethod
    def format(exporter: Exporter, message: Message) -> str:
        args = list(message.args)
        if args and isinstance(args[0], BaseException):
            first = args[0]
            args[0] = getattr(first, "__cordis_outer__", None) or first.args[0] if first.args else str(first)
            args.insert(0, "%s")
        elif not args or not isinstance(args[0], str):
            args.insert(0, "%o")

        fmt: str = args.pop(0)
        output: List[str] = []
        index = 0
        while index < len(fmt):
            char = fmt[index]
            if char == "%" and index + 1 < len(fmt):
                nxt = fmt[index + 1]
                if nxt == "%":
                    output.append("%")
                    index += 2
                    continue
                formatter = (exporter.formatters or {}).get(nxt) or default_formatters.get(nxt)
                if formatter is not None and args:
                    value = args.pop(0)
                    output.append(str(formatter(value, exporter, message)))
                    index += 2
                    continue
            output.append(char)
            index += 1
        fmt = "".join(output)

        o_formatter = (exporter.formatters or {}).get("o") or default_formatters["o"]
        for arg in args:
            if isinstance(arg, (dict, list, tuple)) and arg:
                arg = o_formatter(arg, exporter, message)
            fmt += " " + str(arg)

        max_length = exporter.max_length or 10240
        return "\n".join(
            line[:max_length] + ("..." if len(line) > max_length else "")
            for line in _split_lines(fmt)
        )

    # -- 实例 API ------------------------------------------------------------
    def error(self, *args: Any) -> None:
        self._dispatch("error", ERROR, args)

    def info(self, *args: Any) -> None:
        self._dispatch("info", INFO, args)

    def warn(self, *args: Any) -> None:
        self._dispatch("warn", WARN, args)

    def debug(self, *args: Any) -> None:
        self._dispatch("debug", DEBUG, args)

    def _dispatch(self, type: str, level: int, args: Any) -> None:
        arg_list = list(args)
        if len(arg_list) == 1 and isinstance(arg_list[0], BaseException):
            error = arg_list[0]
            cause = getattr(error, "__cause__", None)
            if cause is not None:
                # 先记 cause，再记外层——错误链要完整，不能只留最内层。
                self._dispatch(type, level, [cause])
            else:
                nested = getattr(error, "exceptions", None)
                if nested:
                    for inner in nested:
                        self._dispatch(type, level, [inner])

        self._service._sn_message += 1
        sn = self._service._sn_message
        ts = now_ms()
        for exporter in list(self._service.exporters.values()):
            levels = exporter.levels or {}
            target_level = levels.get(self.name, levels.get("default", self.level if self.level is not None else INFO))
            if target_level < level:
                continue
            message = Message(sn, ts, self.name, type, level, arg_list)
            if self.meta:
                for key, value in self.meta.items():
                    setattr(message, key, value)
            exporter.export(message)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Logger({self.name!r})"


class LoggerService:
    """内置日志服务，可按 ``ctx.logger()`` 调用。"""

    def __init__(self, ctx: "Context") -> None:
        holder = {"ctx": ctx}
        tracker = Tracker(property="ctx", no_shadow=True)
        self.ctx_holder = holder
        self.buffer_size = 1000
        self.buffer: List[Message] = []
        self._sn_message = 0
        self._sn_exporter = 0
        self.exporters: Dict[int, Exporter] = {}
        self.name = "logger"

        self._callable = _LoggerCallable(self, tracker)
        self._cordis_holder = holder
        # 追踪元数据必须挂在服务本身上：`ctx.logger` 靠它做调用方重绑，
        # 少了这一行，`ctx.logger()` 永远拿到根 fiber 的名字（'root'）。
        define_property(self, symbols.tracker, tracker)
        # 服务以**自身**注册：``ctx.logger`` 既要可调用（``ctx.logger(name)``），
        # 又要直接暴露 `error/info/warn/debug`，对应 logger.ts:203 中的可调用服务。
        ctx.reflect.provide("logger", self, None, caller=ctx)

        self.exporter(
            Exporter(
                export=self._buffer_export,
                colors=3,
            )
        )

    def __call__(self, name: Optional[str] = None) -> Logger:
        return self._invoke(name)

    @property
    def ctx(self) -> "Context":
        return self.ctx_holder["ctx"]

    # -- exporter 管理 -------------------------------------------------------
    def exporter(self, exporter: Exporter) -> Callable[[], Any]:
        """注册一个 exporter，并让它随当前 fiber 一同释放。"""

        def _setup() -> Callable[[], None]:
            self._sn_exporter += 1
            # 序号必须在**注册时**捕获：disposer 描述的是「这一条」exporter，
            # 执行时再去读 `_sn_exporter` 会删掉别人的条目。
            sn = self._sn_exporter
            self.exporters[sn] = exporter
            return lambda: self.exporters.pop(sn, None) is not None

        return self.ctx.effect(_setup, "ctx.logger.exporter()")

    # -- 可调用主体 ----------------------------------------------------------
    def _invoke(self, name: Optional[str] = None) -> Logger:
        config = self._resolve_config()
        fiber = getattr(self.ctx, "fiber")
        if name is None:
            name = config.get("name")
        if name is None:
            name = hyphenate(fiber.name)
        return Logger(
            {"name": name, "level": config.get("level"), "meta": {"fiber": fiber}},
            self,
        )

    def _resolve_config(self) -> Dict[str, Any]:
        intercept = getattr(self.ctx, symbols.intercept)
        configs: List[Dict[str, Any]] = []
        walk = intercept
        while isinstance(walk, dict) and "logger" in walk:
            configs.insert(0, walk["logger"])
            walk = walk.get("__cordis_parent_intercept__")
        result: Dict[str, Any] = {}
        for config in configs:
            if isinstance(config, dict):
                result.update(config)
        return result

    # -- 转发器 --------------------------------------------------------------
    def error(self, *args: Any) -> None:
        self._invoke().error(*args)

    def info(self, *args: Any) -> None:
        self._invoke().info(*args)

    def warn(self, *args: Any) -> None:
        self._invoke().warn(*args)

    def debug(self, *args: Any) -> None:
        self._invoke().debug(*args)

    def _buffer_export(self, message: Message) -> None:
        self.buffer.append(message)
        if len(self.buffer) > self.buffer_size:
            self.buffer = self.buffer[-self.buffer_size :]

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"LoggerService(exporters={len(self.exporters)})"


class _LoggerCallable:
    """绑定到 ``ctx.logger`` 上的可调用对象。"""

    __slots__ = ("service", "tracker")

    def __init__(self, service: LoggerService, tracker: Tracker) -> None:
        self.service = service
        self.tracker = tracker

    def __call__(self, name: Optional[str] = None) -> Logger:
        return self.service._invoke(name)

    def error(self, *args: Any) -> None:
        self.service.error(*args)

    def info(self, *args: Any) -> None:
        self.service.info(*args)

    def warn(self, *args: Any) -> None:
        self.service.warn(*args)

    def debug(self, *args: Any) -> None:
        self.service.debug(*args)

    @property
    def buffer(self) -> List[Message]:
        return self.service.buffer

    def exporter(self, exporter: Exporter) -> Callable[[], Any]:
        return self.service.exporter(exporter)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return "logger"


def _split_lines(text: str) -> List[str]:
    import re

    return re.split(r"\r?\n", text)
