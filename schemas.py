"""
Pydantic request / response models for the paper extraction API.
"""

from typing import Any, Optional
from pydantic import BaseModel, Field


#chapter id and name 
class ChapterEntry(BaseModel):
    chapter_id: Any
    chapter_name: str

#pdf naam and url
class PaperFile(BaseModel):
    file_name: str
    url: str


class UploadPaperRequest(BaseModel):
    exam_id: str

    # Paper metadata
    board_name: str = Field(default="CBSE")
    grade: str = Field(default="Class 10")
    subject: str
    topic: str
    exam_type: str = Field(default="WEEKLY_TEST")
    difficulty_level: str = Field(default="Medium")
    start_date: str = Field(default="")
    end_date: str = Field(default="")
    include_marks_breakdown: bool = Field(default=False)
    # Langfuse tracing metadata — required
    request_id: str
    user_name: str
    
    
    

    # Chapters
    chapter_json: list[ChapterEntry]

    # PDF sources
    question_paper_url: list[PaperFile]
    
"""     ## skip for later use
    answer_paper_url: list[PaperFile] = Field(default_factory=list) """


class JobStatusResponse(BaseModel):
    job_id: str
    exam_id: str
    status: str
    created_at: str
    updated_at: str
    result: Optional[Any] = None
    error: Optional[str] = None
