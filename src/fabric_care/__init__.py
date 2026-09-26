"""织物洗护适配判定库：领域契约、判定引擎与运行时服务。"""

from .contracts import ContractIssue, validate_event
from .errors import DomainError, NotFound, PermissionDenied
from .judgment import CareRuleSet, JudgmentResult, assess
from .service import FabricCareService
from .store import Store

__all__ = [
    "CareRuleSet",
    "ContractIssue",
    "DomainError",
    "FabricCareService",
    "JudgmentResult",
    "NotFound",
    "PermissionDenied",
    "Store",
    "assess",
    "validate_event",
]
