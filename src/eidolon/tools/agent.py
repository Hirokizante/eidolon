"""Local file and command tools for explicitly enabled agent tasks."""

from __future__ import annotations

import asyncio
import codecs
import errno
import fnmatch
import json
import os
import pty
import re
import signal
import stat
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from eidolon.config import atomic_write
from .files import SKIP_DIRECTORIES


_TERMINAL_EXEC = (
    "import fcntl,os,sys,termios; "
    "fcntl.ioctl(0,termios.TIOCSCTTY,0); "
    "os.execv(sys.argv[1],sys.argv[1:])"
)


@dataclass
class CommandSession:
    command_id: str
    process: asyncio.subprocess.Process
    command: str
    cwd: str
    owner: str | None
    terminal: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    readers: list[asyncio.Task] = field(default_factory=list)
    monitor: asyncio.Task | None = None


class AgentTools:
    def __init__(self, *, cwd: Path | None = None):
        self.cwd = (cwd or Path.home()).expanduser().resolve()
        self.commands: dict[str, CommandSession] = {}
        self._spawn_lock = asyncio.Lock()
        self.max_file_bytes = 2_000_000
        self.max_output_chars = 64_000

    def _path(self, path: str) -> Path:
        if not path.strip() or "\x00" in path:
            raise ValueError("Enter a file or directory path")
        target = Path(path).expanduser()
        return (target if target.is_absolute() else self.cwd / target).resolve()

    def _text(self, path: Path) -> str:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Select an ordinary text file")
        with path.open("rb") as stream:
            data = stream.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes:
            raise ValueError("File exceeds 2 MB; use paged read_file or run_command")
        if b"\x00" in data:
            raise ValueError("Binary files require run_command")
        return data.decode("utf-8")

    def read_file(self, path: str, offset: int = 1, limit: int = 200, *,
                  max_result_chars: int = 16_000) -> dict:
        target = self._path(path)
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Select an ordinary text file")
        lines: list[bytes] = []
        size = 0
        truncated = False
        with target.open("rb") as stream:
            for _ in range(offset - 1):
                line = stream.readline(256_001)
                if len(line) > 256_000:
                    raise ValueError("Line exceeds the read limit; use run_command")
                if not line:
                    break
            for _ in range(limit):
                line = stream.readline(256_001)
                if not line:
                    break
                if size + len(line) > 256_000:
                    if not lines:
                        raise ValueError("Line exceeds the read limit; use run_command")
                    truncated = True
                    break
                lines.append(line)
                size += len(line)
            truncated = truncated or bool(stream.read(1))
        while True:
            data = b"".join(lines)
            if b"\x00" in data:
                raise ValueError("Binary files require run_command")
            result = {"path": str(target), "offset": offset, "lines": len(lines),
                      "next_offset": offset + len(lines) if truncated else None,
                      "truncated": truncated, "content": data.decode("utf-8")}
            if len(json.dumps(result, ensure_ascii=False)) <= max_result_chars:
                return result
            if len(lines) <= 1:
                raise ValueError("Line exceeds the response budget; use run_command")
            lines.pop()
            truncated = True

    def list_files(self, path: str, limit: int = 100) -> dict:
        target = self._path(path)
        entries = []
        with os.scandir(target) as directory:
            for item in directory:
                info = item.stat(follow_symlinks=False)
                kind = ("symlink" if item.is_symlink() else "directory" if item.is_dir()
                        else "file" if item.is_file() else "special")
                entries.append({"name": item.name, "kind": kind, "size": info.st_size})
                if len(entries) > limit:
                    break
        entries.sort(key=lambda item: item["name"])
        return {"path": str(target), "entries": entries[:limit],
                "truncated": len(entries) > limit}

    def search_files(self, path: str, query: str, limit: int = 40,
                     glob: str = "*", regex: bool = False) -> dict:
        if not query or len(query) > 500:
            raise ValueError("Search text must contain 1–500 characters")
        target = self._path(path)
        if not target.exists():
            raise FileNotFoundError(target)
        pattern = re.compile(query, re.IGNORECASE) if regex else None
        matches = []
        visited = 0
        is_file = target.is_file()
        paths = ((str(target.parent), [], [target.name]),) if is_file else os.walk(target)
        for directory, subdirectories, filenames in paths:
            subdirectories[:] = sorted(name for name in subdirectories if name not in SKIP_DIRECTORIES)
            for name in sorted(filenames):
                visited += 1
                if visited > 10_000:
                    return {"matches": matches, "truncated": True}
                candidate = Path(directory) / name
                relative = name if is_file else str(candidate.relative_to(target))
                if not (fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(relative, glob)):
                    continue
                try:
                    content = self._text(candidate)
                except (OSError, UnicodeError, ValueError):
                    continue
                for number, line in enumerate(content.splitlines(), 1):
                    if (pattern.search(line) if pattern else query.casefold() in line.casefold()):
                        matches.append({"path": str(candidate), "line": number, "text": line[:500]})
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def write_file(self, path: str, content: str) -> dict:
        target = self._path(path)
        if len(content.encode("utf-8")) > self.max_file_bytes:
            raise ValueError("Content exceeds 2 MB; use run_command")
        exists = target.exists()
        mode = stat.S_IMODE(target.stat().st_mode) if exists else 0o600
        if exists and not target.is_file():
            raise ValueError("Select an ordinary file")
        atomic_write(target, content, mode=mode)
        return {"path": str(target), "created": not exists, "bytes": len(content.encode("utf-8"))}

    def edit_file(self, path: str, old_text: str, new_text: str,
                  replace_all: bool = False) -> dict:
        if not old_text:
            raise ValueError("old_text must contain the exact text to replace")
        target = self._path(path)
        content = self._text(target)
        count = content.count(old_text)
        if not count:
            raise ValueError("old_text was not found; read the file before editing")
        if count > 1 and not replace_all:
            raise ValueError("old_text matches more than once; include more context or use replace_all")
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        result = self.write_file(str(target), updated)
        return result | {"replacements": count if replace_all else 1}

    def _session(self, command_id: str) -> CommandSession:
        if command_id not in self.commands:
            raise ValueError("Command session does not exist; use list_commands")
        return self.commands[command_id]

    def _append(self, session: CommandSession, name: str, text: str) -> None:
        combined = getattr(session, name) + text
        if len(combined) > self.max_output_chars:
            session.truncated = True
        setattr(session, name, combined[-self.max_output_chars:])

    async def _terminal_ready(self, descriptor: int, *, write: bool = False) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        def wake():
            if not ready.done():
                ready.set_result(None)
        (loop.add_writer if write else loop.add_reader)(descriptor, wake)
        try:
            await ready
        finally:
            (loop.remove_writer if write else loop.remove_reader)(descriptor)

    async def _capture(self, session: CommandSession, name: str,
                       stream: asyncio.StreamReader | None = None) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            if stream is not None:
                chunk = await stream.read(4096)
            else:
                await self._terminal_ready(session.terminal)
                try:
                    chunk = os.read(session.terminal, 4096)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
            if not chunk:
                break
            self._append(session, name, decoder.decode(chunk))
        self._append(session, name, decoder.decode(b"", final=True))

    @staticmethod
    def _signal(session: CommandSession, sig: int) -> None:
        try:
            os.killpg(session.process.pid, sig)
        except ProcessLookupError:
            pass

    async def _watch(self, session: CommandSession, timeout: int) -> None:
        try:
            try:
                await asyncio.wait_for(session.process.wait(), timeout)
            except TimeoutError:
                session.timed_out = True
                self._signal(session, signal.SIGKILL)
                await session.process.wait()
            try:
                await asyncio.wait_for(asyncio.gather(*session.readers), 1)
            except TimeoutError:
                self._signal(session, signal.SIGKILL)
        finally:
            if session.terminal is not None:
                os.close(session.terminal)
                session.terminal = None
            session.done.set()

    async def _result(self, session: CommandSession, yield_time_ms: int, *,
                      max_result_chars: int = 16_000) -> dict:
        if yield_time_ms and not session.done.is_set():
            try:
                await asyncio.wait_for(session.done.wait(), yield_time_ms / 1000)
            except TimeoutError:
                pass
        result = {"command_id": session.command_id,
                  "status": "completed" if session.done.is_set() else "running",
                  "exit_code": session.process.returncode, "cwd": session.cwd,
                  "timed_out": session.timed_out, "truncated": session.truncated,
                  "output_remaining": True, "stdout": "", "stderr": ""}
        budget = max(0, max_result_chars - len(json.dumps(result, ensure_ascii=False)) - 64)
        # Keep unread output for subsequent calls instead of losing it to the engine's truncation.
        stdout_budget = budget // 2 if session.stderr else budget
        result["stdout"] = session.stdout[:stdout_budget]
        result["stderr"] = session.stderr[:budget - len(result["stdout"])]
        while len(json.dumps(result, ensure_ascii=False)) > max_result_chars:
            key = "stdout" if len(result["stdout"]) >= len(result["stderr"]) else "stderr"
            result[key] = result[key][:len(result[key]) // 2]
            if not result["stdout"] and not result["stderr"]:
                raise ValueError("Command metadata exceeds the response budget")
        session.stdout = session.stdout[len(result["stdout"]):]
        session.stderr = session.stderr[len(result["stderr"]):]
        session.truncated = False
        result["output_remaining"] = bool(session.stdout or session.stderr)
        return result

    async def run_command(self, command: str, cwd: str = "", yield_time_ms: int = 1000,
                          timeout: int = 600, tty: bool = False, *, owner: str | None = None,
                          authorized: Callable[[], bool] | None = None,
                          max_result_chars: int = 16_000) -> dict:
        if not command.strip() or len(command) > 100_000 or "\x00" in command:
            raise ValueError("Command must contain 1–100000 characters without null bytes")
        directory = self._path(cwd) if cwd else self.cwd
        if not directory.is_dir():
            raise ValueError("Command working directory does not exist")
        async with self._spawn_lock:
            if authorized and not authorized():
                raise ValueError("Agent mode is disabled for this response")
            if sum(not item.done.is_set() for item in self.commands.values()) >= 8:
                raise ValueError("Eight commands are already running; stop or await one first")
            if len(self.commands) >= 32:
                for key, item in list(self.commands.items()):
                    if item.done.is_set():
                        del self.commands[key]
                        break
            shell = os.environ.get("SHELL") or "/bin/zsh"
            master = slave = None
            try:
                if tty:
                    master, slave = pty.openpty()
                    # Claim the controlling terminal after exec, without a threaded preexec_fn.
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-c", _TERMINAL_EXEC, shell, "-lc", command,
                        cwd=directory, stdin=slave, stdout=slave,
                        stderr=slave, start_new_session=True,
                    )
                    os.set_blocking(master, False)
                else:
                    process = await asyncio.create_subprocess_exec(
                        shell, "-lc", command, cwd=directory, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
            except BaseException:
                if master is not None:
                    os.close(master)
                raise
            finally:
                if slave is not None:
                    os.close(slave)
            session = CommandSession(uuid.uuid4().hex[:16], process, command, str(directory),
                                     owner, terminal=master)
            self.commands[session.command_id] = session
            session.readers = ([asyncio.create_task(self._capture(session, "stdout"))] if tty else [
                asyncio.create_task(self._capture(session, "stdout", process.stdout)),
                asyncio.create_task(self._capture(session, "stderr", process.stderr)),
            ])
            session.monitor = asyncio.create_task(self._watch(session, timeout))
        try:
            return await self._result(session, yield_time_ms, max_result_chars=max_result_chars)
        except asyncio.CancelledError:
            await self._terminate(session)
            raise

    async def read_command_output(self, command_id: str, yield_time_ms: int = 1000, *,
                                  max_result_chars: int = 16_000) -> dict:
        return await self._result(self._session(command_id), yield_time_ms,
                                  max_result_chars=max_result_chars)

    async def write_command_input(self, command_id: str, text: str,
                                  close_stdin: bool = False, yield_time_ms: int = 1000, *,
                                  max_result_chars: int = 16_000) -> dict:
        session = self._session(command_id)
        if session.done.is_set() or session.process.returncode is not None:
            raise ValueError("Command has already exited")
        if len(text.encode("utf-8")) > 100_000:
            raise ValueError("Command input exceeds 100 KB")
        data = text.encode("utf-8")
        if session.terminal is not None:
            if close_stdin:
                # In canonical terminals the first EOF flushes an unfinished line.
                data += b"\x04\x04" if data and not data.endswith(b"\n") else b"\x04"
            while data:
                try:
                    data = data[os.write(session.terminal, data):]
                except BlockingIOError:
                    await self._terminal_ready(session.terminal, write=True)
        else:
            session.process.stdin.write(data)
            await session.process.stdin.drain()
            if close_stdin:
                session.process.stdin.close()
        return await self._result(session, yield_time_ms, max_result_chars=max_result_chars)

    async def _terminate(self, session: CommandSession) -> None:
        # Mode-off and turn cancellation can arrive together; terminate a group once.
        async with session.stop_lock:
            if session.done.is_set():
                return
            self._signal(session, signal.SIGTERM)
            try:
                await asyncio.wait_for(session.done.wait(), 1)
            except TimeoutError:
                self._signal(session, signal.SIGKILL)
                await session.done.wait()

    async def stop_command(self, command_id: str, *, max_result_chars: int = 16_000) -> dict:
        session = self._session(command_id)
        await self._terminate(session)
        return await self._result(session, 0, max_result_chars=max_result_chars)

    def list_commands(self) -> dict:
        return {"commands": [{"command_id": item.command_id, "command": item.command[:500],
                              "cwd": item.cwd, "status": "completed" if item.done.is_set() else "running",
                              "exit_code": item.process.returncode}
                             for item in self.commands.values()]}

    async def cancel_commands(self, owner: str) -> None:
        await asyncio.gather(*(self._terminate(item) for item in self.commands.values()
                               if item.owner == owner and not item.done.is_set()))

    async def close(self) -> None:
        async with self._spawn_lock:
            await asyncio.gather(*(self._terminate(item) for item in self.commands.values()))
            await asyncio.gather(*(item.monitor for item in self.commands.values()),
                                 return_exceptions=True)
            self.commands.clear()
