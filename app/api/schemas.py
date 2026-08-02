from typing import Optional

from pydantic import BaseModel, Field

##############################################################################################################################

class PromptCreateRequest(BaseModel):
    name: str = Field(min_length = 1)


class PromptRenameRequest(BaseModel):
    promptID: str
    newName: str = Field(min_length = 1)


class PromptSaveRequest(BaseModel):
    promptID: str
    prompt: str


class ConversationCreateRequest(BaseModel):
    name: str = Field(min_length = 1)


class ConversationRenameRequest(BaseModel):
    historyID: str
    newName: str = Field(min_length = 1)


class QuestionSaveRequest(BaseModel):
    historyID: str
    question: str


class MessageAddRequest(BaseModel):
    historyID: str
    message: dict


class AnswerReceiveRequest(BaseModel):
    historyID: str
    recievedText: str


class ChatRequest(BaseModel):
    historyID: str
    source: str
    env: Optional[str] = None
    model: Optional[str] = None
    code: Optional[str] = None
    apiKey: Optional[str] = None
    testTimes: Optional[int] = Field(default = None, ge = 1)
    message: Optional[dict] = None
    options: Optional[dict] = None

##############################################################################################################################