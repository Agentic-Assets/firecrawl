import os

import pytest
from source_evidence import private_export


@pytest.mark.parametrize('files', [[], [('../escape', b'x')], [('/absolute', b'x')],
                                   [('a', b'x'), ('a', b'y')], [('a', 'not bytes')]])
def test_invalid_file_sets_never_create_output(tmp_path, files):
    out = tmp_path / 'export'
    with pytest.raises(ValueError):
        private_export.write(out, files)
    assert not out.exists()


def test_file_creation_failure_removes_only_own_output(tmp_path, monkeypatch):
    out = tmp_path / 'export'
    original_open = os.open
    def refuse_second(path, flags, *args, **kwargs):
        if path == 'metadata.json':
            raise OSError('fixture publication failure')
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(private_export.os, 'open', refuse_second)
    with pytest.raises(OSError):
        private_export.write(out, [('collector.json', b'{}'), ('metadata.json', b'{}')])
    assert not out.exists()


def test_parent_symlink_is_refused_without_writing_target(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        private_export.write(alias / 'export', [('collector.json', b'{}')])
    assert not list(target.iterdir())
