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
