"""
LLMを使った日本語読みの正誤判定モジュール。

OpenAI互換API（Ollama など）を呼び出し、単語のカナ読みをLLMに独立生成させ、
AivisSpeechの読みと比較して正誤を判定する。
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

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


_CACHE_MAX = 5000


def _load_cache(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(path: str | None, cache: dict[str, str]) -> None:
    if not path:
        return
    try:
        while len(cache) > _CACHE_MAX:  # 古い順に捨てる(dictは挿入順)
            cache.pop(next(iter(cache)))
        tmp = Path(path + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("kana_verify cache save failed: %s", exc)


def _parse_json_array(content: str) -> list[dict]:
    content = content.strip()
    if "```" in content:  # マークダウンコードブロックを除去
        for part in content.split("```"):
            stripped = part.lstrip("json").strip()
            if stripped.startswith("["):
                content = stripped
                break
    return json.loads(content)


async def _ask_openai(words: list[str], api_url: str, api_key: str, model: str, timeout: float) -> list[dict]:
    payload = json.dumps([{"text": w} for w in words], ensure_ascii=False, indent=2)
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
    return _parse_json_array(resp.json()["choices"][0]["message"]["content"])


async def _ask_claude_cli(words: list[str], oauth_token: str, model: str, timeout: float) -> list[dict]:
    """Claude Code のヘッドレスモード(`claude -p`)で読みを得る。

    トークン消費を抑えるため、ツール・スキル・MCP・設定読込・セッション保存をすべて切り、
    システムプロンプトも最小の自前版に置き換える（1回あたり入力約800トークン）。
    """
    payload = json.dumps([{"text": w} for w in words], ensure_ascii=False)
    env = {
        **os.environ,
        "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
        "HOME": "/tmp/claude-home",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
    }
    os.makedirs(env["HOME"], exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p",
        "--model", model or "haiku",
        "--tools", "",
        "--system-prompt", _SYSTEM_PROMPT,
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--effort", "low",
        "--output-format", "json",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd="/tmp",
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Claude CLI がタイムアウトしました")
    if proc.returncode != 0:
        detail = (err or out).decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"Claude CLI エラー: {detail}")
    data = json.loads(out.decode())
    if data.get("is_error"):
        raise RuntimeError(f"Claude CLI エラー: {str(data.get('result'))[:300]}")
    return _parse_json_array(data.get("result", ""))


async def verify_readings(
    pairs: list[dict],
    api_url: str = "",
    api_key: str = "",
    model: str = "",
    timeout: float = 120.0,
    provider: str = "openai",
    cache_file: str | None = None,
) -> list[dict]:
    """単語リストをLLMに独立して読ませ、AivisSpeechの読みと比較して正誤を返す。

    LLMの読みは単語ごとに cache_file へ保存し、同じ単語は再問い合わせしない
    （トークン/クレジット節約）。

    Args:
        pairs: [{"text": "単語", "kana": "AivisSpeechの読み"}, ...]
        provider: "openai"（OpenAI互換API）/ "claude_cli"（api_key にOAuthトークン）
    Returns:
        [{"text": "単語", "reading": "LLMの読み", "correct": bool}, ...]
    """
    cache = _load_cache(cache_file)
    words = list(dict.fromkeys(p["text"] for p in pairs))  # 重複除去（順序維持）
    todo = [w for w in words if w not in cache]

    if todo:
        # LLMには単語名だけ送る（AivisSpeechの読みは見せない）
        if provider == "claude_cli":
            llm_results = await _ask_claude_cli(todo, api_key, model, timeout)
        else:
            llm_results = await _ask_openai(todo, api_url, api_key, model, timeout)
        for r in llm_results:
            reading = _clean_reading(r.get("reading", ""))
            if r.get("text") in todo and reading:
                cache[r["text"]] = reading
        _save_cache(cache_file, cache)

    results = []
    for p in pairs:
        reading = cache.get(p["text"], "")
        if not reading:
            continue
        results.append({
            "text": p["text"],
            "reading": reading,
            "correct": _normalize(reading) == _normalize(p["kana"]),
        })

    logger.info("kana_verify: %d words (%d via LLM %s/%s)", len(results), len(todo), provider, model)
    return results
