# cordis-py

`cordis` 的 Python 移植版，逐文件对照 `../src/*.ts`（`@deepseek-ai/cordis` 4.0.2）编写。
目标是**行为等价**：状态机、并发控制点、effect 所有权规则、以及源码中已记录的缺陷都原样保留。

## 快速开始

```python
import asyncio
from cordis_py import Context, Service

root = Context()

def hello(ctx, config):
    ctx.provide("greeter", "hi")

async def main():
    await root.plugin(hello)
    print(root.greeter)          # hi

asyncio.run(main())
```

运行测试：

```bash
cd python-cordis && python -m unittest discover -s tests -v
```

当前状态：**127 个用例全部通过**（1 个按条件跳过），其中 `test_closure.py` 是形式化
验证器的回归锁定，单独跑约 2.5 分钟。

想看怎么用这套框架写业务插件，读 [`docs/PLUGIN_GUIDE.md`](docs/PLUGIN_GUIDE.md)，
配套的可运行示例在 `examples/tutorial/`。

## 模块映射

| 参考 TS | 本包 | 说明 |
| --- | --- | --- |
| `context.ts` | `context.py` | 全上下文树；JS 的 `Proxy` 陷阱改用 `__getattr__` / `__setattr__` / `__contains__` 钩子 |
| `fiber.ts` | `fiber.py` | 生命周期状态机、epoch / inertia 并发控制、effect 收集 |
| `reflect.ts` | `reflect.py` | 服务注册表、`provide` / `accessor` / `mixin` / `notify` |
| `events.ts` | `events.py` | 五种派发模式 `emit` / `parallel` / `serial` / `bail` / `waterfall` |
| `registry.ts` | `registry.py` | 插件运行时记录、`inject` / `plugin`、依赖注入 |
| `service.ts` | `service.py` | `Service` 抽象基类 |
| `logger.ts` | `logger.py` | 可调用的 `ctx.logger()`、缓冲 exporter、格式化器 |
| `utils.ts` | `utils.py` | `symbols`、`DisposableList`、`createCallable`、`composeError` |
| `index.ts` | `__init__.py` | 公开导出面 |

## 与参考实现的差异（本轮修复）

最初的移植目标是「连缺陷一起复刻」。这一轮按 `docs/CORRECTNESS.md` 的裁决顺序做了
修订：**参考实现自身的缺陷不作为正确性依据**，凡是被它判定为缺陷的行为都已修正，
`tests/test_spec.py` 与 `tests/test_regressions.py` 锁定修正后的契约。具体包括：

- `parallel()` 向 `internal/dispatch` 上报 `'parallel'`（原为 `'emit'`）。
- `LoggerService.exporter()` 的 disposer 在**注册时**捕获序号：释放 A 只删 A，
  不会误删 B、也不会重复删除（`logger.ts:234-235`）。
- 事件名映射改为 `_HookMap`：`constructor` / `items` 这类名字只是普通键，
  不会命中映射自身的成员（`events.ts:132`）。
- `waterfall` 的续体**恰好推进一次**：重复调用返回首次结果，不跳监听器、
  不重复执行内置行为（`events.ts:237-240`）。
- `Fiber.update()` 在任何状态下都返回可 await 对象（`fiber.ts:739-746`）。
- `context.ts:82` 丢弃 buffer exporter disposer 的行为已移除，根 fiber 的
  effect 元数据如实反映常驻 exporter。
- **服务注册与解析都带上「调用方」**：`Service` 构造时把 `ctx` 作为 caller 传给
  `reflect.provide`，`ctx.get` / `accessor` / `mixin` / `on` 同理。这是隔离作用域
  真正生效的前提（此前所有 `Service` 都注册到根上，第二个实例直接撞名）。
- 内置服务经上下文读取时会做**调用方重绑**（`TraceableValue` + `ServiceView`），
  `ctx.logger()` 因此按调用方 fiber 命名，exporter 也归调用方所有。
- `ctx.inject()` 构造的 `{inject, apply}` 插件能被正确解析（`registry.ts:226`）。
- 事件前导 `thisArg` 的识别对齐 TS 的 `typeof === 'object'`：`None` / list / tuple /
  set 都会作为 thisArg 被消费。

## 与 JS 语义的三处必要适配

移植中只有三处因语言差异做了显式调整，其余全部一比一对应：

1. **thenable → 协程**。JS 中"可 await 的 disposer"用 `Promise` 表达；Python 里统一用真协程
   （`_settle` / `_settle_catch`），因为 `asyncio.run` 只接受协程对象。同步可完成的 teardown
   **同步执行**并返回裸值，忠实还原 `finalizeDisposal` 的 thenable 分支（`fiber.ts:492`）。
2. **`next` 的多余实参**。JS 调用 `next(config, noSave)` 时多余的实参被静默忽略；Python 会报
   `TypeError`，因此 `waterfall` 的续体接受并丢弃多余位置参数。
3. **`_effect_inertia` 用 id 作键**。Python 没有可键任意闭包的 `WeakMap`；表项用哨兵属性保护，
   并在 wrapper 摘除时清理，避免 id 回收导致误判。

## 目录结构

```
python-cordis/
├── cordis_py/              # 移植实现（9 个模块，约 2.7k 行）
├── tests/
│   ├── test_cordis.py      # 逐行对照 src/*.ts 的移植用例
│   ├── test_spec.py        # 功能契约（加载 / 解析 / 卸载 / 事件 / 日志）
│   ├── test_regressions.py # 本轮修补的回归锁定
│   ├── test_properties.py  # 属性测试
│   └── test_closure.py     # 形式化验证器的闭包结论锁定
├── tools/                  # 状态机验证器（verify.py / verify_unbounded.py）
├── docs/
│   ├── PLUGIN_GUIDE.md     # 怎么写业务插件（从这里开始）
│   ├── CORRECTNESS.md      # 功能规约
│   └── FORMAL_VERIFICATION.md
├── examples/
│   ├── tutorial/           # 最小可运行示例：store / todo / reminder
│   └── office/             # 五插件办公三件套 + CLI
└── README.md
```

每个测试的 docstring 都写明它锁定的是 `src/*.ts` 的哪一行不变量，便于逐条审计。
