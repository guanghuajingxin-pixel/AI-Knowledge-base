"""Processing engine boundary: no imports from vendor source trees.

EngineError 定义在包级别，供各引擎实现与路由层共用
（先定义再导入子模块，避免循环导入）。
"""


class EngineError(Exception):
    """引擎操作失败（网络/契约/业务错误统一映射）。"""


from .ragflow import RagflowEngine  # noqa: E402
from .mineru import MinerUEngine, MinerUCloudEngine, job_state  # noqa: E402

__all__ = ["EngineError", "RagflowEngine", "MinerUEngine", "MinerUCloudEngine", "job_state"]
