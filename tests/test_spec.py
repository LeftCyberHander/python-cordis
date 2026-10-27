"""cordis_py 的功能规约测试：加载 / 解析 / 卸载三大核心能力及其余能力。

与 ``test_cordis.py``（逐行对照参考实现）的分工不同，本文件锁定的是
**功能契约本身**——调用方可以依赖的行为，不依赖任何具体实现的写法：

- **加载**：配置解析、初始化、依赖满足才激活、失败可观测、重复注册报错；
- **解析**：显式依赖声明、隔离作用域、计算型属性、混入成员、未提供服务的报错；
- **卸载**：effect 逆序且恰好一次、监听器随插件消亡、服务注销、依赖方退回等待、
  幂等、注册表回收；
- **其余**：事件五种派发策略、waterfall 续体的恰好一次语义、logger 的归属与
  名称派生、资源所有权、隔离下事件的可达性。

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


def run(coro):
    """把协程/可 await 对象驱动至完成；普通值原样返回。"""
    if not inspect.isawaitable(coro):
        return coro
    return asyncio.run(coro)


class SpecCase(unittest.TestCase):
    """把所有 RuntimeWarning（尤其是未 await 的协程）升级为错误。"""

    def setUp(self):
        super().setUp()
        self._cm = warnings.catch_warnings()
        self._cm.__enter__()
        warnings.simplefilter("error", RuntimeWarning)

    def tearDown(self):
        self._cm.__exit__(None, None, None)
        super().tearDown()


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


class TestLoading(SpecCase):
    """加载：插件从 PENDING 到 ACTIVE 的全过程。"""

    def test_plugin_activates_with_config(self):
        """加载后插件处于 ACTIVE，且拿到经解析的 config。"""
        root = Context()
        seen = {}
        fiber = root.plugin(lambda ctx, config: seen.update(config=config), {"a": 1})
        run(fiber.await_())
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertEqual(seen["config"], {"a": 1})

    def test_load_failure_is_observable(self):
        """加载抛错：状态 FAILED，`await fiber` 必须抛出同一个错误。

        静默失败是最危险的一种"与预期不符"——插件没起来却没人知道。
        """
        root = Context()

        def broken(ctx, config):
            raise ValueError("boom")

        fiber = root.plugin(broken)
        with self.assertRaises(ValueError):
            run(fiber.await_())
        self.assertEqual(fiber.state, FiberState.FAILED)

    def test_depends_on_inject_before_activation(self):
        """未声明依赖的服务不可读；声明后仅在提供方就绪时激活。"""
        root = Context()
        seen = []

        def consumer(ctx, config):
            seen.append(config)

        consumer.inject = ["svc"]
        fiber = root.plugin(consumer)
        run(fiber.await_())
        self.assertEqual(fiber.state, FiberState.PENDING)
        self.assertEqual(seen, [])

        async def provide_then_settle():
            await root.plugin(lambda ctx, config: ctx.provide("svc", 1)).await_()
            await fiber.await_()

        run(provide_then_settle())
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertEqual(len(seen), 1)

    def test_duplicate_provide_is_rejected(self):
        """同一作用域内重复提供同名服务必须报错，而不是静默覆盖。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", 1)).await_())
        with self.assertRaises(Exception):
            run(root.plugin(lambda ctx, config: ctx.provide("svc", 2)).await_())

    def test_service_class_registers_on_construction(self):
        """类插件：构造即注册服务。"""
        root = Context()

        class Greeter(Service):
            provide = "greeter"

            def hello(self, name):
                return "hi " + name

        run(root.plugin(Greeter).await_())
        self.assertEqual(root.greeter.hello("bob"), "hi bob")

    def test_service_method_sees_calling_context(self):
        """服务方法体内的 `self.ctx` 必须指向**调用方**。

        否则方法里注册的服务/effect/子插件会错挂到提供方作用域：插件卸载后
        残留、隔离语义被破坏。
        """
        root = Context()
        observed = {}

        class Locator(Service):
            provide = "loc"

            def who(self):
                return self.ctx.fiber.uid

        iso = root.isolate("loc")
        run(iso.plugin(Locator).await_())  # 提供方 fiber uid=1

        def caller(ctx, config):
            observed["uid"] = ctx.loc.who()  # 调用方 fiber uid=2

        caller.inject = ["loc"]
        run(iso.plugin(caller).await_())
        self.assertEqual(observed["uid"], 2)

    def test_invalid_effect_shape_is_rejected(self):
        """effect 主体返回非法形态必须报错（而非静默忽略）。"""
        root = Context()
        with self.assertRaises(Exception):
            run(root.plugin(lambda ctx, config: ctx.effect(lambda: "not-a-disposer")).await_())

    def test_async_effect_body_runs_at_registration(self):
        """异步 effect 主体在注册时即执行（不是等到卸载）。"""
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


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class TestResolution(SpecCase):
    """解析：属性读取如何落到具体的服务实现上。"""

    def test_service_resolves_to_impl_value(self):
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", {"v": 1})).await_())
        self.assertEqual(root.svc["v"], 1)

    def test_undeclared_dependency_is_refused(self):
        """未声明 inject 就读取服务必须报错——依赖关系必须显式。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", 1)).await_())

        def consumer(ctx, config):
            return ctx.svc  # noqa: B018 - 只需触发解析

        with self.assertRaises(AttributeError):
            run(root.plugin(consumer).await_())

    def test_isolation_scopes_are_independent(self):
        """隔离作用域下的同名服务互不干扰。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", "ROOT")).await_())
        iso = root.isolate("svc")
        run(iso.plugin(lambda ctx, config: ctx.provide("svc", "ISO")).await_())
        self.assertEqual(root.svc, "ROOT")
        self.assertEqual(iso.svc, "ISO")

    def test_isolation_does_not_cross_into_parent(self):
        """隔离作用域注册的服务，父作用域读不到。

        注意隔离是**按服务名**生效的：没有为某个名字建立隔离作用域时，
        它在全局可见（这正是本例要区分的另一半——见下一个用例）。
        """
        root = Context()
        iso = root.isolate("svc")
        run(iso.plugin(lambda ctx, config: ctx.provide("svc", "ISO")).await_())
        self.assertIsNone(root.get("svc"))
        self.assertEqual(iso.get("svc"), "ISO")

    def test_unisolated_name_is_global_even_from_isolated_scope(self):
        """未隔离的服务名天然全局共享，与它在哪个作用域里被提供无关。"""
        root = Context()
        iso = root.isolate("other")
        run(iso.plugin(lambda ctx, config: ctx.provide("shared_name", 1)).await_())
        self.assertEqual(root.get("shared_name"), 1)

    def test_same_label_isolations_share_scope(self):
        """相同 label 的两次 isolate 合并为同一作用域。"""
        root = Context()
        a = root.isolate("svc", "shared")
        b = root.isolate("svc", "shared")
        run(a.plugin(lambda ctx, config: ctx.provide("svc", "ONE")).await_())
        self.assertEqual(b.svc, "ONE")

    def test_accessor_recomputes_on_every_read(self):
        """计算型属性每次读取都重新求值。"""
        root = Context()
        state = {"n": 0}
        run(
            root.plugin(
                lambda ctx, config: ctx.accessor("now", Accessor(get=lambda receiver, error: state["n"]))
            ).await_()
        )
        self.assertEqual(root.now, 0)
        state["n"] = 5
        self.assertEqual(root.now, 5)

    def test_accessor_lives_with_its_plugin(self):
        """计算型属性属于注册它的插件：插件卸载即失效。"""
        root = Context()
        fiber = root.plugin(
            lambda ctx, config: ctx.accessor("computed", Accessor(get=lambda receiver, error: 42))
        )
        run(fiber.await_())
        self.assertEqual(root.computed, 42)
        self.assertIn("computed", root.reflect.props)
        run(fiber.dispose())
        self.assertNotIn("computed", root.reflect.props)
        # 插件卸载后该属性不再被解析：根上下文读取得到 None
        self.assertIsNone(root.computed)

    def test_mixin_exposes_service_members(self):
        """mixin 把服务的成员暴露到 ctx 上，并随插件卸载移除。"""
        root = Context()

        def plugin(ctx, config):
            ctx.provide("tools", {"ping": lambda: "pong"})
            ctx.mixin("tools", ["ping"])

        fiber = root.plugin(plugin)
        run(fiber.await_())
        self.assertEqual(root.ping(), "pong")
        run(fiber.dispose())
        self.assertNotIn("ping", root.reflect.props)

    def test_get_reads_without_inject_but_respects_scope(self):
        """ctx.get 不要求 inject，但按**调用方**作用域解析。"""
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", "ROOT")).await_())
        iso = root.isolate("svc")
        run(iso.plugin(lambda ctx, config: ctx.provide("svc", "ISO")).await_())
        self.assertEqual(root.get("svc"), "ROOT")
        self.assertEqual(iso.get("svc"), "ISO")
        self.assertIsNone(root.get("nope"))

    def test_effect_from_isolated_scope_works(self):
        """隔离作用域上下文上也能注册 effect（不只插件上下文）。"""
        root = Context()
        iso = root.isolate("svc")
        log = []
        dispose = iso.effect(lambda: log.append("up"), "manual")
        run(dispose)
        self.assertEqual(log, ["up"])


# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------


class TestUnloading(SpecCase):
    """卸载：插件释放它持有的全部资源。"""

    def test_effects_release_in_reverse_order(self):
        root = Context()
        order = []

        def plugin(ctx, config):
            ctx.effect(lambda: (order.append("a"), lambda: order.append("a'"))[1], "a")
            ctx.effect(lambda: (order.append("b"), lambda: order.append("b'"))[1], "b")

        fiber = root.plugin(plugin)
        run(fiber.await_())
        run(fiber.dispose())
        self.assertEqual(order, ["a", "b", "b'", "a'"])

    def test_each_disposer_runs_exactly_once(self):
        root = Context()
        count = []

        def plugin(ctx, config):
            ctx.effect(lambda: lambda: count.append(1), "once")

        fiber = root.plugin(plugin)
        run(fiber.await_())
        run(fiber.dispose())
        run(fiber.dispose())
        self.assertEqual(len(count), 1)

    def test_nested_effects_release_innermost_first(self):
        root = Context()
        order = []

        def plugin(ctx, config):
            def outer():
                ctx.effect(lambda: (order.append("inner"), lambda: order.append("inner'"))[1], "inner")
                return lambda: order.append("outer'")

            ctx.effect(outer, "outer")

        fiber = root.plugin(plugin)
        run(fiber.await_())
        run(fiber.dispose())
        self.assertEqual(order, ["inner", "inner'", "outer'"])

    def test_listener_dies_with_plugin(self):
        root = Context()
        seen = []
        fiber = root.plugin(lambda ctx, config: ctx.on("e", lambda: seen.append(1)))
        run(fiber.await_())
        root.emit("e")
        run(fiber.dispose())
        root.emit("e")
        self.assertEqual(seen, [1])

    def test_service_unregistered_on_unload(self):
        root = Context()
        fiber = root.plugin(lambda ctx, config: ctx.provide("svc", 1))
        run(fiber.await_())
        run(fiber.dispose())
        self.assertIsNone(root.get("svc"))
        self.assertEqual(root.registry.size, 0)

    def test_dependents_return_to_pending(self):
        """提供方消失后，依赖方必须退回等待而不是保持"看起来可用"。"""
        root = Context()
        provider = root.plugin(lambda ctx, config: ctx.provide("svc", 1))
        run(provider.await_())

        def consumer(ctx, config):
            pass

        consumer.inject = ["svc"]
        dependent = root.plugin(consumer)
        run(dependent.await_())
        self.assertEqual(dependent.state, FiberState.ACTIVE)
        run(provider.dispose())
        self.assertEqual(dependent.state, FiberState.PENDING)

    def test_unloading_plugin_cascades_to_children(self):
        root = Context()
        events = []
        parent = root.plugin(lambda ctx, config: None)
        run(parent.await_())
        child = parent.ctx.plugin(lambda ctx, config: ctx.effect(lambda: lambda: events.append("child")))
        run(child.await_())
        run(parent.dispose())
        self.assertIn("child", events)
        self.assertIsNone(child.uid)

    def test_disposed_plugin_refuses_new_effects(self):
        root = Context()
        fiber = root.plugin(lambda ctx, config: None)
        run(fiber.await_())
        run(fiber.dispose())
        self.assertEqual(fiber.state, FiberState.DISPOSED)
        with self.assertRaises(Exception):
            fiber._fiber.effect(lambda: None, "after")

    def test_restart_reloads_plugin(self):
        """restart：先卸载再原地重新加载。"""
        root = Context()
        events = []

        def plugin(ctx, config):
            ctx.effect(lambda: (events.append("up"), lambda: events.append("down"))[1])

        fiber = root.plugin(plugin)
        run(fiber.await_())
        run(fiber.restart())
        self.assertEqual(events, ["up", "down", "up"])

    def test_registry_delete_disposes_every_fiber(self):
        root = Context()
        seen = []

        def plugin(ctx, config):
            ctx.effect(lambda: lambda: seen.append(1))

        for _ in range(3):
            run(root.plugin(plugin).await_())
        self.assertEqual(root.registry.size, 1)
        root.registry.delete(plugin)
        self.assertEqual(root.registry.size, 0)
        self.assertEqual(len(seen), 3)

    def test_one_runtime_per_callback(self):
        """同一函数多次加载共享一条 runtime 记录。"""
        root = Context()
        count = []

        def plugin(ctx, config):
            count.append(1)

        run(root.plugin(plugin).await_())
        run(root.plugin(plugin).await_())
        self.assertEqual(root.registry.size, 1)
        self.assertEqual(len(count), 2)

    def test_root_fiber_reports_its_own_effects(self):
        """根 fiber 的 effect 元数据必须真实（不得被构造期清空）。"""
        root = Context()
        labels = {meta.label for meta in root.fiber.get_effects()}
        self.assertTrue(any("logger.exporter" in label for label in labels))


# ---------------------------------------------------------------------------
# 其余能力
# ---------------------------------------------------------------------------


class TestEventContract(SpecCase):
    """五种派发策略的契约。"""

    def test_emit_is_synchronous(self):
        root = Context()
        order = []
        root.on("e", lambda: order.append("run"))
        root.emit("e")
        self.assertEqual(order, ["run"])

    def test_bail_stops_at_first_value(self):
        root = Context()
        seen = []
        root.on("e", lambda: seen.append(1))
        root.on("e", lambda: "stop")
        root.on("e", lambda: seen.append(3))
        self.assertEqual(root.bail("e"), "stop")
        self.assertEqual(seen, [1])

    def test_bail_stops_on_any_value_except_none_and_false(self):
        """bail 的判据是「有没有返回值」，而不是「真不真」。

        `0` / `''` / `NaN` 都是监听器的**真实返回值**，必须中止派发；
        只有 `None` / `False` 表示"没有返回值"，继续下一个监听器。
        """
        root = Context()
        for value, expected in ((0, [1]), ("", [1]), (float("nan"), [1]), (None, [1, 3]), (False, [1, 3])):
            seen = []
            root.events._hooks.clear()
            root.on("e", lambda: seen.append(1))
            root.on("e", lambda v=value: v)
            root.on("e", lambda: seen.append(3))
            root.bail("e")
            self.assertEqual(seen, expected, f"返回值 {value!r} 的中止行为不符预期")

    def test_serial_stops_at_first_bail(self):
        root = Context()

        async def scenario():
            order = []

            async def a():
                order.append("a")

            async def b():
                order.append("b")
                return "stop"

            async def c():
                order.append("c")

            root.on("e", a)
            root.on("e", b)
            root.on("e", c)
            result = await root.serial("e")
            return order, result

        order, result = run(scenario())
        self.assertEqual(order, ["a", "b"])
        self.assertEqual(result, "stop")

    def test_parallel_reports_its_own_mode(self):
        """派发诊断必须如实反映策略：parallel 上报 'parallel'。"""
        root = Context()
        seen = []
        root.on("internal/dispatch", lambda mode, name, args, this_arg: seen.append((mode, name)))
        run(root.parallel("custom"))
        self.assertIn(("parallel", "custom"), seen)

    def test_waterfall_wraps_listeners(self):
        root = Context()
        root.on("e", lambda a, nxt: f"A({nxt()})")
        root.on("e", lambda a, nxt: f"B({nxt()})")
        self.assertEqual(root.waterfall("e", 1, lambda: "END"), "A(B(END))")

    def test_waterfall_continuation_advances_exactly_once(self):
        """重复调用续体不得跳过监听器、也不得让内置行为执行两次。"""
        root = Context()
        seen = []
        root.on("e", lambda a, nxt: (seen.append("outer"), nxt())[-1])
        root.on("e", lambda a, nxt: (seen.append("inner"), nxt(), nxt())[-1])
        root.waterfall("e", 1, lambda: seen.append("terminal"))
        self.assertEqual(seen, ["outer", "inner", "terminal"])

    def test_waterfall_veto_skips_rest_of_chain(self):
        root = Context()
        root.on("e", lambda a, nxt: "VETO")
        self.assertEqual(root.waterfall("e", 1, lambda: "NEVER"), "VETO")

    def test_once_disposes_before_running(self):
        root = Context()
        calls = []
        root.once("e", lambda: calls.append(1))
        root.emit("e")
        root.emit("e")
        self.assertEqual(calls, [1])

    def test_event_names_never_collide_with_map_members(self):
        """事件名是用户数据：`constructor` 这类名字不得命中映射自身的成员。"""
        root = Context()
        seen = []
        root.on("constructor", lambda: seen.append("hit"))
        root.emit("constructor")
        root.emit("items")
        self.assertEqual(seen, ["hit"])

    def test_internal_service_respects_isolation(self):
        """服务变更事件只送达同一隔离作用域的监听器。"""
        root = Context()
        seen_root, seen_iso = [], []
        root.on("internal/service", lambda name, value: seen_root.append(name))
        iso = root.isolate("svc")
        iso.on("internal/service", lambda name, value: seen_iso.append(name))
        provider = root.plugin(lambda ctx, config: ctx.provide("svc", 1))
        run(provider.await_())
        self.assertTrue(seen_root)
        self.assertEqual(seen_iso, [])


class TestLoggerContract(SpecCase):
    """logger：命名、归属与资源所有权。"""

    def test_named_logger(self):
        root = Context()
        self.assertEqual(root.logger("mymod").name, "mymod")

    def test_default_name_derives_from_calling_plugin(self):
        root = Context()
        seen = {}

        def my_plugin(ctx, config):
            seen["name"] = ctx.logger().name

        run(root.plugin(my_plugin).await_())
        self.assertEqual(seen["name"], "my-plugin")

    def test_exporter_disposed_with_owning_plugin(self):
        root = Context()
        service = root.logger
        fiber = root.plugin(lambda ctx, config: ctx.logger.exporter(Exporter(export=lambda m: None)))
        run(fiber.await_())
        self.assertEqual(len(service.exporters), 2)  # buffer + 插件注册的
        run(fiber.dispose())
        self.assertEqual(len(service.exporters), 1)

    def test_each_exporter_disposer_releases_its_own_entry(self):
        """释放 A 绝不能动 B——否则会留下永久泄漏的 exporter。"""
        root = Context()
        service = root.logger
        first = service.exporter(Exporter(export=lambda m: None))
        second = service.exporter(Exporter(export=lambda m: None))
        before = len(service.exporters)
        run(first())
        self.assertEqual(len(service.exporters), before - 1)
        run(second())
        self.assertEqual(len(service.exporters), before - 2)

    def test_intercept_config_merges_across_levels(self):
        """多级 intercept 按 key 合并：祖先的其它 key 不能丢。"""
        root = Context()
        leaf = root.intercept("logger", {"level": 3, "name": "outer"}).intercept("logger", {"name": "inner"})
        seen = {}

        def plugin(ctx, config):
            logger = ctx.logger()
            seen.update(name=logger.name, level=logger.level)

        run(leaf.plugin(plugin).await_())
        self.assertEqual(seen, {"name": "inner", "level": 3})

    def test_error_chain_is_recorded_in_full(self):
        """带 cause 的错误必须完整记录，不能只记最内层。"""
        root = Context()
        try:
            try:
                raise ValueError("inner")
            except ValueError as error:
                raise RuntimeError("outer") from error
        except RuntimeError as error:
            root.logger.error(error)
        messages = [str(message.args[0]) for message in root.logger.buffer]
        self.assertEqual(messages, ["inner", "outer"])

    def test_buffer_records_messages(self):
        root = Context()
        root.logger("mod").info("hello")
        self.assertEqual(root.logger.buffer[-1].args, ["hello"])
        self.assertEqual(root.logger.buffer[-1].name, "mod")


class TestConfigContract(SpecCase):
    """配置校验与更新。"""

    def test_update_applies_new_config(self):
        root = Context()
        seen = []

        def plugin(ctx, config):
            seen.append(config["v"])

        fiber = root.plugin(plugin, {"v": 1})
        run(fiber.await_())
        run(fiber.update({"v": 2}))
        self.assertEqual(seen, [1, 2])

    def test_update_is_awaitable_in_every_state(self):
        """`await fiber.update(cfg)` 在任何状态下都不得崩。"""

        async def scenario():
            root = Context()

            def consumer(ctx, config):
                pass

            consumer.inject = ["svc"]
            pending = root.plugin(consumer)
            await pending.await_()  # PENDING
            await pending.update({"v": 1})

            active = root.plugin(lambda ctx, config: ctx.provide("svc", 1))
            await active.await_()  # ACTIVE
            await active.update({"v": 1})

            await active.dispose()
            with self.assertRaises(Exception):
                await active.update({"v": 2})  # DISPOSED：明确拒绝

        run(scenario())

    def test_config_validation_failure_is_reported(self):
        """配置校验失败必须以 ValidationError 报出。"""
        from cordis_py import ValidationError

        root = Context()

        def plugin(ctx, config):
            pass

        plugin.Config = lambda config: {"issues": [{"message": "bad config", "path": ["v"]}]}
        with self.assertRaises(ValidationError):
            run(root.plugin(plugin, {"v": 1}).await_())


class TestIntrospection(SpecCase):
    """上下文自省。"""

    def test_contains_covers_builtins_and_services(self):
        root = Context()
        run(root.plugin(lambda ctx, config: ctx.provide("svc", 1)).await_())
        for name in ("extend", "isolate", "intercept", "get", "provide", "logger", "svc"):
            self.assertIn(name, root, name)
        self.assertNotIn("nope", root)

    def test_unknown_property_reading_is_scope_aware(self):
        """根上下文读未知服务得到 None；插件上下文则必须明确报错。

        插件里报错的理由是：读不到"依赖"通常意味着漏了 ``inject``，
        静默给出 None 会把这类配置错误藏起来。
        """
        root = Context()
        self.assertIsNone(root.definitely_not_there)  # noqa: B018

        def consumer(ctx, config):
            return ctx.definitely_not_there  # noqa: B018

        with self.assertRaises(AttributeError):
            run(root.plugin(consumer).await_())


if __name__ == "__main__":
    unittest.main(verbosity=2)