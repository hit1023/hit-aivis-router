"""
LLMを使った日本語読みの正誤判定モジュール。

OpenAI互換API（Ollama など）を呼び出し、単語とカナ読みのペアが
正しいかどうかをバッチで判定する。
"""
import json
import logging

import httpx

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
あなたは日本語の専門家です。単語とその読み（カタカナ）のペアについて、読みが正しいか判定してください。
医療・美容・IT・ビジネスなどの専門用語が含まれる場合があります。

注意事項：
- 「オウ→オオ」「ウウ→ウー」など長音の音便変化は正常なので正しいと判定してください
- 同音異義語や複数の読みが存在する単語は、一般的な読みに合っていれば正しいと判定してください

入力されたJSONの各要素を判定し、必ず以下のJSON配列のみを返してください（前後の説明文不要）：
[
  {"text": "単語", "reading": "LLMが考える正しい読み（カタカナ）", "correct": true},
  {"text": "単語2", "reading": "セイシイヨミ", "correct": false, "note": "補足説明"}
]

`reading` は正誤に関わらず必ず返してください。
"""


async def verify_readings(
    pairs: list[dict],
    api_url: str,
    api_key: str,
    model: str,
    timeout: float = 120.0,
) -> list[dict]:
    """単語とカナ読みのペアをLLMで一括判定する。

    Args:
        pairs: [{"text": "単語", "kana": "カナ"}, ...]
    Returns:
        [{"text": "単語", "correct": bool, "suggested": "...", "note": "..."}, ...]
    """
    payload = json.dumps(
        [{"text": p["text"], "kana": p["kana"]} for p in pairs],
        ensure_ascii=False,
        indent=2,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{api_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"以下を判定してください：\n{payload}"},
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

    results: list[dict] = json.loads(content)
    logger.info("kana_verify: %d pairs checked by LLM (%s)", len(results), model)
    return results
