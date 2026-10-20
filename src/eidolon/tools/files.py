"""Bounded filesystem tools. All access walks directory descriptors without symlinks."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path


class FileBoundaryError(ValueError):
    pass


SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        ".hindsight-runtime",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
    }
)


class FileTools:
    def __init__(self, roots: list[str], *, max_bytes: int = 256_000):
        self.roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.max_bytes = max_bytes

    def _relative(self, path: str) -> tuple[Path, tuple[str, ...]]:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            if len(self.roots) != 1:
                raise FileBoundaryError(
                    "Use an absolute path when multiple or no roots are configured"
                )
            candidate = self.roots[0] / candidate
        if ".." in candidate.parts:
            raise FileBoundaryError("Parent traversal is not permitted")
        for root in self.roots:
            try:
                return root, candidate.relative_to(root).parts
            except ValueError:
                continue
        raise FileBoundaryError("Path is outside the permitted folders")

    @contextmanager
    def _directory(self, root: Path, parts: tuple[str, ...]):
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                if not part or part in (".", ".."):
                    raise FileBoundaryError("Invalid path component")
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def read_file(self, path: str) -> dict:
        root, parts = self._relative(path)
        if not parts:
            raise FileBoundaryError("Select a file, not a root folder")
        with self._directory(root, parts[:-1]) as directory:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise FileBoundaryError("Only ordinary files can be read")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(self.max_bytes + 1)
            finally:
                os.close(fd)
        truncated = len(data) > self.max_bytes
        data = data[: self.max_bytes]
        if b"\x00" in data:
            raise FileBoundaryError("Binary files are not supported")
        return {
            "path": str(root.joinpath(*parts)),
            "content": data.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }

    def list_files(self, path: str, limit: int = 100) -> dict:
        root, parts = self._relative(path)
        limit = max(1, min(int(limit), 500))
        with self._directory(root, parts) as fd:
            names = sorted(os.listdir(fd))
            result = []
            for name in names:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISDIR(info.st_mode) and name in SKIP_DIRECTORIES:
                    continue
                if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                    result.append(
                        {
                            "name": name,
                            "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                            "size": info.st_size,
                        }
                    )
                if len(result) >= limit:
                    break
        return {
            "path": str(root.joinpath(*parts)),
            "entries": result,
            "truncated": len(names) > limit,
        }

    def search_files(self, path: str, query: str, limit: int = 40) -> dict:
        if not query or len(query) > 500:
            raise ValueError("Search text must contain 1–500 characters")
        root, parts = self._relative(path)
        limit = max(1, min(int(limit), 100))
        results, visited, stopped = [], 0, False
        needle = query.casefold()

        def visit(fd: int, components: tuple[str, ...], depth: int):
            nonlocal visited, stopped
            if depth > 12 or stopped:
                return
            for name in sorted(os.listdir(fd)):
                visited += 1
                if visited > 1000 or len(results) >= limit:
                    stopped = True
                    return
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    full_parts = components + (name,)
                    if stat.S_ISDIR(info.st_mode):
                        if name in SKIP_DIRECTORIES:
                            continue
                        sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            visit(sub, full_parts, depth + 1)
                        finally:
                            os.close(sub)
                    elif stat.S_ISREG(info.st_mode) and info.st_size <= self.max_bytes:
                        item = self.read_file(str(root.joinpath(*full_parts)))
                        for line_no, line in enumerate(item["content"].splitlines(), 1):
                            if needle in line.casefold():
                                results.append(
                                    {"path": item["path"], "line": line_no, "text": line[:500]}
                                )
                                if len(results) >= limit:
                                    stopped = True
                                    return
                except (OSError, FileBoundaryError):
                    continue

        with self._directory(root, parts) as fd:
            visit(fd, parts, 0)
        return {"matches": results, "truncated": stopped}
