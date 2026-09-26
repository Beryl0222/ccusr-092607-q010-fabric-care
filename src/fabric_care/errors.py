"""领域异常。"""


class DomainError(Exception):
    """业务规则被拒绝。"""


class PermissionDenied(DomainError):
    """权限或职责分离校验失败。"""


class NotFound(DomainError):
    """引用的对象不存在。"""
