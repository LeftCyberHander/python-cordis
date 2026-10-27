"""教程示例的插件集合。"""

from .reminder import reminder
from .store import Store
from .todo import Todo

__all__ = ["Store", "Todo", "reminder"]
