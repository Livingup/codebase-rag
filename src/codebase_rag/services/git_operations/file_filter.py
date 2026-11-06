"""Gitignore-compatible file filter and modality classifier.

Matching is delegated to the mature ``pathspec`` library (GitWildMatch
semantics, the same engine used by black and other tools) instead of a
hand-written glob translator:
1. Global default ignore patterns (``FileFilterConfig.exclude_patterns``)
2. Per-repository ``.git/codebase-rag/ignore`` rules (full ``.gitignore``
   syntax: comments ``#``, directory rules ``dir/``, ``*``, ``**``, character
   classes, backslash escapes and negation ``!pattern``)
3. Git's ancestor rule: a file cannot be re-included while any parent
   directory is still excluded (``pathspec`` does not implement this, so it is
   applied on top of the spec)
4. Automatic modality guard: only non-ignored text files and ``gemini-embedding-2``
   supported image files (``.png``, ``.jpg``, ``.jpeg``) are tracked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, List, Literal, Optional, Tuple

from pathspec import GitIgnoreSpec

from codebase_rag.common.types import MAX_IMAGE_BYTES

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


def compile_gitignore_lines(lines: Iterable[str]) -> GitIgnoreSpec:
    """Compile ``.gitignore``-format lines into a ``pathspec`` GitIgnoreSpec.

    ``GitIgnoreSpec`` implements Git's own ordering rule (the last matching
    pattern wins), so appending repository rules after global patterns preserves
    the override order without re-implementing glob translation.
    """
    return GitIgnoreSpec.from_lines(
        "gitignore",
        [line.rstrip("\r\n") for line in lines],
    )


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
    """Gitignore-compatible file filter backed by ``pathspec``.

    Evaluates global ``exclude_patterns`` first, then optional per-repository
    ``.git/codebase-rag/ignore`` rules (last matching rule wins, so ``!pattern``
    in a project's ignore file can override earlier exclusions). A file whose
    ancestor directory stays excluded is always ignored, matching Git. Also
    rejects known unsupported binary extensions.
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
        lines = list(base_patterns)
        if extra_ignore_lines is not None:
            lines.extend(extra_ignore_lines)
        self._base_spec = compile_gitignore_lines(lines)

    def _resolve_spec(self, repo_rules: Optional[GitIgnoreSpec]) -> GitIgnoreSpec:
        """Return the spec with repository rules appended in override order."""
        if repo_rules is None or not repo_rules.patterns:
            return self._base_spec
        return GitIgnoreSpec([*self._base_spec.patterns, *repo_rules.patterns])

    @staticmethod
    def _has_ignored_ancestor(spec: GitIgnoreSpec, normalized: str) -> bool:
        """Return True when any parent directory is still excluded by ``spec``.

        Git cannot re-include a file whose parent directory is excluded, and
        ``dir/`` patterns only match a trailing-slash form, so each ancestor is
        probed as a directory path.
        """
        parts = normalized.split("/")
        return any(
            spec.match_file("/".join(parts[:index]) + "/")
            for index in range(1, len(parts))
        )

    @classmethod
    def _excluded_by_spec(cls, spec: GitIgnoreSpec, normalized: str) -> bool:
        """Return True when rules (including the ancestor rule) exclude the path."""
        if cls._has_ignored_ancestor(spec, normalized):
            return True
        return spec.match_file(normalized)

    def is_ignored(
        self,
        file_path: str,
        repo_rules: Optional[GitIgnoreSpec] = None,
    ) -> bool:
        """Return True if ``file_path`` should be excluded."""
        normalized = file_path.replace("\\", "/").lstrip("/")
        if not normalized:
            return True

        if self._excluded_by_spec(self._resolve_spec(repo_rules), normalized):
            return True

        # Unsupported binaries (.exe, .dll, .pdf, .gif, ...) can be embedded
        # neither as text nor as png/jpeg, so they are rejected regardless of
        # ignore rules.
        return is_unsupported_binary_extension(normalized)

    def filter(
        self,
        file_paths: List[str],
        repo_ignore_lines: Optional[Iterable[str]] = None,
    ) -> FilterResult:
        """Apply global + per-repo gitignore filtering to a list of file paths."""
        repo_rules = (
            compile_gitignore_lines(repo_ignore_lines)
            if repo_ignore_lines is not None
            else None
        )
        spec = self._resolve_spec(repo_rules)

        included: List[str] = []
        excluded: List[str] = []
        for path in file_paths:
            normalized = path.replace("\\", "/")
            stripped = normalized.lstrip("/")
            if (
                not stripped
                or self._excluded_by_spec(spec, stripped)
                or is_unsupported_binary_extension(stripped)
            ):
                excluded.append(normalized)
            else:
                included.append(normalized)

        return FilterResult(included=included, excluded=excluded)

    def update_config(self, config: FileFilterConfig) -> None:
        """Re-compile filter rules from updated config (hot reload)."""
        self.__init__(config)
