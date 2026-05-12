"""
LLMを使った日本語読みの正誤判定モジュール。

OpenAI互換API（Ollama など）を呼び出し、単語のカナ読みをLLMに独立生成させ、
AivisSpeechの読みと比較して正誤を判定する。
"""
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
日本語の単語リストをカタカナ読みに変換してください。

ルール：
- readingはカタカナのみ（漢字・ひらがな・スペース・句読点は絶対に使わない）
- JSON配列のみを返す（前後の説明文・コードブロック不要）

例：
入力：[{"text": "眼瞼下垂"}, {"text": "豊胸手術"}, {"text": "脂肪吸引"}]
出力：[{"text": "眼瞼下垂", "reading": "ガンケンカスイ"}, {"text": "豊胸手術", "reading": "ホウキョウシュジュツ"}, {"text": "脂肪吸引", "reading": "シボウキュウイン"}]
"""

# AivisSpeechはオウ→オオ などの長音音便表記をするため正規化して比較する
_NORMALIZE_TABLE = [
    ('ジョオ', 'ジョウ'), ('ショオ', 'ショウ'), ('チョオ', 'チョウ'),
    ('ニョオ', 'ニョウ'), ('ヒョオ', 'ヒョウ'), ('キョオ', 'キョウ'),
    ('ミョオ', 'ミョウ'), ('リョオ', 'リョウ'), ('ギョオ', 'ギョウ'),
    ('ビョオ', 'ビョウ'), ('ピョオ', 'ピョウ'),
    ('オオ', 'オウ'), ('コオ', 'コウ'), ('ソオ', 'ソウ'), ('トオ', 'トウ'),
    ('ノオ', 'ノウ'), ('ホオ', 'ホウ'), ('モオ', 'モウ'), ('ヨオ', 'ヨウ'),
    ('ロオ', 'ロウ'), ('ゴオ', 'ゴウ'), ('ゾオ', 'ゾウ'), ('ドオ', 'ドウ'),
    ('ボオ', 'ボウ'), ('ポオ', 'ポウ'),
]


def _clean_reading(text: str) -> str:
    """LLMの読みからカタカナ・長音以外を除去しスペースも削除する。"""
    text = text.replace(' ', '').replace('　', '')
    return re.sub(r'[^゠-ヿ]', '', text)  # カタカナ範囲のみ残す


def _normalize(kana: str) -> str:
    """長音の音便揺れを統一して比較用に正規化する（スペースも除去）。"""
    kana = kana.replace(' ', '').replace('　', '')
    for src, dst in _NORMALIZE_TABLE:
        kana = kana.replace(src, dst)
    return kana


async def verify_readings(
    pairs: list[dict],
    api_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> list[dict]:
    """単語リストをLLMに独立して読ませ、AivisSpeechの読みと比較して正誤を返す。

    Args:
        pairs: [{"text": "単語", "kana": "AivisSpeechの読み"}, ...]
    Returns:
        [{"text": "単語", "reading": "LLMの読み", "correct": bool}, ...]
    """
    # LLMには単語名だけ送る（AivisSpeechの読みは見せない）
    payload = json.dumps(
        [{"text": p["text"]} for p in pairs],
        ensure_ascii=False,
        indent=2,
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        resp = await client.post(
            f"{api_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ],
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"].strip()

    # マークダウンコードブロックを除去
    if "```" in content:
        parts = content.split("```")
        for part in parts:
            stripped = part.lstrip("json").strip()
            if stripped.startswith("["):
                content = stripped
                break

    llm_results: list[dict] = json.loads(content)

    # AivisSpeechの読みと正規化比較して correct を決定
    kana_map = {p["text"]: p["kana"] for p in pairs}
    results = []
    for r in llm_results:
        raw_reading = r.get("reading", "")
        llm_reading = _clean_reading(raw_reading)  # 漢字・スペース除去
        aivis_kana = kana_map.get(r["text"], "")
        correct = _normalize(llm_reading) == _normalize(aivis_kana)
        results.append({"text": r["text"], "reading": llm_reading, "correct": correct})

    logger.info("kana_verify: %d words checked by LLM (%s)", len(results), model)
    return results
