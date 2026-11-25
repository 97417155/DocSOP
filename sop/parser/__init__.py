"""解析器子包。"""

from .base import BaseParser, ParserRegistry
from .dispatcher import DocumentParser

__all__ = ["BaseParser", "ParserRegistry", "DocumentParser"]
