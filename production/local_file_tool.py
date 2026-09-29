"""Read-only local file tool used by the desktop operator chat.

The tool intentionally has no create, edit, delete, rename, shell, or network
capability.  It searches readable documents by filename, then parses only the
small set of files selected by the Agent.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chat_attachments import AttachmentError, ChatAttachment, TEXT_EXTENSIONS, IMAGE_MEDIA_TYPES, parse_attachment_bytes


MAX_CANDIDATES = 80
MAX_SELECTED = 5
MAX_DISCOVERED_FILES = 12_000
SCAN_SECONDS = 4.0
SKIP_DIRECTORIES = {
    '$recycle.bin', 'node_modules', '.git', '.svn', '.hg', '.venv', '__pycache__',
    'windows', 'program files', 'program files (x86)', 'programdata',
    'appdata', 'application data', 'system volume information',
}
SENSITIVE_NAME_PARTS = {
    '.env', 'credential', 'credentials', 'secret', 'token', 'password', 'passwd',
    'id_rsa', 'id_ed25519', '.ssh', '.aws', '.gnupg', 'wallet',
}
READABLE_EXTENSIONS = TEXT_EXTENSIONS | {'.pdf', '.docx', '.xlsx'} | set(IMAGE_MEDIA_TYPES)


class LocalFileToolError(ValueError):
    pass


@dataclass(frozen=True)
class LocalFileCandidate:
    path: Path
    size_bytes: int
    content_type: str
    score: int

    def to_public(self) -> dict[str, Any]:
        return {
            'path': str(self.path), 'size_bytes': self.size_bytes,
            'content_type': self.content_type,
        }


def is_local_file_question(question: str) -> bool:
    normalized = question.lower()
    markers = ('文件', '文件夹', '目录', '电脑', '本地', '桌面', '下载', '文档', 'pdf', 'word', 'excel', 'xlsx', '照片', '图片', 'image')
    actions = ('读取', '读', '查看', '找', '搜索', '分析', '总结', '整理', '打开', '检索')
    return any(marker in normalized for marker in markers) and any(action in normalized for action in actions)


def _content_type(suffix: str) -> str:
    return {
        '.pdf': 'application/pdf',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
        '.csv': 'text/csv', '.tsv': 'text/tab-separated-values', '.json': 'application/json',
    }.get(suffix, 'text/plain')


def _search_roots() -> list[Path]:
    home = Path.home()
    roots = [home / name for name in ('Desktop', 'Documents', 'Downloads')]
    roots.append(home)
    if os.name == 'nt':
        roots.extend(Path(f'{letter}:\\') for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ' if Path(f'{letter}:\\').exists())
    else:
        roots.append(Path('/'))
    return list(dict.fromkeys(root for root in roots if root.exists()))


def _terms(question: str) -> list[str]:
    normalized = question.lower()
    terms = re.findall(r'[a-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}', normalized)
    extra = [block[i:i + 2] for block in re.findall(r'[\u4e00-\u9fff]{3,}', normalized) for i in range(len(block) - 1)]
    return list(dict.fromkeys(terms + extra))[:18]


def _is_sensitive(path: Path) -> bool:
    normalized = str(path).lower().replace('\\', '/')
    return any(part in normalized for part in SENSITIVE_NAME_PARTS)


class LocalFileReadTool:
    """Find and parse local documents without exposing raw filesystem access to the model."""

    def find_candidates(self, question: str) -> list[LocalFileCandidate]:
        terms = _terms(question)
        found: list[LocalFileCandidate] = []
        seen: set[Path] = set()
        discovered = 0
        started = time.monotonic()
        for root in _search_roots():
            if discovered >= MAX_DISCOVERED_FILES or time.monotonic() - started >= SCAN_SECONDS:
                break
            try:
                for current, directories, filenames in os.walk(root, topdown=True, onerror=lambda _error: None):
                    if discovered >= MAX_DISCOVERED_FILES or time.monotonic() - started >= SCAN_SECONDS:
                        break
                    directories[:] = [
                        name for name in directories
                        if name.lower() not in SKIP_DIRECTORIES and not name.startswith('.')
                    ]
                    for filename in filenames:
                        if discovered >= MAX_DISCOVERED_FILES:
                            break
                        discovered += 1
                        path = Path(current, filename)
                        suffix = path.suffix.lower()
                        if suffix not in READABLE_EXTENSIONS or path in seen or _is_sensitive(path):
                            continue
                        try:
                            size_bytes = path.stat().st_size
                        except OSError:
                            continue
                        if size_bytes <= 0 or size_bytes > 5 * 1024 * 1024:
                            continue
                        normalized = str(path).lower()
                        score = sum(20 if term in path.name.lower() else 4 if term in normalized else 0 for term in terms)
                        if terms and score == 0:
                            continue
                        seen.add(path)
                        found.append(LocalFileCandidate(path, size_bytes, _content_type(suffix), score))
            except OSError:
                continue
        found.sort(key=lambda item: (-item.score, item.path.name.lower()))
        return found[:MAX_CANDIDATES]

    def read_selected(self, candidates: list[LocalFileCandidate], paths: list[str]) -> list[ChatAttachment]:
        allowed = {str(candidate.path): candidate for candidate in candidates}
        selected = list(dict.fromkeys(path for path in paths if path in allowed))[:MAX_SELECTED]
        attachments: list[ChatAttachment] = []
        for path_text in selected:
            candidate = allowed[path_text]
            if _is_sensitive(candidate.path):
                continue
            try:
                raw = candidate.path.read_bytes()
                attachments.append(parse_attachment_bytes(candidate.path.name, candidate.content_type, raw))
            except (OSError, AttachmentError):
                continue
        return attachments
