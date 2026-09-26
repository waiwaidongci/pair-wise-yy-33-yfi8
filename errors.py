"""共享异常类型，供服务层与各台账模块使用。"""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message
