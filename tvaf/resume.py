import collections
import concurrent.futures
import logging
import math
import pathlib
import re
import threading
from typing import Dict
from typing import Optional
from typing import cast

import libtorrent as lt

from tvaf import driver as driver_lib
from tvaf import ltpy

_log = logging.getLogger(__name__)

RESUME_DATA_DIR_NAME = "resume"


def iter_resume_data_from_disk(config_dir: pathlib.Path):
    resume_data_dir = config_dir.joinpath(RESUME_DATA_DIR_NAME)
    if not resume_data_dir.is_dir():
        return
    for path in resume_data_dir.iterdir():
        if path.suffixes != [".resume"]:
            continue
        if not re.match(r"[0-9a-f]{40}", path.stem):
            continue

        try:
            data = path.read_bytes()
        except OSError:
            _log.exception("while reading %s", path)
            continue

        try:
            with ltpy.translate_exceptions():
                yield lt.read_resume_data(data)
        except ltpy.Error:
            _log.exception("while parsing %s", path)
            continue


# ResumeService keeps a counter of each outstanding save_resume_data call, for
# each torrent. This is used to ensure that on shutdown, we really use resume
# data generated after the session pause, and *not* resume data generated by an
# earlier call.

# ResumeService isn't synchronized with adding and removing torrents, so it may
# call save_resume_data() on an invalid handle. We just ignore exceptions for
# this.


class ResumeService(driver_lib.Ticker):
    """ResumeService owns resume data management."""

    SAVE_ALL_INTERVAL = math.tan(1.5657)  # ~196

    def __init__(self,
                 *,
                 config_dir: pathlib.Path = None,
                 session: Optional[lt.session] = None,
                 executor: Optional[concurrent.futures.Executor] = None):
        assert config_dir is not None
        assert session is not None
        assert executor is not None

        self.resume_data_dir = config_dir.joinpath(RESUME_DATA_DIR_NAME)
        self.session = session
        self.executor = executor

        self._condition = threading.Condition(threading.RLock())
        self._outstanding: Dict[str, int] = collections.defaultdict(int)
        self._handles: Dict[str, lt.torrent_handle] = dict()
        self._aborted = False
        self._last_save_all_time = -math.inf

    def _inc(self, infohash: str):
        with self._condition:
            self._outstanding[infohash] += 1
            self._condition.notify_all()

    def _dec(self, infohash: str):
        with self._condition:
            self._outstanding[infohash] -= 1
            if self._outstanding[infohash] <= 0:
                del self._outstanding[infohash]
            self._condition.notify_all()

    def _pop(self, infohash: str):
        with self._condition:
            self._outstanding.pop(infohash, None)
            self._handles.pop(infohash, None)
            self._condition.notify_all()

    def abort(self):
        with self._condition:
            assert not self._aborted
            self._aborted = True
            self._save_all(flush=True)

    def _save_all(self, flush=False):
        with self._condition:
            for infohash in self._handles:
                self._save(infohash, flush=flush)

    def _save(self, infohash: str, flush=False):
        with self._condition:
            handle = self._handles.get(infohash)
            if not handle:
                return
            flags = handle.only_if_modified
            if flush:
                flags |= handle.flush_disk_cache
            try:
                with ltpy.translate_exceptions():
                    handle.save_resume_data(flags)
            except ltpy.InvalidTorrentHandleError:
                return
            self._inc(infohash)

    def _get_resume_data_path(self, infohash: str) -> pathlib.Path:
        return self.resume_data_dir.joinpath(infohash).with_suffix(".resume")

    def _write_resume_data(self, infohash: str, atp: lt.add_torrent_params):
        try:
            self._write_resume_data_inner(infohash, atp)
        except OSError:
            _log.exception("writing resume data for %s", infohash)
        else:
            _log.debug("wrote resume data for %s", infohash)
        finally:
            self._dec(infohash)

    def _write_resume_data_inner(self, infohash: str,
                                 atp: lt.add_torrent_params):
        path = self._get_resume_data_path(infohash)
        with ltpy.translate_exceptions():
            bencoded_resume_data = lt.bencode(lt.write_resume_data(atp))
        tmp_path = path.with_suffix(".tmp")
        self.resume_data_dir.mkdir(parents=True, exist_ok=True)
        try:
            tmp_path.write_bytes(bencoded_resume_data)
            tmp_path.replace(path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    def _delete_resume_data(self, infohash: str):
        try:
            path = self._get_resume_data_path(infohash)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                _log.debug("deleted resume data for %s", infohash)
        except OSError:
            _log.exception("while deleting resume data for %s", infohash)
        finally:
            self._pop(infohash)

    def get_tick_deadline(self):
        with self._condition:
            if self._aborted:
                return math.inf
            return self._last_save_all_time + self.SAVE_ALL_INTERVAL

    def tick(self, now: float):
        with self._condition:
            self._save_all(flush=False)
            self._last_save_all_time = now

    def done(self):
        with self._condition:
            return not self._outstanding

    def wait(self):
        with self._condition:
            assert self._aborted
            return self._condition.wait_for(self.done)

    @staticmethod
    def get_alert_mask() -> int:
        return cast(int, lt.alert_category.status | lt.alert_category.storage)

    def handle_save_resume_data_alert(self, alert: lt.save_resume_data_alert):
        with ltpy.translate_exceptions():
            infohash = str(alert.handle.info_hash())
        # I have seen this happen in testing, if save_resume_data() is
        # called immediately after remove_torrent().
        with self._condition:
            if infohash not in self._handles:
                _log.debug("dropping resume data for missing torrent: %s",
                           infohash)
                return
        self.executor.submit(self._write_resume_data, infohash, alert.params)

    def handle_save_resume_data_failed_alert(
        self, alert: lt.save_resume_data_failed_alert):
        with ltpy.translate_exceptions():
            infohash = str(alert.handle.info_hash())
        self._dec(infohash)

    def handle_add_torrent_alert(self, alert: lt.add_torrent_alert):
        with self._condition:
            if self._aborted:
                _log.warning("torrent added after ResumeService aborted")
                return
            with ltpy.translate_exceptions():
                infohash = str(alert.handle.info_hash())
            self._handles[infohash] = alert.handle

    def handle_torrent_removed_alert(self, alert: lt.torrent_removed_alert):
        with ltpy.translate_exceptions():
            infohash = str(alert.info_hash)
        self.executor.submit(self._delete_resume_data, infohash)

    def _save_on_alert(self, alert: lt.torrent_alert):
        with ltpy.translate_exceptions():
            infohash = str(alert.handle.info_hash())
        self._save(infohash)

    handle_file_renamed_alert = _save_on_alert
    handle_torrent_paused_alert = _save_on_alert
    handle_torrent_finished_alert = _save_on_alert
    handle_storage_moved_alert = _save_on_alert
    handle_cache_flushed_alert = _save_on_alert

    def handle_alert(self, alert: lt.alert):
        driver_lib.dispatch(self, alert)
