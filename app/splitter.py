"""読み上げテキストを文単位に分ける（先頭の文を早く再生するためのストリーミング用）。"""
import re

_SENT_END = "。！？!?\n"
_SOFT = "、，,"


def split_sentences(text: str, min_len: int = 8, max_len: int = 60, first_max: int = 30) -> list[str]:
    """文末（。！？改行）で区切り、短すぎる断片は次の文にくっつける。
    max_len を超える文は読点で区切り直す。先頭の文が first_max より長ければ、最初の読点で
    1 回だけ切る（最初の音を早く出すため）。区切りが無ければそのまま返す。"""
    text = text.strip()
    if not text:
        return []
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENT_END:
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())

    # 長すぎる文は読点で割る
    expanded: list[str] = []
    for p in parts:
        if len(p) <= max_len:
            expanded.append(p)
            continue
        cur = ""
        for ch in p:
            cur += ch
            if ch in _SOFT and len(cur) >= min_len:
                expanded.append(cur)
                cur = ""
        if cur:
            expanded.append(cur)

    # 短い断片は次とくっつける（最後が短ければ直前にくっつける）
    merged: list[str] = []
    carry = ""
    for p in expanded:
        cur = carry + p
        if len(cur) < min_len:
            carry = cur
            continue
        merged.append(cur)
        carry = ""
    if carry:
        if merged:
            merged[-1] += carry
        else:
            merged.append(carry)
    if merged and len(merged[0]) > first_max:
        head = merged[0]
        for i, ch in enumerate(head[:-1]):
            if ch in _SOFT and i + 1 >= min_len:
                merged[0:1] = [head[: i + 1], head[i + 1:]]
                break
    return merged
