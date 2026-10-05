"""领域异常类型。"""
from __future__ import annotations


class RatingError(ValueError):
    """评分领域的基础异常。"""


class ValidationFailed(RatingError):
    """入参不满足领域约束。"""


class NotFound(RatingError):
    """引用的对象不存在。"""


class Conflict(RatingError):
    """操作与当前状态冲突（如对已封账版本进行改写）。"""
