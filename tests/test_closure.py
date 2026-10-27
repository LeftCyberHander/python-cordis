"""闭包结论的回归锁定。

第五轮引入基数约束后，验证器在 N=2、N=3 下都能给出 **PROVED**（可达集闭包）。
第六轮（``tools/verify_unbounded.py``）把结论推到**无界**：不设插件数上限，
在"视图存在集"层面闭包 ⇒ 任意长度、**任意插件数**成立。

这两个结论是本项目形式化保证的核心，故用本文件把它们锁住：

- 若将来某个改动破坏闭包（哪怕框架行为完全正常），这里会失败——因为
  "闭包不再达成"意味着归纳步骤无法被机器检查，证明降级为有界结论；
- 若验证器的裁决逻辑本身被改坏（把未闭包误报成 PROVED），这里同样会失败；
- 无界证明特有的两个机制（幸存二分、存在集投影的有界性）也必须保持非空转、
  有界——否则"无界"三字名不副实。

注意本文件**只**锁定"闭包"这件事本身，不锁定状态数与耗时（那会因无关改动而
波动）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/tools")

import verify as V  # noqa: E402
import verify_unbounded as U  # noqa: E402


class TestClosureProof(unittest.TestCase):
    """N=2 下可达集必须闭包（裁决 PROVED）。"""

    def test_plugin_cap_2_reaches_closure(self):
        """基数约束 N=2 时可达集闭包 ⇒ 任意长度成立。"""
        original = V.PLUGIN_CAP
        V.PLUGIN_CAP = 2
        try:
            with warnings.catch_warnings():
                # INV-7：无悬挂协程。本身不应触发任何 RuntimeWarning。
                warnings.simplefilter("error", RuntimeWarning)
                result = asyncio.run(
                    V.verify(max_depth=16, max_states=2_000_000, verbose=False)
                )
        finally:
            V.PLUGIN_CAP = original

        self.assertIsNone(
            result.counterexample,
            msg=(
                "基数约束 N=2 下发现反例，说明框架行为已改变：\n"
                + (result.counterexample.render() if result.counterexample else "")
            ),
        )
        self.assertTrue(
            result.closure_ok,
            msg=(
                f"基数约束 N=2 下可达集未闭包（depth={result.depth_reached}, "
                f"|S|={result.states}, front={len(result.open_transitions)}），"
                "证明已降级为有界结论。判定式见 docs/FORMAL_VERIFICATION.md §6.1"
            ),
        )
        self.assertFalse(result.inconclusive)

    def test_closure_is_finite_and_bounded(self):
        """闭包时各字段取值基数必须有限（闭包的可解释依据）。"""
        original = V.PLUGIN_CAP
        V.PLUGIN_CAP = 2
        try:
            init = V.project(asyncio.run(V.replay(())))
            seen = {init}
            frontier = [[]]
            for _ in range(9):  # 深度 9 已足以让各字段饱和
                nxt = []
                for path in frontier:
                    for op in V.ALPHABET:
                        state = V.project(asyncio.run(V.replay(path + [op])))
                        if state in seen:
                            continue
                        seen.add(state)
                        nxt.append(path + [op])
                frontier = nxt
        finally:
            V.PLUGIN_CAP = original

        # 每个字段的取值数都应是**小**的（无界时会随深度单调增长）
        bounds = {
            "pending": 12,
            "store": 12,
            "listeners": 24,
            "fibers": 32,
            "edges": 64,
        }
        actual = {
            "pending": len({s.pending for s in seen}),
            "store": len({s.store for s in seen}),
            "listeners": len({s.listeners for s in seen}),
            "fibers": len({s.fibers for s in seen}),
            "edges": len({s.edges for s in seen}),
        }
        for name, limit in bounds.items():
            self.assertLessEqual(
                actual[name],
                limit,
                msg=(
                    f"字段 {name!r} 的取值基数 {actual[name]} 超过上界 {limit}，"
                    "可能重新引入了无界身份量（uid / 代次 / 自增序号）。"
                    "参见 docs/FORMAL_VERIFICATION.md §6.1 的六个无界源"
                ),
            )


#: 完整闭包用例的闸门（与 tests/test_properties.py 的 CORDIS_BMC_DEPTH 同一模式）：
#: 设 ``CORDIS_FORMAL=full`` 才跑那条 ~265 s 的闭包断言。缺省跑快档断言，
#: 代价约 1/5，覆盖面小一些但同样能撞出退化。
_FULL = os.environ.get("CORDIS_FORMAL", "").lower() == "full"

#: BFS 结果缓存：(depth, max_states, bifurcate) → (裁决, 可达集)。
#: 存在集 BFS 是本套件最贵的一步（depth 9 约 150 s），而闭包 / 投影有界性 /
#: 探针层三个用例跑的是**同一个** BFS——缓存后只算一次。键里含字母表，避免
#: 不同字母表的缓存串味。
_BFS_CACHE: dict = {}


def _bfs_full(depth: int, max_states: int, bifurcate: bool, alphabet: str = "l5"):
    """同 ``_bfs``，但一并返回幸存二分派生的合成状态集合。"""
    key = ("full", depth, max_states, bifurcate, alphabet)
    if key not in _BFS_CACHE:
        ops = {"l5": U.UOPS_L5, "tier1": U.UOPS_TIER1, "core": U.UOPS_CORE}[alphabet]
        derived: set = set()
        outcome, visited = asyncio.run(
            U._uverify_impl(depth, max_states, False, bifurcate, ops, derived)
        )
        _BFS_CACHE[key] = (outcome, visited, derived)
    return _BFS_CACHE[key]


def _bfs(depth: int, max_states: int, bifurcate: bool, alphabet: str = "l5"):
    key = (depth, max_states, bifurcate, alphabet)
    if key not in _BFS_CACHE:
        ops = {
            "l5": U.UOPS_L5,
            "tier1": U.UOPS_TIER1,
            "core": U.UOPS_CORE,
        }[alphabet]
        _BFS_CACHE[key] = asyncio.run(
            U._uverify_impl(depth, max_states, False, bifurcate, ops)
        )
    return _BFS_CACHE[key]


class TestUnboundedProof(unittest.TestCase):
    """无界证明的回归锁定：结论不得依赖任何插件数上界。

    第七轮起字母表分三层（结构层 ``UOPS_CORE`` 23 元决定可达集；探针层
    ``UOPS_PROBE`` 10 元是投影自环；``UOPS_TIER1`` 18 元是回归套件的快档）。
    闭包断言跑在快档上——完整结构层的闭包需要小时级算力，属发布前验证而非
    每次改动的回归项；核心字母表另有一条"预算内无违规"的用例兜住退化。
    """

    #: 回归快档：一级结构层字母表（实测 depth 12 闭包 / 1416 状态 / ~265 s）
    FAST = U.UOPS_TIER1

    def test_unbounded_reaches_closure(self):
        """存在集层面闭包 ⇒ 任意长度、任意插件数成立（PROVED-UNBOUNDED）。

        跑回归快档字母表 ``UOPS_L5``（13 元，实测 depth 7 闭包 / 43 状态 /
        ~2 s）：它决定生命周期主轴的可达集，闭包义务对它必须成立。
        """
        with warnings.catch_warnings():
            # INV-7：无悬挂协程。验证器自身不应触发任何 RuntimeWarning。
            warnings.simplefilter("error", RuntimeWarning)
            result = asyncio.run(
                U.uverify(
                    max_depth=24,
                    max_states=20_000,
                    verbose=False,
                    alphabet=U.UOPS_L5,
                )
            )


        self.assertIsNone(
            result.counterexample,
            msg=(
                "无界验证发现反例，说明框架行为已改变：\n"
                + (result.counterexample.render() if result.counterexample else "")
            ),
        )
        self.assertTrue(
            result.closure_ok,
            msg=(
                f"无界可达集未闭包（depth={result.depth_reached}, "
                f"|S|={result.states}），归纳步骤无法被机器检查。"
                "判定式见 docs/FORMAL_VERIFICATION.md §6.2"
            ),
        )
        self.assertFalse(result.truncated)
        self.assertIn("PROVED-UNBOUNDED", result.verdict())

    @unittest.skipUnless(
        _FULL, "18 元字母表的闭包约 265 s（1416 状态），用 CORDIS_FORMAL=full 开启"
    )
    def test_tier1_alphabet_reaches_closure(self):
        """18 元字母表（一级结构层，含嵌套加载与规约错误路径）同样闭包。"""
        result = asyncio.run(
            U.uverify(
                max_depth=24,
                max_states=20_000,
                verbose=False,
                alphabet=U.UOPS_TIER1,
            )
        )
        self.assertIsNone(
            result.counterexample,
            msg="18 元字母表出现反例：\n"
            + (result.counterexample.render() if result.counterexample else ""),
        )
        self.assertTrue(
            result.closure_ok,
            msg=f"18 元字母表未闭包（depth={result.depth_reached}, "
            f"|S|={result.states}）——级联与幸存二分的覆盖退化",
        )

    def test_bifurcation_is_not_vacuous(self):
        """幸存二分必须真实扩大可达集（否则"覆盖计数≥2"是空话）。"""
        # 浅预算即可：断言"开二分的可达集严格更大"在任何深度都成立，而完整
        # 深度要两次各 ~265 s。
        off_result, off_visited = _bfs(24, 20_000, False)
        on_result, on_visited = _bfs(24, 20_000, True)
        # 两种模式下都必须闭包（否则对比本身无意义）
        self.assertTrue(off_result.closure_ok, msg="关闭二分后未闭包")
        self.assertTrue(on_result.closure_ok, msg="开启二分后未闭包")
        self.assertGreater(
            len(on_visited),
            len(off_visited),
            msg=(
                f"幸存二分未扩大可达集（开={len(on_visited)} ≤ 闭={len(off_visited)}），"
                "说明二分后继与代表后继恒等——计数≥2 的分支没有被覆盖，"
                "无界结论退化为'恰好一份'的结论。参见 docs/FORMAL_VERIFICATION.md §6.2"
            ),
        )

    def test_unbounded_projection_is_bounded(self):
        """存在集投影的字段取值必须有限（无界性的可解释依据）。"""
        _, visited = _bfs(24, 20_000, True)
        self.assertTrue(visited, "可达集为空")
        # 互异视图数、互异全局态数都应是**小**的（投影若重新引入无界身份量
        # ——uid / 代次 / 自增序号——它们会随深度单调增长）
        n_views = len({v for s in visited for v in s.views})
        n_globals = len({s.g for s in visited})
        max_views = max(len(s.views) for s in visited)
        self.assertLessEqual(
            n_views,
            16,
            msg=(
                f"互异视图数 {n_views} 超过上界 16，视图定义可能重新包含了"
                "无界身份量。参见 docs/FORMAL_VERIFICATION.md §6.2"
            ),
        )
        self.assertLessEqual(
            n_globals,
            32,
            msg=(
                f"互异全局态数 {n_globals} 超过上界 32，全局态可能重新引入了"
                "无界维度。参见 docs/FORMAL_VERIFICATION.md §6.2"
            ),
        )
        self.assertLessEqual(
            max_views,
            8,
            msg=f"单状态互异视图数 {max_views} 超过上界 8",
        )

    def test_unbounded_mutations_all_caught(self):
        """域内变异全部被无界验证器捕获（灵敏度 100%）。

        变异检验走 ``run_mutation``：短 BFS（**完整结构层**字母表）+ 探针层。
        两层都要跑——探针层的性质（值语义那批）只在探针执行时判定，只跑 BFS
        会把它们记成漏检。
        """
        for name, factory, expected in U.UMUTATIONS:
            if expected is None:
                continue  # 域外变异不计分（量化性质集边界）
            caught, found = asyncio.run(U.run_mutation(factory))
            self.assertTrue(
                found,
                msg=(
                    f"变异 {name!r} 未被无界验证器捕获——对该缺陷是盲的，"
                    f"其 PROVED-UNBOUNDED 结论不可信（期望性质 {expected}）"
                ),
            )
            self.assertIn(
                expected,
                caught,
                msg=f"变异 {name!r} 触发的性质是 {sorted(caught)}，期望包含 {expected}",
            )


    def test_core_alphabet_has_no_violation_within_budget(self):
        """完整结构层字母表在预算内不得出现任何违规（闭包由发布前验证保证）。

        完整结构层（23 元）的闭包是小时级算力，不适合放进每次改动的回归；
        本用例守住"退化会立刻显形"这一半：任何让某条新性质开始报错的行为
        改动，都会在前几千个状态里被撞到。
        """
        result, _visited = _bfs(5, 800, True, alphabet="core")
        self.assertIsNone(
            result.counterexample,
            msg=(
                "完整结构层字母表在预算内即出现反例（规约形式化被破坏）：\n"
                + (result.counterexample.render() if result.counterexample else "")
            ),
        )

    def test_probe_layer_is_self_loop_and_invariant(self):
        """探针层：每个可达状态上性质成立，且投影上是自环。

        自环断言是分层的**安全阀**：若某个探针实际改变了抽象状态，它就属于
        结构层，必须挪进 ``UOPS_CORE`` 重跑闭包——否则"分层"就漏了它的转移。
        """
        _, visited, derived = _bfs_full(24, 20_000, True)
        failures = asyncio.run(
            U.run_probes(visited, verbose=False, bifurcated=derived)
        )
        self.assertEqual(
            failures[:5],
            [],
            msg="探针层出现违规（值语义规约被破坏，或某探针并非投影自环）：\n"
            + "\n".join(failures[:5]),
        )
        self.assertTrue(visited, "探针层没有可核对的可达状态")


class TestVerifierSensitivity(unittest.TestCase):
    """验证器必须能抓到已知缺陷——否则它的"通过"没有意义。"""

    def test_in_scope_mutations_all_caught(self):
        """域内变异全部被捕获（灵敏度 100%）。"""
        original = V.PLUGIN_CAP
        V.PLUGIN_CAP = 2
        try:
            for name, factory, expected in V.MUTATIONS:
                if expected is None:
                    continue  # 域外变异不计分
                with factory():
                    result = asyncio.run(
                        V.verify(
                            max_depth=8, max_states=1_000_000, verbose=False
                        )
                    )
                self.assertIsNotNone(
                    result.counterexample,
                    msg=(
                        f"变异 {name!r} 未被捕获——验证器对该缺陷是盲的，"
                        f"其PROVED 结论不可信（期望性质 {expected}）"
                    ),
                )
                caught = {n for n, _ in result.counterexample.violations}
                self.assertIn(
                    expected,
                    caught,
                    msg=f"变异 {name!r} 触发的性质是 {caught}，期望包含 {expected}",
                )
        finally:
            V.PLUGIN_CAP = original

    def test_clean_tree_has_no_false_positive(self):
        """未注入缺陷时不得误报（否则灵敏度指标无意义）。"""
        original = V.PLUGIN_CAP
        V.PLUGIN_CAP = 2
        try:
            result = asyncio.run(
                V.verify(max_depth=8, max_states=1_000_000, verbose=False)
            )
        finally:
            V.PLUGIN_CAP = original
        self.assertIsNone(result.counterexample)


if __name__ == "__main__":
    unittest.main(verbosity=2)
