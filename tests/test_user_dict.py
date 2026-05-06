"""
ユーザー辞書エンドポイントのテスト。

テスト戦略
----------
- AivisClient をモックし、実際のバックエンドサーバーなしで動作を検証する
- 正常系・異常系・バリデーションエラー を網羅する
- 複数バックエンド時の「全サーバーへの書き込み」挙動も確認する

実行方法
--------
    pip install pytest pytest-asyncio httpx
    pytest tests/test_user_dict.py -v
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MOCK_WORD_UUID = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
MOCK_USER_DICT = {
    MOCK_WORD_UUID: {
        "surface": "テスト",
        "priority": 5,
        "pronunciation": "テスト",
        "accent_type": 2,
    }
}


def _make_pool_mock(num_backends: int = 1):
    """BackendPool の非同期メソッドをモックした MagicMock を返す。"""
    pool = MagicMock()
    pool.get_user_dict = AsyncMock(return_value=MOCK_USER_DICT)
    pool.add_user_dict_word = AsyncMock(return_value=MOCK_WORD_UUID)
    pool.update_user_dict_word = AsyncMock(return_value=None)
    pool.delete_user_dict_word = AsyncMock(return_value=None)
    # speak / speakers / models は今回未使用だが定義しておく
    pool.get_speakers = AsyncMock(return_value=[])
    pool.get_models_status = AsyncMock(return_value=[])
    pool.next = MagicMock(return_value=MagicMock())
    pool.initialize = AsyncMock()
    pool.start_cleanup_loop = AsyncMock()
    pool.close = AsyncMock()
    return pool


@pytest.fixture()
def mock_pool():
    return _make_pool_mock()


@pytest_asyncio.fixture()
async def client(mock_pool):
    """テスト用 ASGI クライアント。pool をモックで差し替える。"""
    # main モジュールの pool グローバル変数を差し替える
    from app import main as m
    m.pool = mock_pool

    transport = ASGITransport(app=m.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /user_dict
# ---------------------------------------------------------------------------

class TestGetUserDict:
    @pytest.mark.asyncio
    async def test_returns_dict(self, client, mock_pool):
        resp = await client.get("/user_dict")
        assert resp.status_code == 200
        assert resp.json() == MOCK_USER_DICT
        mock_pool.get_user_dict.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_enable_compound_accent(self, client, mock_pool):
        resp = await client.get("/user_dict", params={"enable_compound_accent": "true"})
        assert resp.status_code == 200
        mock_pool.get_user_dict.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_backend_error_returns_502(self, client, mock_pool):
        mock_pool.get_user_dict.side_effect = RuntimeError("backend down")
        resp = await client.get("/user_dict")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /user_dict
# ---------------------------------------------------------------------------

class TestAddUserDictWord:
    VALID_PAYLOAD = {
        "surface": ["東京スカイツリー"],
        "pronunciation": ["トウキョウスカイツリー"],
        "accent_type": [5],
        "word_type": "PROPER_NOUN",
        "priority": 5,
    }

    @pytest.mark.asyncio
    async def test_adds_word_returns_uuid(self, client, mock_pool):
        resp = await client.post("/user_dict", json=self.VALID_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json() == {"word_uuid": MOCK_WORD_UUID}
        mock_pool.add_user_dict_word.assert_awaited_once_with(
            surface=["東京スカイツリー"],
            pronunciation=["トウキョウスカイツリー"],
            accent_type=[5],
            word_type="PROPER_NOUN",
            priority=5,
        )

    @pytest.mark.asyncio
    async def test_compound_word(self, client, mock_pool):
        payload = {
            "surface": ["新田", "真剣佑"],
            "pronunciation": ["アラタ", "マッケンユウ"],
            "accent_type": [1, 3],
            "word_type": "PERSON_NAME",
            "priority": 7,
        }
        resp = await client.post("/user_dict", json=payload)
        assert resp.status_code == 200
        mock_pool.add_user_dict_word.assert_awaited_once_with(
            surface=["新田", "真剣佑"],
            pronunciation=["アラタ", "マッケンユウ"],
            accent_type=[1, 3],
            word_type="PERSON_NAME",
            priority=7,
        )

    @pytest.mark.asyncio
    async def test_length_mismatch_returns_422(self, client, mock_pool):
        payload = {
            "surface": ["新田", "真剣佑"],
            "pronunciation": ["アラタ"],  # 長さ不一致
            "accent_type": [1, 3],
        }
        resp = await client.post("/user_dict", json=payload)
        assert resp.status_code == 422
        mock_pool.add_user_dict_word.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_422(self, client, mock_pool):
        resp = await client.post("/user_dict", json={"surface": ["テスト"]})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_word_type_returns_422(self, client, mock_pool):
        payload = {**self.VALID_PAYLOAD, "word_type": "INVALID_TYPE"}
        resp = await client.post("/user_dict", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_priority_out_of_range_returns_422(self, client, mock_pool):
        payload = {**self.VALID_PAYLOAD, "priority": 11}
        resp = await client.post("/user_dict", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_default_word_type_and_priority(self, client, mock_pool):
        payload = {
            "surface": ["テスト"],
            "pronunciation": ["テスト"],
            "accent_type": [2],
        }
        resp = await client.post("/user_dict", json=payload)
        assert resp.status_code == 200
        mock_pool.add_user_dict_word.assert_awaited_once_with(
            surface=["テスト"],
            pronunciation=["テスト"],
            accent_type=[2],
            word_type="PROPER_NOUN",
            priority=5,
        )

    @pytest.mark.asyncio
    async def test_backend_error_returns_502(self, client, mock_pool):
        mock_pool.add_user_dict_word.side_effect = RuntimeError("backend down")
        resp = await client.post("/user_dict", json=self.VALID_PAYLOAD)
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# PUT /user_dict/{word_uuid}
# ---------------------------------------------------------------------------

class TestUpdateUserDictWord:
    VALID_PAYLOAD = {
        "surface": ["テスト"],
        "pronunciation": ["テスト"],
        "accent_type": [2],
        "word_type": "COMMON_NOUN",
        "priority": 3,
    }

    @pytest.mark.asyncio
    async def test_update_returns_204(self, client, mock_pool):
        resp = await client.put(f"/user_dict/{MOCK_WORD_UUID}", json=self.VALID_PAYLOAD)
        assert resp.status_code == 204
        mock_pool.update_user_dict_word.assert_awaited_once_with(
            word_uuid=MOCK_WORD_UUID,
            surface=["テスト"],
            pronunciation=["テスト"],
            accent_type=[2],
            word_type="COMMON_NOUN",
            priority=3,
        )

    @pytest.mark.asyncio
    async def test_length_mismatch_returns_422(self, client, mock_pool):
        payload = {
            "surface": ["新田", "真剣佑"],
            "pronunciation": ["アラタ"],
            "accent_type": [1, 3],
        }
        resp = await client.put(f"/user_dict/{MOCK_WORD_UUID}", json=payload)
        assert resp.status_code == 422
        mock_pool.update_user_dict_word.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backend_error_returns_502(self, client, mock_pool):
        mock_pool.update_user_dict_word.side_effect = RuntimeError("backend down")
        resp = await client.put(f"/user_dict/{MOCK_WORD_UUID}", json=self.VALID_PAYLOAD)
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# DELETE /user_dict/{word_uuid}
# ---------------------------------------------------------------------------

class TestDeleteUserDictWord:
    @pytest.mark.asyncio
    async def test_delete_returns_204(self, client, mock_pool):
        resp = await client.delete(f"/user_dict/{MOCK_WORD_UUID}")
        assert resp.status_code == 204
        mock_pool.delete_user_dict_word.assert_awaited_once_with(MOCK_WORD_UUID)

    @pytest.mark.asyncio
    async def test_backend_error_returns_502(self, client, mock_pool):
        mock_pool.delete_user_dict_word.side_effect = RuntimeError("backend down")
        resp = await client.delete(f"/user_dict/{MOCK_WORD_UUID}")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# BackendPool の複数バックエンド書き込み確認
# ---------------------------------------------------------------------------

class TestBackendPoolMultiBackend:
    """BackendPool が全バックエンドへ書き込みを行うことを確認する。"""

    @pytest.mark.asyncio
    async def test_add_word_syncs_to_all_backends(self):
        """add_user_dict_word は全バックエンドの client を呼び出す。"""
        from app.backend_pool import BackendPool

        client1 = MagicMock()
        client1.add_user_dict_word = AsyncMock(return_value="uuid-from-backend-1")
        client2 = MagicMock()
        client2.add_user_dict_word = AsyncMock(return_value="uuid-from-backend-2")

        pool = BackendPool.__new__(BackendPool)
        backend1 = MagicMock()
        backend1.client = client1
        backend2 = MagicMock()
        backend2.client = client2
        pool._backends = [backend1, backend2]

        result = await pool.add_user_dict_word(
            surface=["テスト"],
            pronunciation=["テスト"],
            accent_type=[2],
        )

        # 最初のバックエンドの UUID が返る
        assert result == "uuid-from-backend-1"
        client1.add_user_dict_word.assert_awaited_once()
        client2.add_user_dict_word.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_word_syncs_to_all_backends(self):
        """update_user_dict_word は全バックエンドの client を呼び出す。"""
        from app.backend_pool import BackendPool

        client1 = MagicMock()
        client1.update_user_dict_word = AsyncMock()
        client2 = MagicMock()
        client2.update_user_dict_word = AsyncMock()

        pool = BackendPool.__new__(BackendPool)
        backend1 = MagicMock()
        backend1.client = client1
        backend2 = MagicMock()
        backend2.client = client2
        pool._backends = [backend1, backend2]

        await pool.update_user_dict_word(
            word_uuid="some-uuid",
            surface=["テスト"],
            pronunciation=["テスト"],
            accent_type=[2],
        )

        client1.update_user_dict_word.assert_awaited_once()
        client2.update_user_dict_word.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_word_syncs_to_all_backends(self):
        """delete_user_dict_word は全バックエンドの client を呼び出す。"""
        from app.backend_pool import BackendPool

        client1 = MagicMock()
        client1.delete_user_dict_word = AsyncMock()
        client2 = MagicMock()
        client2.delete_user_dict_word = AsyncMock()

        pool = BackendPool.__new__(BackendPool)
        backend1 = MagicMock()
        backend1.client = client1
        backend2 = MagicMock()
        backend2.client = client2
        pool._backends = [backend1, backend2]

        await pool.delete_user_dict_word("some-uuid")

        client1.delete_user_dict_word.assert_awaited_once_with("some-uuid")
        client2.delete_user_dict_word.assert_awaited_once_with("some-uuid")

    @pytest.mark.asyncio
    async def test_get_dict_uses_first_backend_only(self):
        """get_user_dict は最初のバックエンドのみから取得する。"""
        from app.backend_pool import BackendPool

        client1 = MagicMock()
        client1.get_user_dict = AsyncMock(return_value={"uuid1": {}})
        client2 = MagicMock()
        client2.get_user_dict = AsyncMock(return_value={"uuid2": {}})

        pool = BackendPool.__new__(BackendPool)
        backend1 = MagicMock()
        backend1.client = client1
        backend2 = MagicMock()
        backend2.client = client2
        pool._backends = [backend1, backend2]

        result = await pool.get_user_dict()
        assert result == {"uuid1": {}}
        client1.get_user_dict.assert_awaited_once()
        client2.get_user_dict.assert_not_awaited()
