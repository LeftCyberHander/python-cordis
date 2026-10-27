import asyncio
from cordis_py import Context, Service

root = Context()

def hello(ctx, config):
    ctx.provide("greeter", "hi")

async def main():
    await root.plugin(hello)
    print(root.greeter)          # hi

asyncio.run(main())
