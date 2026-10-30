"""
テスト共通設定。

main.py は `/srv/static` をマウントしようとするが、ローカル環境には存在しない。
StaticFiles の __init__ をパッチして、テスト時のみ一時ディレクトリに差し替える。
"""
import os
import tempfile
import starlette.staticfiles

# データの保存先 (/data) は本番コンテナにしか無いので、テストでは一時ディレクトリに向ける。
# app をインポートする前（Settings が作られる前）に環境変数で上書きしておく必要がある。
_tmp_data = tempfile.mkdtemp()
for _name, _file in {
    "BACKENDS_FILE": "backends.json",
    "AIVIS_CLOUD_USER_DICT_UUID_FILE": "aivis_cloud_user_dict_uuid.txt",
    "TEXT_REPLACEMENTS_FILE": "text_replacements.json",
    "COMPOUND_SPLITS_FILE": "compound_splits.json",
    "SPEAKER_PRESETS_FILE": "speaker_presets.json",
    "SPEECH_HISTORY_DB": "speech_history.db",
}.items():
    os.environ.setdefault(_name, os.path.join(_tmp_data, _file))

_tmp_static = tempfile.mkdtemp()
_orig_static_init = starlette.staticfiles.StaticFiles.__init__


def _patched_static_init(self, *, directory=None, **kwargs):
    if directory == "/srv/static":
        directory = _tmp_static
    _orig_static_init(self, directory=directory, **kwargs)


starlette.staticfiles.StaticFiles.__init__ = _patched_static_init  # type: ignore[method-assign]
