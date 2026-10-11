from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from jsonschema import Draft202012Validator


class ToolError(ValueError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    action: str
    input_schema: dict
    requires_approval: bool
    reason: str
    execute: Callable[[dict], dict]


def object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


class SandboxTools:
    """Use directory descriptors and O_NOFOLLOW to prevent symlink escapes."""

    def __init__(self, sandbox: Path):
        self.sandbox = sandbox.resolve()
        for name in ("notes", "outbox"):
            directory = self.sandbox / name
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise ToolError(f"sandbox/{name} 必须是真实目录")

    @staticmethod
    def path_parts(path: str) -> list[str]:
        parts = path.split("/")
        if (
            not path or path.startswith("/") or "\\" in path or "\x00" in path
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ToolError("目标路径超出 sandbox/notes 或路径格式非法")
        return parts

    @contextmanager
    def parent(self, directory: str, parts: list[str]) -> Iterator[tuple[int, str]]:
        # Open the sandbox root first: replacing it with a symlink must not escape.
        root_fd = os.open(self.sandbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def validate_note(self, arguments: dict) -> None:
        parts = self.path_parts(arguments["path"])
        try:
            with self.parent("notes", parts) as (fd, name):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ToolError("只能访问 sandbox/notes 中的普通文本文件，禁止符号链接")
        except OSError as exc:
            raise ToolError("文件不存在或路径含有不可访问的目录/符号链接") from exc

    def read_note(self, arguments: dict) -> dict:
        with self.parent("notes", self.path_parts(arguments["path"])) as (fd, name):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ToolError("只能读取普通文本文件")
                return {"content": stream.read()}

    def delete_note(self, arguments: dict) -> dict:
        self.validate_note(arguments)
        with self.parent("notes", self.path_parts(arguments["path"])) as (fd, name):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ToolError("禁止删除符号链接或非普通文件")
            os.unlink(name, dir_fd=fd)
        return {"deleted": arguments["path"]}

    def send_email(self, arguments: dict) -> dict:
        message_id = f"mail_{uuid4().hex}"
        name = f"{message_id}.json"
        with self.parent("outbox", [name]) as (fd, filename):
            file_fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=fd)
            with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
                json.dump(arguments, stream, ensure_ascii=False, indent=2)
        return {"message_id": message_id, "stored_at": f"sandbox/outbox/{name}"}


class ToolRegistry:
    def __init__(self, sandbox: Path):
        self.tools = SandboxTools(sandbox)
        path_schema = object_schema({"path": {"type": "string", "minLength": 1}})
        email_schema = object_schema({
            "to": {"type": "string", "pattern": r"^[^\s@]+@[^\s@]+\.[^\s@]+$"},
            "subject": {"type": "string", "minLength": 1},
            "body": {"type": "string"},
        })
        self._definitions = {
            tool.name: tool for tool in (
                ToolDefinition("read_note", "read", path_schema, False,
                               "合法的只读操作", self.tools.read_note),
                ToolDefinition("send_email", "send", email_schema, True,
                               "send_email 会发送指定内容（本项目保存为模拟邮件文件）",
                               self.tools.send_email),
                ToolDefinition("delete_note", "delete", path_schema, True,
                               "delete_note 会删除指定文件", self.tools.delete_note),
            )
        }
        for tool in self._definitions.values():
            Draft202012Validator.check_schema(tool.input_schema)

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def validate(self, tool: ToolDefinition, arguments: dict) -> None:
        errors = sorted(Draft202012Validator(tool.input_schema).iter_errors(arguments),
                        key=lambda error: str(list(error.path)))
        if errors:
            raise ToolError(f"参数非法：{errors[0].message}")
        if tool.name in ("read_note", "delete_note"):
            self.tools.validate_note(arguments)
