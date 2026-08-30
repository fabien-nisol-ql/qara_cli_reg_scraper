from concurrent.futures import ThreadPoolExecutor

import pytest

from qara_reg_scraper.storage.local import LocalStorage


def test_write_read_roundtrip(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    storage.write_bytes("a/b/c.txt", b"hello")
    assert storage.exists("a/b/c.txt")
    assert storage.read_bytes("a/b/c.txt") == b"hello"


def test_read_missing_raises(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        storage.read_bytes("nope.txt")


def test_exists_false_for_missing(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    assert storage.exists("nope.txt") is False


def test_list_recursive(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    storage.write_bytes("src/documents/a/current.json", b"{}")
    storage.write_bytes("src/documents/b/current.json", b"{}")
    storage.write_bytes("src/_manifest/runs/run1.json", b"{}")
    paths = sorted(storage.list("src"))
    assert paths == [
        "src/_manifest/runs/run1.json",
        "src/documents/a/current.json",
        "src/documents/b/current.json",
    ]


def test_list_missing_prefix_is_empty(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    assert list(storage.list("nothing/here")) == []


def test_path_cannot_escape_root(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    with pytest.raises(ValueError):
        storage.write_bytes("../escape.txt", b"nope")


def test_write_is_overwritten_atomically(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    storage.write_bytes("f.txt", b"first")
    storage.write_bytes("f.txt", b"second")
    assert storage.read_bytes("f.txt") == b"second"
    # no leftover .tmp file
    assert not (tmp_path / "f.txt.tmp").exists()


def test_concurrent_writers_to_the_same_path_never_collide(tmp_path):
    """Regression test for a real, live bug (confirmed 2026-08-30): two
    separate `qara-reg-scraper` processes/containers can legitimately race
    to write the SAME target path — e.g. two sources sharing a host, both
    caching that host's robots.txt for the first time in the same
    instant (eu:mdr and eu:ivdr both hitting eur-lex.europa.eu, triggered
    in the same SourceRetryScheduler tick). Before this fix, every writer
    used the identical `<path>.tmp` name, so whichever writer's atomic
    `replace()` lost the race raised FileNotFoundError renaming a `.tmp`
    file the winner had already consumed. Each writer now gets its own
    unique tmp name, so every concurrent write must succeed cleanly —
    this drives 20 threads at the same path and asserts none of them
    raise, the final content is one of the written values (not corrupt/
    truncated/mixed), and no leftover `.tmp.*` files are left behind."""
    storage = LocalStorage(root=str(tmp_path))
    writes = [f"payload-{i}".encode() for i in range(20)]

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(storage.write_bytes, "shared/host.json", payload) for payload in writes]
        for future in futures:
            future.result()  # re-raises if any writer's write_bytes raised

    assert storage.read_bytes("shared/host.json") in writes
    leftover_tmp_files = list((tmp_path / "shared").glob("*.tmp*"))
    assert leftover_tmp_files == []


def test_local_root_is_the_real_filesystem_root(tmp_path):
    """origin_pacing.py and cli.py's own same-source lock both depend on
    this returning a real, lockable filesystem path for LocalStorage -
    unlike StorageBackend's own default (None, see its docstring)."""
    storage = LocalStorage(root=str(tmp_path))
    assert storage.local_root() == tmp_path.resolve()
