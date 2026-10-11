"""Markdown chunks retain exact source lines; no external parser required."""

from dataclasses import asdict, dataclass
import hashlib
import re


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    start_line: int
    end_line: int
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def metadata(text: str) -> tuple[dict[str, str], int]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, 0
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Markdown front matter is not closed")
    values = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values, end + 1


def split_markdown(text: str, document_id: str, max_chars: int = 1600) -> list[Chunk]:
    """Split on headings/paragraphs, never cut a fenced code block.

    A long paragraph or fence stays intact (up to the explicit batch limit).
    Line ranges refer to the original file, including its front matter.
    """
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    lines = text.splitlines(keepends=True)
    _, start = metadata(text)
    sections = []
    titles: list[tuple[int, str]] = []
    title = "正文"
    fence = None
    for i in range(start, len(lines)):
        stripped = lines[i].lstrip()
        mark = re.match(r"(`{3,}|~{3,})", stripped)
        if mark:
            token = mark.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[i]) if fence is None else None
        if heading:
            if i > start:
                sections.append((start, i, title))
            level, name = len(heading.group(1)), heading.group(2)
            titles = [(depth, value) for depth, value in titles if depth < level]
            titles.append((level, name))
            title = " / ".join(value for _, value in titles)
            start = i
    if start < len(lines):
        sections.append((start, len(lines), title))
    chunks = []
    for left, right, title in sections:
        begin, length, fence = left, 0, None
        for i in range(left, right):
            length += len(lines[i])
            mark = re.match(r"\s*(`{3,}|~{3,})", lines[i])
            if mark:
                token = mark.group(1)
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
            if length >= max_chars and not lines[i].strip() and fence is None:
                _append(chunks, lines, begin, i + 1, title, document_id)
                begin, length = i + 1, 0
        _append(chunks, lines, begin, right, title, document_id)
    return chunks


def _append(chunks, lines, left, right, title, document_id):
    raw = "".join(lines[left:right])
    if raw.strip():
        cid = "chunk_" + digest(f"{document_id}:{left}:{right}:{raw}")[:24]
        chunks.append(Chunk(cid, title, left + 1, right, raw))


def batches(chunks: list[Chunk], budget: int = 8000) -> list[list[Chunk]]:
    result, current, size = [], [], 0
    for chunk in chunks:
        if len(chunk.text) > budget:
            raise ValueError(f"section too long: {chunk.title}; add paragraph breaks")
        if current and size + len(chunk.text) > budget:
            result.append(current)
            current, size = [], 0
        current.append(chunk)
        size += len(chunk.text)
    if current:
        result.append(current)
    return result
