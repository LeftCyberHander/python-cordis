"""办公三件套插件集合。

每个模块一个能力（一个 ``Service`` 子类）：documents 输出、存储、做表、记账、会议。
新增能力 = 新增一个模块 + 在 ``office.py`` 的 ``make_workspace()`` 里加一行装配。
"""

from .docs import Docs
from .ledger import Ledger
from .meetings import Meetings
from .store import Store
from .tables import Tables

__all__ = ["Docs", "Store", "Tables", "Ledger", "Meetings"]
