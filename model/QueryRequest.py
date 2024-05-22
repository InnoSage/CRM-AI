from pydantic import BaseModel


class QueryRequest(BaseModel):
    user_question: str
    organization_id: int
    sheet_id: int
