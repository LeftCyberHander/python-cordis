"""Python 版 cordis 移植的行为测试套件。

运行方式::

    python -m unittest discover -s tests -v

每个测试都对一条从 ``src/*.ts`` 转录而来的具体不变量做断言；docstring 中
标明了对应的参考实现行号，便于把移植代码与 TypeScript 原版逐条核对。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cordis_py import Accessor, Context, Exporter, FiberState, Service, symbols  # noqa: E402


def run(coro):
    """把协程/可 await 对象驱动至完成；普通值原样返回。

    当 effect 的拆卸过程没有需要 await 的东西时，其 disposer 是同步的
    （对应 fiber.ts:492 中 ``finalizeDisposal`` 的非 thenable 分支），
    因此 ``run`` 既要容忍协程，也要容忍裸结果。
    """
    if not inspect.isawaitable(coro):
        return coro
    return asyncio.run(coro)


class TestLifecycle(unittest.TestCase):
    """fiber 的状态迁移与释放语义。"""

    def test_root_fiber_shape(self):
        """context.ts:77 —— 根 fiber 的 uid 为 0、状态为 ACTIVE 且没有 runtime。"""
        root = Context()
        self.assertEqual(root.fiber.uid, 0)
        self.assertEqual(root.fiber.state, FiberState.ACTIVE)
        self.assertIsNone(root.fiber.runtime)
        self.assertEqual(root.fiber.name, "root")

    def test_keyword_plugin_config(self):
        """registry.ts:316 —— 插件加载完成后状态为 ACTIVE，且能拿到配置。"""
        root = Context()
        seen = {}

        def plugin(ctx, config):
            seen["config"] = config

        fiber = root.plugin(plugin, {"n": 1})
        run(fiber.await_())
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertEqual(fiber.uid, 1)
        self.assertEqual(seen["config"], {"n": 1})

    def test_effects_release_in_reverse_order(self):
        """fiber.ts:431 —— effect 按逆序释放其 disposer。"""
        root = Context()
        order = []

        def plugin(ctx, config):
            def outer() -> object:
                ctx.effect(lambda: (order.append("a"), lambda: order.append("a'"))[1], "a")
                ctx.effect(lambda: (order.append("b"), lambda: order.append("b'"))[1], "b")
                return lambda: None

            ctx.effect(outer, "outer")

        fiber = root.plugin(plugin)
        run(fiber.await_())
        self.assertEqual(order, ["a", "b"])
        run(fiber.dispose())
        self.assertEqual(order, ["a", "b", "b'", "a'"])

    def test_dispose_is_idempotent(self):
        """fiber.ts:427 —— 对 effect 的 disposer 重复调用是空操作。"""
        root = Context()
        count = []

        def plugin(ctx, config):
            ctx.effect(lambda: lambda: count.append(1), "once")

        fiber = root.plugin(plugin)
        run(fiber.await_())
        run(fiber.dispose())
        run(fiber.dispose())
        self.assertEqual(len(count), 1)

    def test_disposed_fiber_rejects_new_effects(self):
        """fiber.ts:351 —— 释放之后 assertActive 会抛 CordisError。"""
        root = Context()
        fiber = root.plugin(lambda ctx, config: None)
        run(fiber.await_())
        run(fiber.dispose())
        self.assertIsNone(fiber.uid)
        self.assertEqual(fiber.state, FiberState.DISPOSED)
        with self.assertRaises(Exception):
            fiber._fiber.effect(lambda: None, "after")

    def test_parent_unload_cascades_to_child(self):
        """fiber.ts:265 —— dispose 本身是父 fiber 的一个 effect，故子随父亡。"""
        root = Context()
        events = []
        parent = root.plugin(lambda ctx, config: None)
        run(parent.await_())
        child = parent.ctx.plugin(lambda ctx, config: ctx.effect(lambda: lambda: events.append("child")))
        run(child.await_())
        run(parent.dispose())
        self.assertIn("child", events)
        self.assertIsNone(child.uid)


class TestEvents(unittest.TestCase):
    """各派发策略（events.ts）。"""

    def test_waterfall_composition(self):
        """events.ts:234 —— 监听器包裹最内层的 next，由外向内依次包裹。"""
        root = Context()
        root.on("e", lambda a, nxt: f"A({nxt()})")
        root.on("e", lambda a, nxt: f"B({nxt()})")
        self.assertEqual(root.waterfall("e", 1, lambda: "END"), "A(B(END))")

    def test_waterfall_veto(self):
        """events.ts:230 —— 不调用 next() 即否决整条链，包括内置实现。"""
        root = Context()
        root.on("e", lambda a, nxt: "VETO")
        self.assertEqual(root.waterfall("e", 1, lambda: "NEVER"), "VETO")

    def test_bail_stops_on_truthy(self):
        """events.ts:217 —— bail 在遇到第一个中止值时停下。"""
        root = Context()
        seen = []
        root.on("e", lambda: seen.append(1))
        root.on("e", lambda: "stop")
        root.on("e", lambda: seen.append(3))
        self.assertEqual(root.bail("e"), "stop")
        self.assertEqual(seen, [1])

    def test_bail_ignores_falsey(self):
        """events.ts:13 —— None / False 不算中止值。"""
        root = Context()
        seen = []
        root.on("e", lambda: seen.append("a"))
        root.on("e", lambda: None)
        root.on("e", lambda: seen.append("c"))
        root.bail("e")
        self.assertEqual(seen, ["a", "c"])

    def test_once_disposes_after_first_call(self):
        """events.ts:312 —— once() 在调用监听器之前就把自己摘除。"""
        root = Context()
        calls = []
        root.once("e", lambda: calls.append(1))
        root.emit("e")
        root.emit("e")
        self.assertEqual(calls, [1])

    def test_serial_awaits_in_order(self):
        """events.ts:204 —— serial 逐个 await 监听器，遇到中止值即停。"""
        root = Context()

        def first() -> object:
            async def _inner():
                return None

            return _inner()

        async def scenario():
            order = []

            async def listener_a():
                order.append("a")

            async def listener_b():
                order.append("b")
                return "done"

            async def listener_c():
                order.append("c")

            root.on("e", listener_a)
            root.on("e", listener_b)
            root.on("e", listener_c)
            result = await root.serial("e")
            return order, result

        order, result = run(scenario())
        self.assertEqual(order, ["a", "b"])
        self.assertEqual(result, "done")

    def test_parallel_reports_own_mode(self):
        """events.ts:184 —— parallel() 应上报自己的策略 'parallel'。

        参考实现在这里误报为 'emit'（已知偏离）；诊断必须如实反映策略，
        否则排障时会被误导，故本轮按功能契约修正。
        """
        root = Context()
        seen = []
        root.on("internal/dispatch", lambda mode, name, args, this_arg: seen.append((mode, name)))
        run(root.parallel("custom"))
        self.assertIn(("parallel", "custom"), seen)

    def test_listener_dies_with_fiber(self):
        """events.ts:254 —— 监听器是注册它的 fiber 所持有的 effect。"""
        root = Context()
        seen = []
        fiber = root.plugin(lambda ctx, config: ctx.on("e", lambda: seen.append(1)))
        run(fiber.await_())
        root.emit("e")
        run(fiber.dispose())
        root.emit("e")
        self.assertEqual(seen, [1])


class TestServices(unittest.TestCase):
    """服务注册、注入与隔离（reflect.ts）。"""

    def test_provide_and_read(self):
        """reflect.ts:277 —— 已 provide 的服务可从其自身 fiber 解析到。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", {"v": 1})).await_())
        self.assertEqual(root.svc["v"], 1)

    def test_inject_waits_for_provider(self):
        """fiber.ts:625 —— 在依赖满足之前，fiber 一直保持 PENDING。

        JS 中消费方的重新激活跑在共享的微任务队列上；Python 中它是被调度的
        任务，因此测试在提供方注册之后显式 await 消费方来驱动它。
        """
        root = Context()
        seen = []

        def consumer(ctx, config):
            seen.append("loaded")

        consumer.inject = ["svc"]
        fiber = root.plugin(consumer)
        run(fiber.await_())
        self.assertEqual(fiber.state, FiberState.PENDING)
        self.assertEqual(seen, [])

        async def provide_and_settle():
            await root.plugin(lambda ctx, config: ctx.provide("svc", 1)).await_()
            await fiber.await_()

        run(provide_and_settle())
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertEqual(seen, ["loaded"])

    def test_dependent_unloads_when_provider_goes_away(self):
        """reflect.ts:297 —— 移除某个 impl 会通知并卸载依赖它的一方。"""
        root = Context()
        seen = []
        provider = root.plugin(lambda ctx, config: ctx.provide("svc", 1))
        run(provider.await_())

        def consumer(ctx, config):
            seen.append("up")

        consumer.inject = ["svc"]
        cf = root.plugin(consumer)
        run(cf.await_())
        self.assertEqual(cf.state, FiberState.ACTIVE)
        run(provider.dispose())
        self.assertEqual(cf.state, FiberState.PENDING)
        self.assertEqual(seen, ["up"])

    def test_isolate_scopes_service(self):
        """context.ts:121 —— 隔离作用域内的 provide 与父作用域互不影响。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", "ROOT")).await_())
        iso = root.isolate("svc")
        run(iso.plugin(lambda ctx, config: ctx.provide("svc", "SCOPED")).await_())
        self.assertEqual(root.svc, "ROOT")
        self.assertEqual(iso.svc, "SCOPED")

    def test_isolate_join_via_label(self):
        """context.ts:115 —— 两次 isolate() 若 label 相同则加入同一作用域。"""
        root = Context()
        a = root.isolate("svc", "shared")
        b = root.isolate("svc", "shared")
        run(a.plugin(lambda ctx, config: ctx.provide("svc", "ONE")).await_())
        self.assertEqual(b.svc, "ONE")

    def test_provide_twice_in_same_scope_raises(self):
        """reflect.ts:289 —— 在同一作用域内重复注册是错误。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", 1)).await_())
        with self.assertRaises(Exception):
            run(root.plugin(lambda ctx, config: ctx.provide("svc", 2)).await_())

    def test_accessor(self):
        """reflect.ts:345 —— accessor 在每次读取时重新计算该属性。"""
        root = Context()
        state = {"n": 0}
        run(
            root.plugin(
                lambda ctx, config: ctx.accessor(
                    "computed",
                    Accessor(get=lambda receiver, error: state["n"]),
                )
            ).await_()
        )
        self.assertEqual(root.computed, 0)
        state["n"] = 5
        self.assertEqual(root.computed, 5)

    def test_get_without_inject(self):
        """reflect.ts:233 —— ctx.get 读取服务时不看 inject。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", 42)).await_())
        self.assertEqual(root.get("svc"), 42)
        self.assertIsNone(root.get("missing"))

    def test_service_base_class(self):
        """service.ts:42 —— 类插件会构造服务并完成注册。

        参考写法是 ``ctx.plugin(Greeter)``：`Fiber` 实例化该类并返回
        ``instance[Service.init]?.()``（此处为 undefined），真正的注册由
        构造函数里的 ``ctx.reflect.provide`` 完成。
        """

        class Greeter(Service):
            provide = "greeter"

            def hello(self, name):
                return "hi " + name

        root = Context()
        run(root.plugin(Greeter).await_())
        self.assertEqual(root.greeter.hello("bob"), "hi bob")

    def test_property_declared_once_globally(self):
        """reflect.ts:280 —— props 是进程级全局的；第二个 fiber 重复声明同一
        服务是被允许的（同一种类），但 accessor 与 service 冲突则不允许。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", 1)).await_())
        with self.assertRaises(Exception):
            run(
                root.plugin(
                    lambda ctx, config: ctx.accessor("svc", Accessor(get=lambda r, e: 1))
                ).await_()
            )


class TestRegistry(unittest.TestCase):
    """插件注册表的身份判定与更新（registry.ts）。"""

    def test_runtime_reused_per_callback(self):
        """registry.ts:322 —— 同一个 callback 的所有 fiber 共享一条 runtime 记录。"""
        root = Context()
        shared = {}

        def plugin(ctx, config):
            shared["n"] = shared.get("n", 0) + 1

        f1 = root.plugin(plugin)
        f2 = root.plugin(plugin)
        run(f1.await_())
        run(f2.await_())
        self.assertEqual(root.registry.size, 1)
        self.assertEqual(shared["n"], 2)

    def test_registry_delete_disposes_fibers(self):
        """registry.ts:258 —— delete() 会释放该插件的每一个 fiber。"""
        root = Context()
        seen = []

        def plugin(ctx, config):
            ctx.effect(lambda: lambda: seen.append(1))

        fiber = root.plugin(plugin)
        run(fiber.await_())
        root.registry.delete(plugin)
        self.assertEqual(root.registry.size, 0)
        self.assertEqual(len(seen), 1)

    def test_update_restarts_plugin(self):
        """fiber.ts:736 —— update() 重新解析配置并重启该 fiber。"""
        root = Context()
        configs = []

        def plugin(ctx, config):
            configs.append(config.get("v"))

        fiber = root.plugin(plugin, {"v": 1})
        run(fiber.await_())
        run(fiber.update({"v": 2}))
        self.assertEqual(configs, [1, 2])

    def test_restart_cycles_effects(self):
        """fiber.ts:718 —— restart() 先卸载再原地重新加载。"""
        root = Context()
        events = []

        def plugin(ctx, config):
            ctx.effect(lambda: (events.append("up"), lambda: events.append("down"))[1])

        fiber = root.plugin(plugin)
        run(fiber.await_())
        run(fiber.restart())
        self.assertEqual(events, ["up", "down", "up"])


class TestLogger(unittest.TestCase):
    """logger 的行为（logger.ts）。"""

    def test_named_logger(self):
        """logger.ts:251 —— ctx.logger(name) 返回带该名字的门面。"""
        root = Context()
        logger = root.logger("mymod")
        self.assertEqual(logger.name, "mymod")

    def test_buffer_exporter_is_resident(self):
        """logger.ts:213 —— 内置 buffer exporter 在构造时安装并常驻。

        参考实现在 context.ts:82 丢弃了它的 disposer；本轮不再丢弃，disposer
        仍在根 fiber 上，但没有任何代码会去执行它，因此效果同样是常驻。
        """
        root = Context()
        root.logger("mod").info("hello")
        self.assertEqual(len(root.logger.buffer), 1)
        self.assertEqual(root.logger.buffer[-1].args, ["hello"])

    def test_each_exporter_disposer_releases_its_own_entry(self):
        """logger.ts:234 —— 每个 disposer 只释放自己登记的那一条。

        参考实现在 disposer 执行时才读 _snExporter，导致释放 A 却删掉 B、
        且会被反复删除（已知缺陷）；资源归还必须精确，故本轮修正。
        """
        root = Context()
        service = root.logger
        first = service.exporter(Exporter(export=lambda m: None))
        second = service.exporter(Exporter(export=lambda m: None))
        before = len(service.exporters)
        run(first())
        self.assertEqual(len(service.exporters), before - 1)
        run(second())
        self.assertEqual(len(service.exporters), before - 2)

    def test_format_placeholders(self):
        """logger.ts:99 —— printf 占位符按各 exporter 的 formatters 展开。"""
        from cordis_py import Exporter, Logger, Message

        exporter = Exporter(export=lambda m: None)
        message = Message(1, 0, "mod", "info", 1, ["%s=%d", "x", 3])
        self.assertEqual(Logger.format(exporter, message), "x=3")


class TestUtils(unittest.TestCase):
    """DisposableList 与符号辅助工具（utils.ts）。"""

    def test_clear_returns_reverse_order(self):
        """utils.ts:27 —— clear() 按注册顺序的逆序返回值。"""
        from cordis_py import DisposableList

        items = DisposableList()
        items.push("a")
        items.push("b")
        items.push("c")
        self.assertEqual(items.clear(), ["c", "b", "a"])

    def test_delete_by_value(self):
        """utils.ts:21 —— 按值删除，O(1)。"""
        from cordis_py import DisposableList

        items = DisposableList()
        token = lambda: None  # noqa: E731
        items.push(token)
        self.assertTrue(items.delete(token))
        self.assertEqual(len(items), 0)

    def test_symbol_roundtrip(self):
        """utils.ts:50 —— symbol 键对应 `Symbol.for('cordis.*')`。"""
        self.assertEqual(symbols.name_of(symbols.effect), "cordis.effect")
        self.assertEqual(symbols.name_of(symbols.isolate), "cordis.isolate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
