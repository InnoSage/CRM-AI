from typing import Any, Optional
from pydantic import BaseModel


class ResponseModel(BaseModel):
    isSuccess: bool
    code: str
    message: str
    data: Optional[Any] = None


class SuccessResponseModel(ResponseModel):
    isSuccess: bool = True
    code: str = "200"
    message: str = "Request succeeded"
    data: Any


class ErrorResponseModel(ResponseModel):
    isSuccess: bool = False
    code: str
    message: str
