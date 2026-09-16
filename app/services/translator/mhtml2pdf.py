"""MHTML 浏览器转换旧路径 Facade；唯一实现位于 DocumentProcessing。"""

from app.modules.document_processing.adapters.mhtml import (
    MHTMLToPDFConverter,
    convert_mhtml_to_pdf,
)

__all__ = ["MHTMLToPDFConverter", "convert_mhtml_to_pdf"]
