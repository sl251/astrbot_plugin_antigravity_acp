"""Bridge only the official token pathname; never open credential contents."""

import os
import stat
from pathlib import Path

from .errors import AgyError


def bridge_auth(isolated_home: Path) -> None:
    """Validate a same-user file backend and link it into an isolated session.

    Missing files defer to the CLI's native keyring. Present but unsafe files
    still fail closed. A link follows atomic replacement of the official file;
    deletion of the isolated directory never follows it.
    """
    try:
        home = Path.home().absolute()
        if home.resolve(strict=True) != home:
            raise ValueError
        parent = home / ".gemini" / "antigravity-cli"
        target = parent / "antigravity-oauth-token"
        uid = os.getuid() if hasattr(os, "getuid") else None
        for directory in (home, home / ".gemini", parent):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                return  # Missing official files defer to the native keyring.
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError
            if uid is not None and (info.st_uid != uid or info.st_mode & 0o022):
                raise ValueError
        try:
            info = target.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size == 0
            or info.st_nlink != 1
            or (uid is not None and info.st_uid != uid)
            or info.st_mode & 0o022
            or not info.st_mode & stat.S_IRUSR
        ):
            raise ValueError
        (isolated_home / "agy" / "antigravity-oauth-token").symlink_to(target)
    except (OSError, ValueError, RuntimeError):
        raise AgyError(
            "AUTH_BRIDGE",
            "官方认证文件或父目录的归属、权限或链接边界不安全，"
            "或当前环境不支持文件认证桥。请检查同一用户的官方 agy 登录与文件权限。",
        ) from None
