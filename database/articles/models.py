# database/articles/models.py
from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class ArticleDeleteResult:
    """Result of an article delete operation."""
    success: bool
    article_id: str
    error: Optional[str] = None


@dataclass
class ArticleWriteResult:
    success: bool
    article_id: str
    receipt: Optional[Any] = None
    error: Optional[str] = None
    embedding_success: bool = False
