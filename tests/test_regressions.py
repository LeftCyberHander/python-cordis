"""移植版缺陷回归测试（2026-10 修补轮次）。

每个用例都锁定一条从 ``src/*.ts`` 转录而来的不变量，docstring 标明参考
实现的对应行号；凡与本轮修补相关的，额外注明修补前的错误行为。

运行方式::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cordis_py import Accessor, Context, Exporter, FiberState, Service, symbols  # noqa: E402
from cordis_py.fiber import _run_disposable  # noqa: E402
from cordis_py.utils import DisposableList  # noqa: E402


def run(coro):
    """把协程/可 await 对象驱动至完成；普通值原样返回。"""
    if not inspect.isawaitable(coro):
        return coro
    return asyncio.run(coro)


class WarningGuard(unittest.TestCase):
    """把所有 RuntimeWarning（尤其是未 await 协程）升级为错误。"""

    def setUp(self):
        super().setUp()
        self._cm = warnings.catch_warnings()
        self._cm.__enter__()
        warnings.simplefilter("error", RuntimeWarning)

    def tearDown(self):
        self._cm.__exit__(None, None, None)
        super().tearDown()


class TestFireAndForgetCatchChains(WarningGuard):
    """fiber.ts:299-308 / 545-548 —— fire-and-forget 的 catch 链必须真正执行。"""

    def test_internal_plugin_observer_failure_fully_disposes(self):
        """fiber.ts:303-307：observer 同步抛错时，disposer 要被驱动到完成。

        修补前：``_settle_catch(self.dispose(), ...)`` 的返回值被丢弃，
        拆卸协程从未执行——fiber 停留在半释放状态，且泄漏未 await 协程。
        """
        root = Context()

        def observer(fiber):
            raise RuntimeError("observer boom")

        root.on("internal/plugin", observer)
        with self.assertRaises(RuntimeError):
            root.plugin(lambda ctx, config: None)
        # 拆卸已跑完：runtime 记录被移除，没有残留 fiber
        self.assertEqual(root.registry.size, 0)

    def test_async_effect_body_runs_at_load(self):
        """fiber.ts:522 + utils.ts executor 语义：异步 effect 主体在加载时执行。

        修补前：catch 链被丢弃，异步主体直到 dispose 才运行（甚至永不运行）。
        """
        root = Context()
        log = []

        def plugin(ctx, config):
            async def body():
                log.append("start")
                return lambda: log.append("disposed")

            ctx.effect(body, "async")

        fiber = root.plugin(plugin)
        run(fiber.await_())
        self.assertEqual(log, ["start"])
        run(fiber.dispose())
        self.assertEqual(log, ["start", "disposed"])

    def test_async_effect_with_real_await_completes(self):
        """持久循环下，带真实 await 的 effect 主体会被驱动到完成。"""

        async def scenario():
            root = Context()
            log = []

            def plugin(ctx, config):
                async def body():
                    log.append("start")
                    await asyncio.sleep(0.01)
                    log.append("done")
                    return lambda: log.append("disposed")

                ctx.effect(body, "async")

            fiber = root.plugin(plugin)
            await fiber.await_()
            await asyncio.sleep(0.05)
            self.assertEqual(log, ["start", "done"])
            await fiber.dispose()
            self.assertEqual(log, ["start", "done", "disposed"])

        run(scenario())

    def test_failing_async_effect_is_unloaded_not_raised(self):
        """fiber.ts:545-548：task reject 由 catch 链接住并卸载 effect。

        对应 TS 语义：内层 handler 吞掉错误（只做拆卸），异常不外泄。
        """
        root = Context()
        disposed = []

        def plugin(ctx, config):
            async def bad():
                raise ValueError("async boom")

            def disposer():
                disposed.append(1)

            ctx.effect(bad, "bad")
            ctx.effect(lambda: disposer, "keep")

        fiber = root.plugin(plugin)
        run(fiber.await_())
        labels = {meta.label for meta in fiber.get_effects()}
        self.assertNotIn("bad", labels)  # 失败的 effect 已被卸载
        self.assertIn("keep", labels)
        run(fiber.dispose())
        self.assertEqual(disposed, [1])


class TestInternalUpdateHookOwnership(unittest.TestCase):
    """events.ts:140-155 / 254-260 —— internal/update 钩子归属注册方 fiber。"""

    def test_hook_dies_with_registering_fiber(self):
        """修补前：钩子错挂根 fiber，插件卸载后仍对其它 fiber 的 update 触发。"""
        root = Context()
        calls = []

        def plugin_a(ctx, config):
            ctx.on(
                "internal/update",
                lambda config, no_save, next_: (calls.append("A"), next_(config, no_save))[-1],
            )

        a = root.plugin(plugin_a)
        run(a.await_())
        run(a.dispose())

        def plugin_b(ctx, config):
            pass

        b = root.plugin(plugin_b)
        run(b.await_())
        run(b.update({"x": 1}))
        self.assertEqual(calls, [])

    def test_own_update_still_triggers_hooks(self):
        """events.ts:148-154：插件自己的 update 触发自己注册的钩子。"""
        root = Context()
        calls = []

        def plugin(ctx, config):
            ctx.on(
                "internal/update",
                lambda config, no_save, next_: (calls.append(("plain", config)), next_(config, no_save))[-1],
            )
            ctx.on(
                "internal/update",
                lambda config, no_save, next_: (calls.append(("prepend", config)), next_(config, no_save))[-1],
                {"prepend": True},
            )

        fiber = root.plugin(plugin, {"v": 1})
        run(fiber.await_())
        run(fiber.update({"v": 2}))
        self.assertEqual(
            calls,
            [("prepend", {"v": 2}), ("plain", {"v": 2})],
        )

    def test_prepend_registration_does_not_crash(self):
        """events.ts:143：``hooks.unshift`` —— 修补前 DisposableList 无此方法，
        prepend 的 internal/update 监听器必然抛 AttributeError。"""
        items = DisposableList()
        items.push("b")
        remove = items.unshift("a")
        self.assertEqual(list(items), ["a", "b"])
        self.assertTrue(remove())
        self.assertEqual(list(items), ["b"])
        self.assertFalse(remove())  # 幂等


class TestInternalServiceScopeFilter(unittest.TestCase):
    """reflect.ts:330-333 —— internal/service 只送达同一隔离作用域。"""

    def test_isolated_listener_does_not_receive_foreign_scope(self):
        """修补前：notify 派发时不携带 symbols.filter，跨作用域泄漏。"""
        root = Context()
        seen_root, seen_iso = [], []
        root.on("internal/service", lambda name, value: seen_root.append((name, value)))
        iso = root.isolate("svc")
        iso.on("internal/service", lambda name, value: seen_iso.append((name, value)))

        provider = root.plugin(lambda ctx, config: ctx.provide("svc", 1))
        run(provider.await_())
        run(provider.dispose())
        # 根作用域：激活、进入 UNLOADING、注销 impl 各一次通知
        self.assertEqual(seen_root, [("svc", 1), ("svc", 1), ("svc", None)])
        self.assertEqual(seen_iso, [])

    def test_same_scope_listener_receives_event(self):
        root = Context()
        seen = []
        iso = root.isolate("svc")
        iso.on("internal/service", lambda name, value: seen.append((name, value)))
        provider = iso.plugin(lambda ctx, config: ctx.provide("svc", 7))
        run(provider.await_())
        self.assertEqual(seen, [("svc", 7)])


class TestInterceptMerge(unittest.TestCase):
    """context.ts:139-145 + service.ts:86-102 —— 多级 intercept 按 key 合并。"""

    def test_multi_level_intercept_merges_by_key(self):
        """修补前：扁平映射直接覆盖，祖先条目的其它 key 丢失。"""
        root = Context()
        mid = root.intercept("logger", {"level": 3, "name": "outer"})
        leaf = mid.intercept("logger", {"name": "inner"})

        def plugin(ctx, config):
            logger = ctx.logger()
            results["name"] = logger.name
            results["level"] = logger.level

        results = {}
        run(leaf.plugin(plugin).await_())
        self.assertEqual(results, {"name": "inner", "level": 3})

    def test_single_level_intercept_applies(self):
        root = Context()
        scoped = root.intercept("logger", {"name": "single"})
        seen = {}

        def plugin(ctx, config):
            seen["name"] = ctx.logger().name

        run(scoped.plugin(plugin).await_())
        self.assertEqual(seen["name"], "single")


class TestLoggerCallerRebinding(WarningGuard):
    """logger.ts:203-261 —— ctx.logger 按调用方 fiber 派生名称并持有副作用。"""

    def test_logger_name_derives_from_calling_fiber(self):
        """修补前：服务从不重绑，ctx.logger() 恒得根 fiber 名称 'root'。"""
        root = Context()
        seen = {}

        def my_plugin(ctx, config):
            seen["name"] = ctx.logger().name
            seen["fiber"] = ctx.logger().meta["fiber"]
            ctx.logger.info("hello")

        fiber = root.plugin(my_plugin)
        run(fiber.await_())
        self.assertEqual(seen["name"], "my-plugin")
        self.assertIs(seen["fiber"], fiber._fiber)
        self.assertEqual(root.logger.buffer[-1].name, "my-plugin")

    def test_forwarding_methods_use_calling_fiber(self):
        """ctx.logger.error(...) 与 ctx.logger() 走同一重绑路径。"""
        root = Context()

        def my_plugin(ctx, config):
            ctx.logger.info("careful")

        run(root.plugin(my_plugin).await_())
        self.assertEqual(root.logger.buffer[-1].name, "my-plugin")
        self.assertEqual(root.logger.buffer[-1].args, ["careful"])

    def test_exporter_disposed_with_calling_fiber(self):
        """logger.ts:233：exporter 是调用方 fiber 的 effect。

        修补前：exporter 恒注册在根 fiber 上，插件卸载后泄漏。
        """
        root = Context()
        service = root.logger

        def my_plugin(ctx, config):
            ctx.logger.exporter(Exporter(export=lambda m: None))

        fiber = root.plugin(my_plugin)
        run(fiber.await_())
        self.assertEqual(len(service.exporters), 2)  # buffer + 插件注册的
        run(fiber.dispose())
        self.assertEqual(len(service.exporters), 1)  # 只剩常驻 buffer exporter

    def test_error_with_cause_logs_both(self):
        """logger.ts:143-149：cause 分支不 return——cause 与原错误都记录。

        修补前：只记录 cause，外层错误丢失。
        """
        root = Context()
        try:
            try:
                raise ValueError("inner")
            except ValueError as error:
                raise RuntimeError("outer") from error
        except RuntimeError as error:
            root.logger.error(error)
        messages = [message.args[0] for message in root.logger.buffer]
        self.assertEqual([str(m) for m in messages], ["inner", "outer"])


class TestDispatchThisArgDetection(unittest.TestCase):
    """events.ts:166 —— 前导 thisArg 的识别与 TS 的 typeof 检查一致。"""

    def test_list_first_argument_is_this_arg(self):
        """修补前：list/tuple/set 不被视为 thisArg，``emit([..], 'e')`` 直接把
        列表当事件名查询 dict —— unhashable type 崩溃。"""
        root = Context()
        seen = []
        root.on("e", lambda *args: seen.append(args))
        root.emit([1, 2], "e", "payload")
        self.assertEqual(seen, [("payload",)])

    def test_none_first_argument_is_this_arg(self):
        """TS: ``typeof null === 'object'`` —— None 同样被消费为 thisArg。"""
        root = Context()
        seen = []
        root.on("e", lambda *args: seen.append(args))
        root.emit(None, "e", "payload")
        self.assertEqual(seen, [("payload",)])

    def test_dict_this_arg_still_works(self):
        root = Context()
        seen = []
        root.on("e", lambda *args: seen.append(args))
        root.emit({"a": 1}, "e", "payload")
        self.assertEqual(seen, [("payload",)])


class TestContextIntrospection(unittest.TestCase):
    """reflect.ts:199-205 / 233 / 254 / 346 / 366。"""

    def test_contains_covers_class_members(self):
        """``Reflect.has`` 沿原型链包含 Context 类自身成员。"""
        root = Context()
        for name in ("extend", "isolate", "intercept", "get", "set", "provide", "logger"):
            self.assertIn(name, root, name)
        self.assertNotIn("definitely-not-a-service", root)

    def test_get_uses_calling_scope(self):
        """修补前：``ctx.get`` 退化为根作用域解析，隔离域读到父作用域的值。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", "ROOT")).await_())
        iso = root.isolate("svc")
        run(iso.plugin(lambda ctx, config: ctx.provide("svc", "ISO")).await_())
        self.assertEqual(root.get("svc"), "ROOT")
        self.assertEqual(iso.get("svc"), "ISO")

    def test_accessor_disposed_with_calling_fiber(self):
        """修补前：accessor 恒注册在根 fiber，插件卸载后残留。"""
        root = Context()

        def plugin(ctx, config):
            ctx.accessor("computed", Accessor(get=lambda receiver, error: 42))

        fiber = root.plugin(plugin)
        run(fiber.await_())
        self.assertEqual(root.computed, 42)
        self.assertIn("computed", root.reflect.props)
        run(fiber.dispose())
        self.assertNotIn("computed", root.reflect.props)

    def test_mixin_disposed_with_calling_fiber(self):
        root = Context()

        def plugin(ctx, config):
            ctx.provide("svc", {"ping": lambda: "pong"})
            ctx.mixin("svc", ["ping"])

        fiber = root.plugin(plugin)
        run(fiber.await_())
        self.assertEqual(root.ping(), "pong")
        run(fiber.dispose())
        self.assertNotIn("ping", root.reflect.props)


class TestRunDisposableSemantics(unittest.TestCase):
    """fiber.ts:114 —— ``effectInertia.get(dispose)?.() ?? result``。"""

    def test_falsy_inertia_result_falls_back(self):
        """``??`` 只在 null/undefined 时回退；``or`` 会吞掉 0/''/False。"""
        marker = lambda: "own"  # noqa: E731
        setattr(marker, "__cordis_effect_inertia__", True)
        from cordis_py.fiber import _effect_inertia

        _effect_inertia[id(marker)] = lambda: False
        try:
            self.assertIs(_run_disposable(marker), False)
        finally:
            _effect_inertia.pop(id(marker), None)

    def test_nullish_inertia_falls_back(self):
        marker = lambda: "own"  # noqa: E731
        setattr(marker, "__cordis_effect_inertia__", True)
        from cordis_py.fiber import _effect_inertia

        _effect_inertia[id(marker)] = lambda: None
        try:
            self.assertEqual(_run_disposable(marker), "own")
        finally:
            _effect_inertia.pop(id(marker), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
