from tvaf import fs
import time
import logging
import threading
import os
import contextlib
from typing import Generator
from typing import Mapping
from typing import Any
from tvaf import library
from tvaf import auth
import io
from typing import List
from typing import Iterator
from typing import cast
import stat as stat_lib
import errno
import tvaf.config as config_lib
from typing import Optional
from typing import Tuple
import functools
import pyftpdlib
import pyftpdlib.filesystems
import pyftpdlib.servers
import pyftpdlib.authorizers
import pyftpdlib.handlers
from typing import Callable


def _partialclass(cls, *args, **kwds):
    class Wrapped(cls):
        __init__ = functools.partialmethod(cls.__init__, *args, **kwds)
    return Wrapped


class _FS(pyftpdlib.filesystems.AbstractedFS):

    def __init__(self, *args, root:fs.Dir=None, **kwargs):
        assert root is not None
        super().__init__(*args, **kwargs)
        self.cur_dir = root

    def validpath(self, path:str) -> bool:
        # This is used to check whether a path traverses symlinks to escape a
        # home directory.
        return True

    def ftp2fs(self, ftppath:str) -> str:
        return self.ftpnorm(ftppath)

    def fs2ftp(self, fspath:str) -> str:
        return fspath

    def mkstemp(self, *args, **kwargs):
        raise fs.mkoserror(errno.EROFS)

    def mkdir(self, path:str):
        raise fs.mkoserror(errno.EROFS)

    def rmdir(self, path:str):
        raise fs.mkoserror(errno.EROFS)

    def rename(self, src:str, dst:str):
        raise fs.mkoserror(errno.EROFS)

    def chmod(self, path:str, mode:str):
        raise fs.mkoserror(errno.EROFS)

    def utime(self, path:str, timeval):
        raise fs.mkoserror(errno.EROFS)

    def get_user_by_uid(self, uid:int) -> str:
        return "root"

    def get_group_by_gid(self, gid:int) -> str:
        return "root"

    def _traverse(self, path:str, *, follow_symlinks=True) -> fs.Node:
        return self.cur_dir.traverse(path)

    def _ltraverse(self, path:str) -> fs.Node:
        return self.cur_dir.traverse(path, follow_symlinks=False)

    def _traverse_to_dir(self, path:str) -> fs.Dir:
        dir_ = cast(fs.Dir, self._traverse(path))
        if not dir_.is_dir():
            raise fs.mkoserror(errno.ENOTDIR)
        return dir_

    def _traverse_to_link(self, path:str) -> fs.Symlink:
        symlink = cast(fs.Symlink, self._ltraverse(path))
        if not symlink.is_link():
            raise fs.mkoserror(errno.EINVAL)
        return symlink

    def chdir(self, path:str):
        self.cur_dir = self._traverse_to_dir(path)
        self.cwd = str(self.cur_dir.abspath())

    def open(self, path:str, mode:str) -> io.RawIOBase:
        f = cast(fs.File, self._traverse(path))
        if f.is_dir():
            raise fs.mkoserror(errno.EISDIR)
        fp = f.open(mode)
        return fp

    def listdir(self, path:str) -> List[str]:
        dir_ = self._traverse_to_dir(path)
        return [d.name for d in dir_.readdir()]

    def listdirinfo(self, path:str) -> List[str]:
        # Doesn't seem to be used. However, the base class implements it and we
        # don't want to allow access to the filesystem.
        return self.listdir(path)

    def stat(self, path:str) -> os.stat_result:
        return self._traverse(path).stat().os()

    def lstat(self, path:str) -> os.stat_result:
        return self._ltraverse(path).stat().os()

    def readlink(self, path:str) -> str:
        return str(self._traverse_to_link(path).readlink())

    def isfile(self, path:str) -> bool:
        try:
            return self._traverse(path).is_file()
        except OSError:
            return False

    def islink(self, path:str) -> bool:
        try:
            return self._ltraverse(path).is_link()
        except OSError:
            return False

    def lexists(self, path:str) -> bool:
        try:
            self._ltraverse(path)
        except OSError:
            return False
        return True

    def isdir(self, path:str) -> bool:
        try:
            return self._traverse(path).is_dir()
        except OSError:
            return False

    def getsize(self, path:str) -> int:
        return self._traverse(path).stat().size

    def getmtime(self, path:str) -> int:
        mtime = self._traverse(path).stat().mtime
        if mtime is not None:
            return mtime
        else:
            return int(time.time())

    def realpath(self, path:str) -> str:
        return str(self.cur_dir.realpath(path))


class _Authorizer(pyftpdlib.authorizers.DummyAuthorizer):

    def __init__(self, *, auth_service:auth.AuthService=None):
        assert auth_service is not None
        self.auth_service = auth_service

    def add_user(self, *args, **kwargs):
        raise NotImplementedError

    def add_anonymous(self, *args, **kwargs):
        raise NotImplementedError

    def remove_user(self, *args, **kwargs):
        raise NotImplementedError

    def override_perm(self, *args, **kwargs):
        raise NotImplementedError

    def has_user(self, *args, **kwargs):
        raise NotImplementedError

    def get_msg_login(self, username:str) -> str:
        return "Login successful."

    def get_msg_quit(self, username:str) -> str:
        return "Goodbye."

    def get_home_dir(self, username:str) -> str:
        return "/"

    def has_perm(self, username:str, perm:str, path:str=None) -> bool:
        return perm in self.read_perms

    def get_perms(self, username:str) -> str:
        return self.read_perms

    def validate_authentication(self, username:str, password:str, handler):
        try:
            self.auth_service.auth_password_plain(username, password)
        except auth.AuthenticationFailed as e:
            raise pyftpdlib.authorizers.AuthenticationFailed(e)

    def impersonate_user(self, username:str, password:str):
        self.auth_service.push_user(username)

    def terminate_impersonation(self, username:str):
        self.auth_service.pop_user()


class _FTPHandler(pyftpdlib.handlers.FTPHandler):

    # pyftpd just tests for existence of fileno, but BytesIO and
    # BufferedTorrentIO expose fileno that raises io.UnsupportedOperation.
    use_sendfile = False

    def __init__(self, *args, root:fs.Dir=None,
            auth_service:auth.AuthService=None, **kwargs):
        assert root is not None
        assert auth_service is not None
        super().__init__(*args, **kwargs)
        self.authorizer = _Authorizer(auth_service=auth_service)
        self.abstracted_fs = _partialclass(_FS, root=root)


class FTPD(config_lib.HasConfig):

    def __init__(self, *, config:config_lib.Config=None, root:fs.Dir=None, auth_service:auth.AuthService=None):
        assert auth_service is not None
        assert root is not None
        assert config is not None

        self.auth_service = auth_service
        self.root = root

        self._lock = threading.RLock()
        self.server:Optional[pyftpdlib.servers.FTPServer] = None
        self.thread:Optional[threading.Thread] = None
        self._address:Optional[Tuple] = None

        self.set_config(config)

    def set_config(self, config:config_lib.Config):
        config.setdefault("ftp_enabled", True)
        config.setdefault("ftp_bind_address", "localhost")
        config.setdefault("ftp_port", 8821)

        with self._lock:
            enabled = config.get_bool("ftp_enabled")
            address:Optional[Tuple] = None

            # Only parse address and port if enabled
            if config.require_bool("ftp_enabled"):
                address = (config.require_str("ftp_bind_address"),
                        config.require_int("ftp_port"))

            # No change
            if address == self._address:
                return

            # Address changed or shutting down, kill the current server
            self.abort()
            self.wait()

            # Just shutting down
            if address is None:
                return

            # We have a new address
            handler = _partialclass(_FTPHandler, root=self.root,
                    auth_service=self.auth_service)
            # ThreadedFTPServer binds its socket in its constructor
            try:
                server = pyftpdlib.servers.ThreadedFTPServer(address, handler)
            except OSError as exc:
                raise config_lib.InvalidConfigError(str(exc)) from exc

            self._address = address
            self.server = server
            self.thread = threading.Thread(name="ftpd",
                    target=self.server.serve_forever)
            self.thread.start()

    def abort(self):
        with self._lock:
            if self.server is not None:
                self.server.close_all()

    def wait(self):
        with self._lock:
            if self.thread is not None:
                self.thread.join()
            self.thread = None
            self.server = None
            self._address = None