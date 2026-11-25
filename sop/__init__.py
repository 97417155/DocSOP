"""文档解析 SOP 通用工具包。

对外提供统一入口：
    from sop import DocumentSOP
    sop = DocumentSOP.from_config()
    result = sop.process("a.pdf", strategy="parent_child")

也可单独使用：
    from sop import DocumentParser
    result = DocumentParser.from_config().parse("a.pdf")
"""

from .models import ParseResult, Chunk, ChunkResult, ProcessResult
from .parser import DocumentParser
from .chunking import ChunkFactory
from .pipeline import DocumentSOP

__all__ = [
    "DocumentSOP",
    "DocumentParser",
    "ChunkFactory",
    "ParseResult",
    "Chunk",
    "ChunkResult",
    "ProcessResult",
]
