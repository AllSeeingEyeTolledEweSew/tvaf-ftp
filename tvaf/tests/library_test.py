# Copyright (c) 2020 AllSeeingEyeTolledEweSew
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
# REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
# AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
# INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
# LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
# OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
# PERFORMANCE OF THIS SOFTWARE.

import io
import stat as stat_lib
from typing import Any
from typing import cast
from typing import Iterable
from typing import Tuple
from typing import Union
import unittest

import libtorrent as lt

from tvaf import fs
from tvaf import library
from tvaf import protocol

from . import library_test_utils as ltu
from . import tdummy


def get_placeholder_data(info_hash: str, start: int, stop: int) -> bytes:
    data = f"{info_hash}:{start}:{stop}"
    return data.encode()


class TestLibraryService(unittest.TestCase):
    def setUp(self) -> None:
        def opener(
            info_hash: str, start: int, stop: int, _: Any
        ) -> io.BytesIO:
            return io.BytesIO(get_placeholder_data(info_hash, start, stop))

        self.torrents = {
            torrent.info_hash: torrent for torrent in ltu.TORRENTS
        }
        self.libraries = library.Libraries()
        ltu.add_test_libraries(self.libraries)
        self.libs = library.LibraryService(
            opener=opener, libraries=self.libraries
        )

    def assert_torrent_file(
        self,
        tfile: library.TorrentFile,
        *,
        dummy: tdummy.Torrent,
        start: int = None,
        stop: int = None,
        dummy_file: Union[tdummy.File, int] = None,
    ) -> None:
        info_hash = dummy.info_hash
        if dummy_file is not None:
            if isinstance(dummy_file, int):
                assert dummy is not None
                dummy_file = dummy.files[dummy_file]
            start = dummy_file.start
            stop = dummy_file.stop
            filename = protocol.decode(dummy_file.path_split[-1])
        assert filename is not None
        assert info_hash is not None
        assert start is not None
        assert stop is not None

        self.assertEqual(tfile.info_hash, info_hash)
        self.assertEqual(tfile.start, start)
        self.assertEqual(tfile.stop, stop)
        self.assertEqual(
            tfile.open(mode="rb").read(),
            get_placeholder_data(info_hash, start, stop),
        )

        atp = lt.add_torrent_params()
        atp.info_hash = lt.sha1_hash(bytes.fromhex(info_hash))
        tfile.configure_atp(atp)
        assert atp.ti is not None
        self.assertEqual(atp.ti.metadata(), lt.bencode(dummy.info))

    def assert_is_dir(self, node: fs.Node) -> None:
        self.assertEqual(node.stat().filetype, stat_lib.S_IFDIR)

    def assert_is_regular_file(self, node: fs.Node) -> None:
        self.assertEqual(node.stat().filetype, stat_lib.S_IFREG)

    def assert_is_symlink(self, node: fs.Node) -> None:
        self.assertEqual(node.stat().filetype, stat_lib.S_IFLNK)

    def assert_dirents_like(
        self, dirents: Iterable[fs.Dirent], expected: Iterable[Tuple[str, str]]
    ) -> None:
        got = [(stat_lib.filemode(d.stat.filetype), d.name) for d in dirents]
        expected = list(expected)
        # Test file types only
        if expected and len(expected[0][0]) == 1:
            got = [(mode[0], name) for mode, name in got]
        self.assertCountEqual(got, expected)

    def test_get_torrent_path(self) -> None:
        for info_hash in self.torrents:
            path = self.libs.get_torrent_path(info_hash)
            torrent_dir = self.libs.root.traverse(path)
            self.assert_is_dir(torrent_dir)

    def test_lookup_torrent(self) -> None:
        for info_hash in self.torrents:
            torrent_dir = self.libs.lookup_torrent(info_hash)
            self.assert_is_dir(torrent_dir)

    def test_browse(self) -> None:
        test_dir = fs.StaticDir()
        test_dir.mkchild(
            "single",
            fs.Symlink(target=self.libs.lookup_torrent(ltu.SINGLE.info_hash)),
        )
        self.libs.browse_nodes["test"] = test_dir

        browse = cast(fs.Dir, self.libs.root.traverse("browse"))
        self.assert_is_dir(browse)
        self.assert_dirents_like(browse.readdir(), [("d", "test")])

        test_dir = cast(fs.StaticDir, browse.lookup("test"))
        self.assert_is_dir(test_dir)

        link = cast(
            fs.Symlink,
            self.libs.root.traverse(
                "browse/test/single", follow_symlinks=False
            ),
        )
        self.assertEqual(
            str(link.readlink()), f"../../v1/{ltu.SINGLE.info_hash}"
        )

    def test_v1_lookup(self) -> None:
        for info_hash in self.torrents:
            self.assert_is_dir(self.libs.root.traverse(f"v1/{info_hash}"))

    def test_v1_lookup_bad(self) -> None:
        v1_dir = cast(fs.Dir, self.libs.root.traverse("v1"))
        self.assert_is_dir(v1_dir)
        with self.assertRaises(FileNotFoundError):
            v1_dir.lookup("0" * 40)

    def test_v1_readdir(self) -> None:
        v1_dir = cast(fs.Dir, self.libs.root.traverse("v1"))
        self.assert_is_dir(v1_dir)
        with self.assertRaises(OSError):
            list(v1_dir.readdir())

    def test_torrent_dir_readdir(self) -> None:
        for info_hash in self.torrents:
            torrent_dir = cast(
                fs.Dir, self.libs.root.traverse(f"v1/{info_hash}")
            )
            self.assert_is_dir(torrent_dir)
            self.assert_dirents_like(torrent_dir.readdir(), [("d", "test")])

    def test_torrent_dir_lookup(self) -> None:
        for info_hash in self.torrents:
            self.assert_is_dir(self.libs.root.traverse(f"v1/{info_hash}/test"))

    def test_torrent_dir_lookup_bad(self) -> None:
        for info_hash in self.torrents:
            with self.assertRaises(FileNotFoundError):
                self.assert_is_dir(
                    self.libs.root.traverse(f"v1/{info_hash}/does-not-exist")
                )

    def test_torrent_dir_with_no_network(self) -> None:
        self.libraries.networks.clear()
        torrent_dir = cast(
            fs.Dir, self.libs.root.traverse(f"v1/{ltu.SINGLE.info_hash}")
        )
        self.assert_is_dir(torrent_dir)
        self.assert_dirents_like(torrent_dir.readdir(), [])

    def test_network_readdir(self) -> None:
        for info_hash in self.torrents:
            network = cast(
                fs.Dir, self.libs.root.traverse(f"v1/{info_hash}/test")
            )
            self.assert_dirents_like(
                network.readdir(), [("d", "f"), ("d", "i")]
            )

    def test_network_lookup(self) -> None:
        for info_hash in self.torrents:
            self.assert_is_dir(
                self.libs.root.traverse(f"v1/{info_hash}/test/f")
            )
            self.assert_is_dir(
                self.libs.root.traverse(f"v1/{info_hash}/test/i")
            )

    def test_by_path_single(self) -> None:
        by_path = cast(
            fs.Dir,
            self.libs.root.traverse(f"v1/{ltu.SINGLE.info_hash}/test/f"),
        )

        self.assert_dirents_like(by_path.readdir(), [("l", "test.txt")])

        link = cast(fs.Symlink, by_path.lookup("test.txt"))
        self.assert_is_symlink(link)
        self.assertEqual(str(link.readlink()), "../i/0")

    def test_by_index_single(self) -> None:
        by_index = cast(
            fs.Dir,
            self.libs.root.traverse(f"v1/{ltu.SINGLE.info_hash}/test/i"),
        )

        self.assert_dirents_like(by_index.readdir(), [("-", "0")])

        tfile = cast(library.TorrentFile, by_index.lookup("0"))
        self.assert_torrent_file(tfile, dummy=ltu.SINGLE, dummy_file=0)

    def test_by_path_multi(self) -> None:
        by_path = cast(
            fs.Dir, self.libs.root.traverse(f"v1/{ltu.MULTI.info_hash}/test/f")
        )

        self.assert_dirents_like(by_path.readdir(), [("d", "multi")])

        subdir = cast(fs.Dir, by_path.lookup("multi"))
        self.assert_is_dir(subdir)

        self.assert_dirents_like(
            subdir.readdir(), [("l", "file.tar.gz"), ("l", "info.nfo")]
        )

        link = cast(fs.Symlink, subdir.lookup("file.tar.gz"))
        self.assert_is_symlink(link)
        self.assertEqual(str(link.readlink()), "../../i/0")

        link = cast(fs.Symlink, subdir.lookup("info.nfo"))
        self.assert_is_symlink(link)
        self.assertEqual(str(link.readlink()), "../../i/1")

    def test_by_index_multi(self) -> None:
        by_index = cast(
            fs.Dir, self.libs.root.traverse(f"v1/{ltu.MULTI.info_hash}/test/i")
        )

        self.assert_dirents_like(by_index.readdir(), [("-", "0"), ("-", "1")])

        tfile = cast(library.TorrentFile, by_index.lookup("0"))
        self.assert_torrent_file(tfile, dummy=ltu.MULTI, dummy_file=0)

        tfile = cast(library.TorrentFile, by_index.lookup("1"))
        self.assert_torrent_file(tfile, dummy=ltu.MULTI, dummy_file=1)

    def test_conflict_file(self) -> None:
        # Don't test by-path directory, as its contents are undefined. Do test
        # that the by-index path still holds file references.
        by_index = cast(
            fs.Dir,
            self.libs.root.traverse(
                f"v1/{ltu.CONFLICT_FILE.info_hash}/test/i"
            ),
        )

        self.assert_dirents_like(by_index.readdir(), [("-", "0"), ("-", "1")])

        for i in range(len(ltu.CONFLICT_FILE.files)):
            tfile = cast(library.TorrentFile, by_index.lookup(str(i)))
            self.assert_torrent_file(
                tfile, dummy=ltu.CONFLICT_FILE, dummy_file=i
            )

    def test_conflict_file_dir(self) -> None:
        # Don't test by-path directory, as its contents are undefined. Do test
        # that the by-index path still holds file references.
        by_index = cast(
            fs.Dir,
            self.libs.root.traverse(
                f"v1/{ltu.CONFLICT_FILE_DIR.info_hash}/test/i"
            ),
        )

        self.assert_dirents_like(by_index.readdir(), [("-", "0"), ("-", "1")])

        for i in range(len(ltu.CONFLICT_FILE_DIR.files)):
            tfile = cast(library.TorrentFile, by_index.lookup(str(i)))
            self.assert_torrent_file(
                tfile, dummy=ltu.CONFLICT_FILE_DIR, dummy_file=i
            )

    def test_conflict_dir_file(self) -> None:
        # Don't test by-path directory, as its contents are undefined. Do test
        # that the by-index path still holds file references.
        by_index = cast(
            fs.Dir,
            self.libs.root.traverse(
                f"v1/{ltu.CONFLICT_DIR_FILE.info_hash}/test/i"
            ),
        )

        self.assert_dirents_like(by_index.readdir(), [("-", "0"), ("-", "1")])

        for i in range(len(ltu.CONFLICT_DIR_FILE.files)):
            tfile = cast(library.TorrentFile, by_index.lookup(str(i)))
            self.assert_torrent_file(
                tfile, dummy=ltu.CONFLICT_DIR_FILE, dummy_file=i
            )

    def test_bad_paths(self) -> None:
        # All paths in BAD_PATHS are bad, so the by-path directory should be
        # empty.
        by_path = cast(
            fs.Dir,
            self.libs.root.traverse(f"v1/{ltu.BAD_PATHS.info_hash}/test/f"),
        )
        self.assert_dirents_like(by_path.readdir(), [])

        by_index = cast(
            fs.Dir,
            self.libs.root.traverse(f"v1/{ltu.BAD_PATHS.info_hash}/test/i"),
        )

        # Ensure we can still access files by index.
        self.assert_dirents_like(
            by_index.readdir(), [("-", "0"), ("-", "1"), ("-", "2")]
        )

        for i in range(len(ltu.BAD_PATHS.files)):
            tfile = cast(library.TorrentFile, by_index.lookup(str(i)))
            self.assert_torrent_file(tfile, dummy=ltu.BAD_PATHS, dummy_file=i)

    def test_padded(self) -> None:
        by_path = cast(
            fs.Dir,
            self.libs.root.traverse(
                f"v1/{ltu.PADDED.info_hash}/test/f/padded"
            ),
        )
        self.assert_dirents_like(
            by_path.readdir(), [("l", "file.tar.gz"), ("l", "info.nfo")]
        )

        by_index = cast(
            fs.Dir,
            self.libs.root.traverse(f"v1/{ltu.PADDED.info_hash}/test/i"),
        )

        # Ensure we can still access files by index.
        self.assert_dirents_like(by_index.readdir(), [("-", "0"), ("-", "2")])

        for i in range(len(ltu.PADDED.files)):
            if b"p" in ltu.PADDED.files[i].attr:
                with self.assertRaises(FileNotFoundError):
                    by_index.lookup(str(i))
            else:
                tfile = cast(library.TorrentFile, by_index.lookup(str(i)))
                self.assert_torrent_file(tfile, dummy=ltu.PADDED, dummy_file=i)
