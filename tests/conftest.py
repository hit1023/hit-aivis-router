"""
テスト共通設定。

main.py は `/srv/static` をマウントしようとするが、ローカル環境には存在しない。
StaticFiles の __init__ をパッチして、テスト時のみ一時ディレクトリに差し替える。
"""
import os
import tempfile
import starlette.staticfiles

_tmp_static = tempfile.mkdtemp()
_orig_static_init = starlette.staticfiles.StaticFiles.__init__


def _patched_static_init(self, *, directory=None, **kwargs):
    if directory == "/srv/static":
        directory = _tmp_static
    _orig_static_init(self, directory=directory, **kwargs)


starlette.staticfiles.StaticFiles.__init__ = _patched_static_init  # type: ignore[method-assign]
