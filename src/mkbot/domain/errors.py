"""可由管理接口与运行时明确处理的领域错误。"""


class MkbotError(Exception):
    """已知运行错误；message 不应包含凭据。"""


class NotFound(MkbotError):
    pass


class ContextInvalidated(MkbotError):
    """上下文引用已失效，旧生成结果不能继续执行或对外发送。"""


class Conflict(MkbotError):
    pass


class AdmissionDenied(MkbotError):
    pass


class BudgetExceeded(AdmissionDenied):
    pass


class UnsupportedSchema(MkbotError):
    pass
