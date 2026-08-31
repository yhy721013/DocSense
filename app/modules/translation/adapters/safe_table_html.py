"""严格校验并恢复 Markdown 中被转义的表格 HTML。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag


logger = logging.getLogger(__name__)

_TABLE_OPEN_HINT = re.compile(r"<\s*table\b", flags=re.IGNORECASE)

# 只接受 MinerU/Office 当前会生成的窄标签集合。这里没有开放通用 HTML；未知标签
# 会使整个候选片段继续保持转义文本，避免“局部清洗后误放行”的安全歧义。
_TABLE_STRUCTURAL_TAGS = frozenset(
    {
        "table",
        "caption",
        "colgroup",
        "col",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
    }
)
_TABLE_CONTENT_TAGS = frozenset(
    {
        "p",
        "div",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "sub",
        "sup",
        "code",
        "br",
        "ul",
        "ol",
        "li",
        "a",
        "img",
    }
)
_TABLE_ALLOWED_TAGS = _TABLE_STRUCTURAL_TAGS | _TABLE_CONTENT_TAGS
_TABLE_VOID_TAGS = frozenset({"br", "col", "img"})
_TABLE_INLINE_TAGS = frozenset(
    {
        "span",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "sub",
        "sup",
        "code",
        "br",
        "a",
        "img",
    }
)
_TABLE_FLOW_CONTAINERS = frozenset({"caption", "th", "td", "div", "li"})
_TABLE_TEXT_CONTEXT_TAGS = frozenset(
    {
        "caption",
        "th",
        "td",
        "p",
        "div",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "sub",
        "sup",
        "code",
        "li",
        "a",
    }
)

# 这些上限不仅防止异常 rowspan/colspan 撑爆浏览器布局，也限制二次 HTML 解析的
# CPU/内存成本。所有计数均为单份文档上限，每次调用都使用局部状态，支持并发复用。
_MAX_FRAGMENT_CHARS = 8 * 1024 * 1024
_MAX_TABLES = 256
_MAX_ELEMENTS = 50_000
_MAX_CELLS = 20_000
_MAX_TABLE_DEPTH = 4
_MAX_SPAN = 1_000
_MAX_URL_CHARS = 2 * 1024 * 1024
_MAX_TEXT_ATTRIBUTE_CHARS = 4_096


@dataclass(frozen=True, slots=True)
class _ValidatedTableFragment:
    """通过结构和资源校验的隔离 HTML 片段。"""

    soup: BeautifulSoup
    table_count: int
    element_count: int
    cell_count: int


@dataclass(slots=True)
class _RestorationBudget:
    """一次模板构建内已经恢复的资源量，不在任务之间共享。"""

    table_count: int = 0
    element_count: int = 0
    cell_count: int = 0

    def can_consume(self, fragment: _ValidatedTableFragment) -> bool:
        return (
            self.table_count + fragment.table_count <= _MAX_TABLES
            and self.element_count + fragment.element_count <= _MAX_ELEMENTS
            and self.cell_count + fragment.cell_count <= _MAX_CELLS
        )

    def consume(self, fragment: _ValidatedTableFragment) -> None:
        self.table_count += fragment.table_count
        self.element_count += fragment.element_count
        self.cell_count += fragment.cell_count


class _StrictTableFragmentParser(HTMLParser):
    """只验证表格候选，不负责输出或修复格式不合法的 HTML。

    ``BeautifulSoup`` 会尽力修复缺失闭合标签；安全边界不能依赖这种宽松行为，
    因此先使用标准库 Parser 严格检查标签集合、父子关系、闭合顺序和资源上限，
    校验通过后才允许进入隔离的 BeautifulSoup 清洗阶段。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.invalid_reason: str | None = None
        self.table_count = 0
        self.element_count = 0
        self.cell_count = 0
        self._table_depth = 0

    @property
    def valid(self) -> bool:
        return (
            self.invalid_reason is None
            and not self.stack
            and self.table_count > 0
        )

    def finish(self) -> None:
        """结束增量解析，并把未闭合标签转换成明确拒绝原因。"""

        try:
            self.close()
        except Exception:
            self._reject("parser_error")
        if self.invalid_reason is None and self.stack:
            self._reject("unclosed_tag")

    def handle_starttag(self, tag: str, attrs) -> None:
        self._handle_start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._handle_start(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        if self.invalid_reason is not None:
            return
        normalized = tag.casefold()
        if normalized in _TABLE_VOID_TAGS:
            self._reject("void_tag_closed")
            return
        if not self.stack or self.stack[-1] != normalized:
            self._reject("mismatched_end_tag")
            return
        self.stack.pop()
        if normalized == "table":
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        self._validate_text(data)

    def handle_entityref(self, name: str) -> None:
        del name
        self._validate_text("entity")

    def handle_charref(self, name: str) -> None:
        del name
        self._validate_text("entity")

    def handle_comment(self, data: str) -> None:
        del data
        self._reject("comment_not_allowed")

    def handle_decl(self, decl: str) -> None:
        del decl
        self._reject("declaration_not_allowed")

    def unknown_decl(self, data: str) -> None:
        del data
        self._reject("unknown_declaration")

    def handle_pi(self, data: str) -> None:
        del data
        self._reject("processing_instruction_not_allowed")

    def _handle_start(self, tag: str, attrs, *, self_closing: bool) -> None:
        if self.invalid_reason is not None:
            return
        normalized = tag.casefold()
        if normalized not in _TABLE_ALLOWED_TAGS:
            self._reject("tag_not_allowed")
            return
        if not self._allows_child(normalized):
            self._reject("invalid_parent_child")
            return
        # HTML 的 void 标签既可写成 ``<br>`` 也可写成 ``<br/>``；其他标签
        # 不接受自闭合写法，避免宽松解析器替我们猜测结构。
        if self_closing and normalized not in _TABLE_VOID_TAGS:
            self._reject("invalid_self_closing_tag")
            return
        if not self._validate_attributes(normalized, attrs):
            return

        self.element_count += 1
        if self.element_count > _MAX_ELEMENTS:
            self._reject("limit_elements")
            return
        if normalized in {"th", "td"}:
            self.cell_count += 1
            if self.cell_count > _MAX_CELLS:
                self._reject("limit_cells")
                return
        if normalized == "table":
            self.table_count += 1
            self._table_depth += 1
            if self.table_count > _MAX_TABLES:
                self._reject("limit_tables")
                return
            if self._table_depth > _MAX_TABLE_DEPTH:
                self._reject("limit_table_depth")
                return
        if normalized not in _TABLE_VOID_TAGS:
            self.stack.append(normalized)

    def _allows_child(self, child: str) -> bool:
        parent = self.stack[-1] if self.stack else None
        if child == "table":
            return parent is None or parent in {"th", "td"}
        if parent is None:
            # 候选段落允许表格前后保留普通文本，但不允许其他顶层 HTML。
            return False
        if child in {"caption", "colgroup", "thead", "tbody", "tfoot"}:
            return parent == "table"
        if child == "col":
            return parent == "colgroup"
        if child == "tr":
            return parent in {"table", "thead", "tbody", "tfoot"}
        if child in {"th", "td"}:
            return parent == "tr"
        if child == "li":
            return parent in {"ul", "ol"}
        if child in {"p", "div", "ul", "ol"}:
            return parent in _TABLE_FLOW_CONTAINERS
        if child in _TABLE_INLINE_TAGS:
            if parent not in _TABLE_FLOW_CONTAINERS | {"p"} | _TABLE_INLINE_TAGS:
                return False
            # 嵌套链接在浏览器中会被自动重排，必须在宽松 Parser 介入前拒绝。
            return child != "a" or "a" not in self.stack
        return False

    def _validate_attributes(self, tag: str, attrs) -> bool:
        seen: set[str] = set()
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            if name in seen:
                self._reject("duplicate_attribute")
                return False
            seen.add(name)
            if name in {"rowspan", "colspan"} and tag in {"th", "td"}:
                if not self._valid_span(raw_value):
                    self._reject("invalid_cell_span")
                    return False
            if name == "span" and tag in {"col", "colgroup"}:
                if not self._valid_span(raw_value):
                    self._reject("invalid_column_span")
                    return False
        return True

    @staticmethod
    def _valid_span(value) -> bool:
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
        ):
            return False
        return 1 <= int(value) <= _MAX_SPAN

    def _validate_text(self, data: str) -> None:
        if self.invalid_reason is not None or not self.stack or not data.strip():
            return
        if self.stack[-1] not in _TABLE_TEXT_CONTEXT_TAGS:
            self._reject("text_outside_cell")

    def _reject(self, reason: str) -> None:
        if self.invalid_reason is None:
            self.invalid_reason = reason


def restore_validated_table_html(
    soup: BeautifulSoup,
    *,
    task_id: object,
) -> None:
    """恢复转义的安全表格；不符合任一规则的候选保持原始转义文本。

    原始 HTML 已经在进入 Markdown Parser 前统一转义，因此它当前只可能作为
    ``<p>`` 中的文本或 ``<br>`` 存在。这里只检查这种“纯文本段落”，不会触碰
    Markdown 自己生成的链接、列表等结构。
    """

    budget = _RestorationBudget()
    for paragraph in tuple(soup.find_all("p")):
        literal = _literal_paragraph_text(paragraph)
        if literal is None or not _TABLE_OPEN_HINT.search(literal):
            continue

        fragment, rejection_reason = _validated_table_fragment(literal)
        if fragment is None:
            if rejection_reason.startswith("limit_"):
                logger.warning(
                    "翻译 Renderer 拒绝恢复超出资源上限的表格 HTML: "
                    "task_id=%s reason=%s",
                    task_id,
                    rejection_reason,
                )
            continue
        if not budget.can_consume(fragment):
            logger.warning(
                "翻译 Renderer 拒绝恢复累计资源超限的表格 HTML: "
                "task_id=%s restored_tables=%d",
                task_id,
                budget.table_count,
            )
            continue

        _sanitize_fragment(fragment.soup)
        _replace_literal_paragraph(
            document_soup=soup,
            paragraph=paragraph,
            fragment_soup=fragment.soup,
        )
        budget.consume(fragment)

    if budget.table_count:
        logger.debug(
            "翻译 Renderer 已恢复严格校验的表格 HTML: "
            "task_id=%s tables=%d cells=%d elements=%d",
            task_id,
            budget.table_count,
            budget.cell_count,
            budget.element_count,
        )


def _literal_paragraph_text(paragraph: Tag) -> str | None:
    """把 Markdown ``nl2br`` 生成的换行还原为候选文本。"""

    parts: list[str] = []
    for child in paragraph.contents:
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue
        if (
            isinstance(child, Tag)
            and child.name
            and child.name.casefold() == "br"
            and not child.attrs
        ):
            parts.append("\n")
            continue
        # 已经包含其他真实标签时不重新解释，避免跨越 Markdown 的 DOM 边界。
        return None
    return "".join(parts)


def _validated_table_fragment(
    literal: str,
) -> tuple[_ValidatedTableFragment | None, str]:
    if len(literal) > _MAX_FRAGMENT_CHARS:
        return None, "limit_fragment_chars"

    validator = _StrictTableFragmentParser()
    try:
        validator.feed(literal)
    except Exception:
        return None, "parser_error"
    validator.finish()
    if not validator.valid:
        return None, validator.invalid_reason or "invalid_fragment"

    # 结构合法后仍在隔离 Soup 中解析，并要求节点计数完全一致。这样可以检测
    # HTML Parser 若因边界情况规范化/补写了节点，避免把“被修复”的结构放行。
    fragment_soup = BeautifulSoup(literal, "html.parser")
    tags = tuple(fragment_soup.find_all(True))
    tables = tuple(fragment_soup.find_all("table"))
    cells = tuple(fragment_soup.find_all(["th", "td"]))
    if (
        len(tags) != validator.element_count
        or len(tables) != validator.table_count
        or len(cells) != validator.cell_count
    ):
        return None, "parser_normalized_structure"

    # 空壳表格和空行不属于可展示的业务表格。每一行都必须直接包含单元格，
    # 防止嵌套表格的后代单元格错误地替外层空行通过检查。
    for table in tables:
        if not table.find("tr") or not table.find(["th", "td"]):
            return None, "table_without_cells"
    for row in fragment_soup.find_all("tr"):
        if not row.find_all(["th", "td"], recursive=False):
            return None, "row_without_cells"

    return (
        _ValidatedTableFragment(
            soup=fragment_soup,
            table_count=validator.table_count,
            element_count=validator.element_count,
            cell_count=validator.cell_count,
        ),
        "",
    )


def _sanitize_fragment(soup: BeautifulSoup) -> None:
    """按标签重建属性，只保留布局所需字段和安全 URL。"""

    # 采用“清空后重建”而不是黑名单删除，能够覆盖未来出现的事件属性、CSS、
    # ``srcdoc`` 等未知输入。翻译文本稍后仍会经过 ``html.escape``，不会借单元格
    # 文本重新注入 HTML。
    for tag in tuple(soup.find_all(True)):
        name = tag.name.casefold()
        original = dict(tag.attrs)
        sanitized: dict[str, str] = {}

        if name in {"th", "td"}:
            for attribute in ("rowspan", "colspan"):
                value = original.get(attribute)
                if isinstance(value, str) and value.isdecimal():
                    sanitized[attribute] = str(int(value))
            if name == "th":
                scope = original.get("scope")
                if isinstance(scope, str) and scope.casefold() in {
                    "row",
                    "col",
                    "rowgroup",
                    "colgroup",
                }:
                    sanitized["scope"] = scope.casefold()
        elif name in {"col", "colgroup"}:
            span = original.get("span")
            if isinstance(span, str) and span.isdecimal():
                sanitized["span"] = str(int(span))
        elif name == "a":
            href = original.get("href")
            if (
                isinstance(href, str)
                and len(href) <= _MAX_URL_CHARS
                and _safe_url(href, image=False)
            ):
                sanitized["href"] = href
            _copy_short_text_attribute(original, sanitized, "title")
        elif name == "img":
            src = original.get("src")
            if (
                not isinstance(src, str)
                or len(src) > _MAX_URL_CHARS
                or not _safe_table_image_url(src)
            ):
                # 没有可信图片源的空节点没有展示价值，直接删除而不是保留
                # 来历不明的属性组合。
                tag.decompose()
                continue
            sanitized["src"] = src
            _copy_short_text_attribute(original, sanitized, "alt")
            _copy_short_text_attribute(original, sanitized, "title")

        tag.attrs = sanitized


def _copy_short_text_attribute(
    source: dict,
    destination: dict[str, str],
    name: str,
) -> None:
    value = source.get(name)
    if isinstance(value, str) and len(value) <= _MAX_TEXT_ATTRIBUTE_CHARS:
        destination[name] = value


def _replace_literal_paragraph(
    *,
    document_soup: BeautifulSoup,
    paragraph: Tag,
    fragment_soup: BeautifulSoup,
) -> None:
    """用清洗后的表格替换候选段落，并保留表格前后的普通文本。"""

    for child in tuple(fragment_soup.contents):
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if not text:
                continue
            text_paragraph = document_soup.new_tag("p")
            lines = text.splitlines() or [text]
            for index, line in enumerate(lines):
                if index:
                    text_paragraph.append(document_soup.new_tag("br"))
                text_paragraph.append(NavigableString(line))
            paragraph.insert_before(text_paragraph)
            continue
        if isinstance(child, Tag) and child.name.casefold() == "table":
            paragraph.insert_before(child.extract())
    paragraph.decompose()


def _safe_url(value: str, *, image: bool) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    if normalized.startswith(("#", "/", "./", "../")):
        return True
    lowered = normalized.casefold()
    if image and lowered.startswith("data:image/"):
        return ";base64," in lowered
    try:
        scheme = urlsplit(normalized).scheme.casefold()
    except ValueError:
        return False
    return scheme in {"http", "https", "mailto"}


def _safe_table_image_url(value: str) -> bool:
    """恢复表格中的图片仅允许普通 URL 或常见位图 Data URL。"""

    normalized = value.strip()
    lowered = normalized.casefold()
    if lowered.startswith("data:image/"):
        return bool(
            re.match(
                r"^data:image/(?:png|jpe?g|gif|webp|bmp);base64,",
                lowered,
            )
        )
    if normalized.startswith(("#", "/", "./", "../")):
        return True
    try:
        scheme = urlsplit(normalized).scheme.casefold()
    except ValueError:
        return False
    return scheme in {"http", "https"}


__all__ = ["restore_validated_table_html"]
