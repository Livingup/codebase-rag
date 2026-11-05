"""Gitignore-compatible file filter and modality classifier.

Replaces the legacy three-layer (blacklist/whitelist/L3 LLM gray-area) filter with:
1. Global default ignore patterns (``FileFilterConfig.exclude_patterns``)
2. Per-repository ``.git/codebase-rag/ignore`` rules (full ``.gitignore`` syntax,
   including comments ``#``, directory rules ``dir/``, ``*``, ``**``, and
   negation ``!pattern``)
3. Automatic modality guard: only non-ignored text files and ``gemini-embedding-2``
   supported image files (``.png``, ``.jpg``, ``.jpeg``) are tracked.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, List, Literal, Optional, Sequence, Tuple

from codebase_rag.common.types import MAX_IMAGE_BYTES, FileType

if TYPE_CHECKING:
    from codebase_rag.config.schema import FileFilterConfig

SUPPORTED_IMAGE_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# Known binary/archive/media extensions that are neither text nor supported images
UNSUPPORTED_BINARY_EXTENSIONS = {
    "gif", "webp", "bmp", "ico", "tiff", "tif", "heic", "heif", "avif",
    "mp3", "wav", "ogg", "flac", "aac", "m4a", "wma",
    "mp4", "avi", "mov", "mkv", "webm", "flv", "wmv", "m4v", "mpeg", "mpg",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
    "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "zst", "lz4",
    "jar", "war", "ear", "whl", "egg",
    "pyc", "pyo", "pyd", "class", "o", "obj", "a", "lib", "so", "dll", "dylib",
    "exe", "bin", "dat", "iso", "img", "dmg", "msi", "apk", "ipa", "deb", "rpm",
    "db", "sqlite", "sqlite3", "mdb", "accdb", "ldf", "mdf",
    "woff", "woff2", "ttf", "otf", "eot",
    "wasm", "parquet", "avro", "orc", "feather", "arrow",
    "npy", "npz", "pt", "pth", "ckpt", "safetensors", "onnx", "pb", "tflite", "h5", "hdf5", "pkl", "pickle",
    "DS_Store",
}


@dataclass
class FilterResult:
    """Result of applying the file filter."""

    included: List[str]
    excluded: List[str]
    uncertain: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class _CompiledIgnoreRule:
    """Single compiled gitignore rule."""

    raw: str
    negated: bool
    dir_only: bool
    regex: re.Pattern


def _translate_gitignore_glob(pattern: str, anchored: bool, dir_only: bool) -> re.Pattern:
    """Translate a gitignore pattern into a compiled regular expression."""
    # Convert legacy patterns like `foo/**` into directory prefix match
    if pattern.endswith("/**"):
        base = pattern[:-3].lstrip("/")
        if base.startswith("**/"):
            base = base[3:]
            prefix = r"(?:^|.*/)"
        elif anchored or "/" in base:
            prefix = r"^"
        else:
            prefix = r"(?:^|.*/)"
        body = _glob_body_to_regex(base)
        return re.compile(f"{prefix}{body}(?:/.*)?$")

    if pattern.startswith("**/"):
        pattern = pattern[3:]
        anchored = False

    if anchored:
        prefix = r"^"
    else:
        prefix = r"(?:^|.*/)"

    body = _glob_body_to_regex(pattern)
    if dir_only:
        # Matches any file inside the matched directory
        return re.compile(f"{prefix}{body}/.*$")
    # Matches either the file itself or anything inside a directory of that name
    return re.compile(f"{prefix}{body}(?:$|/.*$)")


def _glob_body_to_regex(pat: str) -> str:
    """Convert glob tokens (``, ``*``, ``?``, ``[...]``) to regex."""
    i = 0
    n = len(pat)
    out: List[str] = []
    while i < n:
        c = pat[i]
        if c == "\\":
            if i + 1 < n:
                out.append(re.escape(pat[i + 1]))
                i += 2
            else:
                out.append(r"\\")
                i += 1
        elif c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                if i + 2 < n and pat[i + 2] == "/":
                    out.append(r"(?:.*/)?")
                    i += 3
                else:
                    out.append(r".*")
                    i += 2
            else:
                out.append(r"[^/]*")
                i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in ("!", "^"):
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j < n:
                stuff = pat[i + 1 : j].replace("\\", "\\\\")
                if stuff.startswith("!"):
                    stuff = "^" + stuff[1:]
                out.append(f"[{stuff}]")
                i = j + 1
            else:
                out.append(r"\[")
                i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def compile_gitignore_lines(lines: Iterable[str]) -> List[_CompiledIgnoreRule]:
    """Compile an iterable of .gitignore lines into ordered rules."""
    compiled: List[_CompiledIgnoreRule] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        # Strip unescaped trailing spaces
        while line.endswith(" ") and not line.endswith(r"\ "):
            line = line[:-1]
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith(r"\#") or line.startswith(r"\!"):
            pattern = line[1:]
            negated = False
        elif line.startswith("!"):
            negated = True
            pattern = line[1:]
        else:
            negated = False
            pattern = line

        pattern = pattern.replace("\\", "/").strip()
        if not pattern:
            continue

        dir_only = pattern.endswith("/")
        if dir_only:
            pattern = pattern.rstrip("/")
        if not pattern:
            continue

        anchored = pattern.startswith("/")
        if anchored:
            pattern = pattern.lstrip("/")

        # In gitignore, if there is a slash anywhere in the middle of the pattern,
        # the pattern is relative to the root directory (anchored).
        if "/" in pattern and not pattern.startswith("**/"):
            anchored = True

        regex = _translate_gitignore_glob(pattern, anchored=anchored, dir_only=dir_only)
        compiled.append(
            _CompiledIgnoreRule(
                raw=raw_line.strip(),
                negated=negated,
                dir_only=dir_only,
                regex=regex,
            )
        )
    return compiled


def is_unsupported_binary_extension(path: str) -> bool:
    """Return True if path has a known unsupported binary extension."""
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in UNSUPPORTED_BINARY_EXTENSIONS


def classify_file_modality(
    file_path: str,
    raw_bytes: Optional[bytes] = None,
) -> Tuple[Literal["text", "image", "unsupported_binary"], Optional[str]]:
    """Classify a file as ('text', None), ('image', mime_type), or ('unsupported_binary', reason).

    Supported image formats for gemini-embedding-2:
      - .png  -> image/png
      - .jpg / .jpeg -> image/jpeg
    """
    normalized = file_path.replace("\\", "/")
    filename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in SUPPORTED_IMAGE_MIME_BY_EXT:
        expected_mime = SUPPORTED_IMAGE_MIME_BY_EXT[ext]
        if raw_bytes is not None:
            if len(raw_bytes) == 0:
                return ("unsupported_binary", "图片文件为空（0 字节）")
            if len(raw_bytes) > MAX_IMAGE_BYTES:
                return (
                    "unsupported_binary",
                    f"图片文件大小（{len(raw_bytes)} 字节）超过 20MB 上限",
                )
            if expected_mime == "image/png" and not raw_bytes.startswith(PNG_MAGIC):
                return ("unsupported_binary", "文件扩展名为 .png，但文件头不是合法的 PNG 格式")
            if expected_mime == "image/jpeg" and not raw_bytes.startswith(JPEG_MAGIC):
                return ("unsupported_binary", "文件扩展名为 .jpg/.jpeg，但文件头不是合法的 JPEG 格式")
        return ("image", expected_mime)

    if ext in UNSUPPORTED_BINARY_EXTENSIONS:
        return (
            "unsupported_binary",
            f"不支持的文件类型 '.{ext}'（仅支持文本文件与 .png / .jpg / .jpeg 图片）",
        )

    if raw_bytes is not None:
        # Check for NUL bytes in the first 8KB (standard Git binary detection heuristic)
        if b"\x00" in raw_bytes[:8192]:
            return (
                "unsupported_binary",
                "检测到二进制文件内容（仅支持 UTF-8 文本文件与 .png / .jpg / .jpeg 图片）",
            )

    return ("text", None)


class FileFilter:
    """Compiled gitignore-style file filter.

    Evaluates global ``exclude_patterns`` followed by optional per-repository
    ``.git/codebase-rag/ignore`` rules in order (last matching rule wins, so
    ``!pattern`` in a project's ignore file can override earlier exclusions).
    Also excludes known unsupported binary extensions.
    """

    def __init__(
        self,
        config: Optional[FileFilterConfig] = None,
        *,
        exclude_patterns: Optional[Iterable[str]] = None,
        extra_ignore_lines: Optional[Iterable[str]] = None,
    ) -> None:
        self._config = config
        base_patterns = (
            list(exclude_patterns)
            if exclude_patterns is not None
            else (list(config.exclude_patterns) if config is not None else [])
        )
        self._global_rules: List[_CompiledIgnoreRule] = compile_gitignore_lines(
            base_patterns
        )
        self._extra_rules: List[_CompiledIgnoreRule] = (
            compile_gitignore_lines(extra_ignore_lines)
            if extra_ignore_lines is not None
            else []
        )

    def is_ignored(
        self,
        file_path: str,
        repo_rules: Optional[Sequence[_CompiledIgnoreRule]] = None,
    ) -> bool:
        """Return True if ``file_path`` should be excluded."""
        normalized = file_path.replace("\\", "/").lstrip("/")
        if not normalized:
            return True

        ignored = False
        for rule in self._global_rules:
            if rule.regex.search(normalized):
                ignored = not rule.negated

        for rule in self._extra_rules:
            if rule.regex.search(normalized):
                ignored = not rule.negated

        if repo_rules:
            for rule in repo_rules:
                if rule.regex.search(normalized):
                    ignored = not rule.negated

        if ignored:
            return True

        # Reject known unsupported binary extensions unless explicitly un-ignored?
        # Even if not in ignore file, unsupported binaries (.exe, .dll, .pdf, .gif, etc.)
        # cannot be embedded as text or png/jpeg.
        if is_unsupported_binary_extension(normalized):
            return True

        return False

    def filter(
        self,
        file_paths: List[str],
        repo_ignore_lines: Optional[Iterable[str]] = None,
    ) -> FilterResult:
        """Apply global + per-repo gitignore filtering to a list of file paths."""
        included: List[str] = []
        excluded: List[str] = []
        repo_rules = (
            compile_gitignore_lines(repo_ignore_lines)
            if repo_ignore_lines is not None
            else None
        )

        for path in file_paths:
            normalized = path.replace("\\", "/")
            if self.is_ignored(normalized, repo_rules=repo_rules):
                excluded.append(normalized)
            else:
                included.append(normalized)

        return FilterResult(
            included=included,
            excluded=excluded,
            uncertain=[],
        )

    def update_config(self, config: FileFilterConfig) -> None:
        """Re-compile filter rules from updated config (hot reload)."""
        self.__init__(config)

