# 写自己的业务插件

这篇文档只讲一件事：拿到 `cordis_py` 之后，怎么把「一个业务功能」拆成插件，装配起来，
并且不踩坑。所有代码都在 `examples/tutorial/` 里可以直接跑。

---

## 一、先建立心智模型

`cordis` 不是 MVC，也不是依赖注入容器那么简单，它首先是**一棵上下文树**。

根上下文 `root = Context()` 代表整个进程。你在这棵树上挂插件，每个插件拿到一个
自己的子上下文（以及一个叫 `fiber` 的生命周期对象）。插件里调 `ctx.provide("todo", 实例)`，
就把一个值挂到了这棵树的某个「槽位」上；别的插件用 `ctx.todo` 取值，取到的是
**离自己最近的那个槽位**。

槽位由两个东西决定：服务名，以及隔离标签（`isolate` 里那个 label）。默认情况下
同名服务只有一个槽位，全树共享；一旦用了 `ctx.isolate("todo")`，这棵子树就有
自己的槽位，父树看不见它，它也看不见父树的实现。

所以整个框架只有三个动作值得记：

**挂插件** —— `ctx.plugin(fn 或类, 配置)`，返回一个可 await 的 fiber。

**挂服务** —— `ctx.provide(名字, 值)`，插件卸载时自动摘掉。

**声明依赖** —— `inject = ["名字"]`，依赖不齐时插件停在那里不动，齐了自动跑。

其余概念（effect、事件、配置校验、热插拔）都是这三件事的配套设施。

---

## 二、引入方式

这个仓库没有 `pyproject.toml`，不是 pip 包。把 `python-cordis` 目录放进 `sys.path` 即可：

```python
import sys
sys.path.insert(0, r"D:\auto\deepseek-harness-master\vendor\cordis\python-cordis")

from cordis_py import Context, Service, inject_decorator
```

如果你把自己的业务代码放到 `python-cordis/examples/你的项目/` 下面，那就再补一行
指向仓库根目录，写法参考 `examples/tutorial/main.py` 开头。

---

## 三、第一个插件：注册一个服务

插件最朴素的形态就是一个函数，签名固定为 `(ctx, config)`：

```python
from cordis_py import Context

root = Context()

def greeter(ctx, config):
    ctx.provide("greeter", f"你好，{config['name']}")

async def main():
    await root.plugin(greeter, {"name": "世界"})
    print(root.greeter)          # 你好，世界

asyncio.run(main())
```

三个细节：

`ctx.plugin()` 是**立即返回**的，返回值是个 fiber 句柄，`await` 它只是等它加载完。
你也可以不 await，插件会在当前事件循环的下一个空隙里跑起来；但测试和 CLI 里建议
统一 await，错误会在这里抛出来，而不是被吞掉。

`root.greeter` 不是普通属性，它走的是服务解析。`root` 上没有这个属性，解析器去
`reflect.store` 里按服务名找。找不到时，**根上下文返回 `None`，插件上下文抛
`AttributeError`** —— 后者是故意的，插件里读不到依赖通常意味着你漏写了 `inject`，
静默给 `None` 会把配置错误藏起来。

`provide` 注册的服务是 effect，插件卸载时自动注销，不需要手动清理。

---

## 四、声明依赖：inject

插件需要别人的服务，就把名字写进 `inject`：

```python
def report(ctx, config):
    print(ctx.greeter)

report.inject = ["greeter"]         # 函数式插件用属性
```

或者用装饰器，等价且更好看：

```python
from cordis_py import inject_decorator

@inject_decorator("greeter")
def report(ctx, config):
    print(ctx.greeter)
```

**依赖不齐时，插件体根本不执行。** fiber 停在 `PENDING`，等提供方出现后自动跳到
`ACTIVE`；提供方卸载了，它退回 `PENDING`。这条链路是自动的，你不需要写重试、
不需要写就绪回调。

有一个容易忽略的后果：**没声明 `inject` 就读服务会报错**。这不是限制，是保护——
它保证每个插件的依赖都写在签名能看见的地方。

---

## 五、隔离：同一份代码服务多个人

`ctx.isolate("todo")` 派生一个子上下文，在这棵子树里，`todo` 这个名字指向新的槽位：

```python
a = root.isolate("todo")
b = root.isolate("todo")

await a.plugin(Todo)
await b.plugin(Todo)

a.todo is b.todo     # False，两份独立实例
```

典型用途是「一人一份」：每个人一个数据库连接、一个输出目录、一个账本。你不用把
用户 ID 传进每一个函数，作用域本身就是隔离边界。

`isolate(name, label)` 的第二个参数用来**合并**作用域：两次传同一个 `label`，它们
就共享同一个槽位。省略 label 时每次都是全新的，永远不会撞。

注意隔离是**按服务名**生效的。你 `isolate("todo")` 只隔离了 `todo`，同一个插件里
用到的 `store` 仍然是全树共享的，除非你也 `isolate("store")`。装配时通常要按顺序
把该隔离的名字逐个隔离一遍，`examples/tutorial/main.py` 里的 `ISOLATED` 元组就是
干这个的。

---

## 六、资源与生命周期：effect

凡是「打开要关、注册要撤、占用要还」的东西，都用 `ctx.effect` 表达：

```python
def store(ctx, config):
    def open():
        conn = sqlite3.connect(config["path"])
        return conn.close          # 返回的函数就是卸载时执行的清理

    ctx.effect(open, "store.conn")
```

`effect` 的主体**立即执行**，返回值必须是「零参可调用」或者 `None`，否则报
`Invalid effect`。清理函数在两种时机执行：插件卸载时，或者你手动调用 `dispose()`
时——以先发生者为准，并且**只执行一次**。

多个 effect 按注册的**逆序**清理，嵌套 effect 从最内层开始，这保证了「先开的最后关」。
异步 effect 主体也支持，加载时就会启动，`await fiber.await_()` 会等它稳定下来。

effect 的价值不只是「省一个 finally」。它把「这份资源属于哪个插件」变成了结构事实：
插件 A 注册的 effect 绝不会被插件 B 的卸载误伤，反过来 A 被卸载时也不会漏掉。

---

## 七、插件间协作：事件

服务解决「我要用你的能力」，事件解决「你做完事告诉我一声」。

```python
def todo(ctx, config):
    ...
    ctx.emit("todo/added", item)          # 发布

def audit(ctx, config):
    ctx.on("todo/added", lambda item: print("记录", item["id"]))   # 订阅
```

监听器也是 effect：插件卸载时自动摘除，不会残留、不会在别人卸载后还收到事件。

五种派发方式，日常只需要记住两种：

`emit` 是同步广播，不等待异步监听器；`parallel` 并发执行并等全部完成，任一失败会
汇总成 `ExceptionGroup`；`serial` 串行执行，遇到非 `None`/`False` 的返回值就停；
`bail` 是同步版的串行，用于「谁能处理就谁处理」；`waterfall` 把监听器串成责任链，
用于「允许别人改写我的中间结果」——办公示例里 `meeting/minutes/blocks` 就留了这么
一个扩展点。

**事件是进程级的**，不随 isolate 隔离。同一个进程里三个用户的监听器都能收到
`todo/added`。要限定自己那一份，就在监听器里按业务字段过滤（`examples/tutorial/plugins/reminder.py`
里按 `owner` 过滤）。这一点和「服务」正好相反，别记混。

---

## 八、函数插件还是 Service 子类

不需要对外提供能力的，写函数，例如提醒、审计、日志转发。

需要挂一个 `ctx.xxx` 让别处调用的，写 `Service` 子类：

```python
from cordis_py import Service

class Todo(Service):
    provide = "todo"          # 注册成 ctx.todo
    inject = ["store"]        # 依赖

    def __init__(self, ctx, config=None):
        super().__init__(ctx, "todo")
        self._store = self.ctx.store       # 构造期取一次，缓存住

    def add(self, text):
        ...
```

`super().__init__` 一执行，服务就注册好了，不需要额外的 `register()`。

**这里有一个必须知道的语义**：方法体里的 `self.ctx` 指向**调用方**上下文，不是提供方。
框架这样做是为了让方法里注册的 effect、监听器、子插件归调用方所有。副作用是：如果你在
方法里写 `self.ctx.store`，而调用方插件没有声明 `inject store`，就会报
`cannot get property "store" without inject`。

所以推荐的写法是**在 `__init__` 里把依赖取出来缓存成 `self._store`**。构造函数里的
`ctx` 是提供方上下文，依赖一定齐；方法里用缓存，就不再受调用方的 inject 影响。
只有当你故意想让副作用落到调用方时（比如 `self.ctx.emit` 发事件），才用 `self.ctx`。

---

## 九、配置与热更新

配置就是 `ctx.plugin(插件, 配置)` 的第二个参数，原样传给插件函数/构造函数。

需要校验时给插件挂一个 `Config`，它是一个「配置进、结果出」的可调用对象：

```python
def validate(config):
    if "path" not in config:
        return {"issues": [{"message": "缺少 path", "path": ["path"]}]}
    return {"value": {"path": str(config["path"])}}

Todo.Config = validate
```

返回 `{"issues": [...]}` 会抛 `ValidationError`，返回 `{"value": ...}` 则把归一化后的
配置交给插件。校验失败时插件不会启动，`await fiber` 会把错误抛给你。

运行期换配置用 `fiber.update(新配置)`，它会跑完本 fiber 的 `internal/update` 钩子，
然后卸载重载。已释放的 fiber 会明确拒绝。

---

## 十、目录结构与装配

`examples/tutorial/` 是一个可以直接抄的骨架：

```
tutorial/
├── main.py              装配 + CLI
├── plugins/
│   ├── __init__.py
│   ├── store.py         Service：JSON 存储
│   ├── todo.py          Service：业务逻辑
│   └── reminder.py      函数插件：监听事件
└── data/<用户>/todo.json
```

装配函数只做三件事：隔离该隔离的名字、按依赖顺序挂插件、await 到就绪。

```python
async def make_scope(root, who):
    scope = root
    for name in ("store", "todo"):
        scope = scope.isolate(name)
    await scope.plugin(Store, {"path": f"./data/{who}/todo.json"})
    await scope.plugin(Todo)
    await scope.plugin(reminder, {"who": who})
    return scope
```

顺序不必严格——依赖没齐的插件会自己停在 PENDING，等提供方起来再激活。但按依赖
顺序写，出错时第一个报错的就是真正的根因，更好排查。

同一个装配函数可以接三种入口，业务插件一行都不用改：CLI（`main.py` 现在就是）、
Web（Flask/FastAPI 里调 `asyncio.run(boot(...))` 后调用服务方法）、IM 机器人（回调里
做同样的事）。这是把能力做成插件而不是脚本的全部意义。

---

## 十一、怎么写测试

插件的好处是可测。测试里最重要的是把 fiber 驱动到稳定态，再看它的状态：

```python
import asyncio
from cordis_py import Context, FiberState

async def scenario():
    root = Context()
    seen = []

    def consumer(ctx, config):
        seen.append("run")

    consumer.inject = ["todo"]
    fiber = root.plugin(consumer)
    await fiber.await_()
    assert fiber.state == FiberState.PENDING      # 依赖没来，不执行
    assert seen == []

    await root.plugin(lambda ctx, config: ctx.provide("todo", 1)).await_()
    await fiber.await_()
    assert fiber.state == FiberState.ACTIVE
    assert seen == ["run"]

asyncio.run(scenario())
```

**整个场景要放在同一个事件循环里**。不要在每一条断言前各开一个 `asyncio.run`：
fiber 的加载任务是在某个循环里创建的，循环一关任务就被取消，下一次 `await` 会拿到
`CancelledError`。这是这套框架最容易踩的测试坑。

断言卸载也很直接：调用 `fiber.dispose()`，然后确认服务消失、文件关闭、监听器不再触发。
仓库里 `tests/` 下有 200 多个现成用例，覆盖了加载、解析、卸载、事件、日志各条契约，
写自己的插件前扫一遍会很有帮助。

---

## 十二、常见错误对照

`cannot get property "xxx" without inject` —— 你读了服务但没声明依赖。要么在插件上补
`inject = ["xxx"]`，要么在构造函数里缓存。

`service "xxx" has been registered at <root>` —— 同一个作用域里注册了两次同名服务。
检查是不是漏了 `isolate`，或者一个进程里挂了两份同一个 Service 插件。

`coroutine ... was never awaited` —— 你把一个协程当普通值丢掉了。所有 `plugin`、
`effect`、`dispose`、`update` 的返回值都该 await（或者交给框架的调度器）。

监听器收到了别人作用域的事件 —— 事件不隔离，按业务字段过滤。

`AttributeError: 'Context' object has no attribute 'fiber'` —— 这个已经修了；如果你
在旧版本上遇到，是作用域上下文没沿原型链找到 fiber。

`CancelledError` 出现在 `await fiber` 上 —— 你的场景跨了多个 `asyncio.run`。把整段
流程收进一个协程、只跑一次 `asyncio.run`。

---

## 十三、接下来读什么

想看完整的三件套业务（做表、记账、会议纪要）怎么组织，读 `examples/office/`：五个
服务、每人一个隔离作用域、审计插件热插拔、CLI 一套命令。

想看框架自己保证什么行为，读 `docs/CORRECTNESS.md`；想知道这些保证是怎么被机器
验证的，读 `docs/FORMAL_VERIFICATION.md`。
