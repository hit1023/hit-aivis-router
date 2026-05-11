"""
テキスト前処理モジュール。

AivisSpeech へ送信する前に、登録されたルールでテキストを置換する。
長い置換前文字列を優先することで部分一致の競合を回避する。

置換ルールは JSON ファイルに永続化される。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TextReplacer:
    """送信テキストに置換ルールを適用するクラス。"""

    def __init__(self, rules_file: Path, seed_file: Path | None = None) -> None:
        self._file = rules_file
        self._rules: dict[str, str] = {}
        self._load()
        if not self._rules and seed_file and seed_file.exists():
            self._seed(seed_file)

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._rules = {str(k): str(v) for k, v in data.items()}
            logger.info("テキスト置換ルール %d 件をロード", len(self._rules))
        except Exception as exc:
            logger.warning("置換ルールの読み込みに失敗しました: %s", exc)

    def _seed(self, seed_file: Path) -> None:
        """name.txt（src@dst 形式）からルールを初期インポートして保存する。"""
        count = 0
        for line in seed_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "@" not in line:
                continue
            src, _, dst = line.partition("@")
            if src and dst:
                self._rules[src] = dst
                count += 1
        self._save()
        logger.info("name.txt から %d 件の置換ルールを初期インポートしました", count)

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(self._rules, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def apply(self, text: str) -> str:
        """全ルールを適用して置換後テキストを返す（長いルールを優先）。"""
        for src in sorted(self._rules, key=len, reverse=True):
            text = text.replace(src, self._rules[src])
        return text

    def add(self, src: str, dst: str) -> None:
        """ルールを追加（既存キーは上書き）して保存する。"""
        if not src:
            raise ValueError("置換前テキストが空です")
        self._rules[src] = dst
        self._save()

    def remove(self, src: str) -> bool:
        """ルールを削除して保存する。存在しなかった場合は False を返す。"""
        if src not in self._rules:
            return False
        del self._rules[src]
        self._save()
        return True

    def upsert_many(self, rules: dict[str, str]) -> tuple[int, int]:
        """複数ルールをUPSERT（追加 or 上書き）して保存する。
        Returns (inserted, updated) counts.
        """
        inserted = updated = 0
        for src, dst in rules.items():
            if not src:
                continue
            if src in self._rules:
                updated += 1
            else:
                inserted += 1
            self._rules[src] = dst
        if inserted + updated:
            self._save()
        return inserted, updated

    def get_all(self) -> dict[str, str]:
        """全ルールのコピーを返す。"""
        return dict(self._rules)
