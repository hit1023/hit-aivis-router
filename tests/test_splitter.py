from app.splitter import split_sentences


def test_split_on_sentence_end():
    assert split_sentences("今日は晴れですね。明日は雨が降るでしょう。") == ["今日は晴れですね。", "明日は雨が降るでしょう。"]


def test_short_fragment_merged_into_next():
    assert split_sentences("はい。今日はとてもいい天気ですね。") == ["はい。今日はとてもいい天気ですね。"]


def test_trailing_short_merged_into_previous():
    assert split_sentences("今日はとてもいい天気ですね。うん。") == ["今日はとてもいい天気ですね。うん。"]


def test_long_sentence_split_on_comma():
    t = "あいうえおかきくけこ、" * 8 + "終わり。"
    parts = split_sentences(t, max_len=30)
    assert len(parts) > 1 and "".join(parts) == t


def test_empty_and_no_punctuation():
    assert split_sentences("  ") == []
    assert split_sentences("こんにちは") == ["こんにちは"]


def test_long_first_sentence_cut_once_at_first_comma():
    t = "あと、この表記も今回の実装でただのノイズになってしまうので、別の表現にしてほしいですね。次の文です。"
    parts = split_sentences(t)
    assert parts[0] == "あと、この表記も今回の実装でただのノイズになってしまうので、"
    assert "".join(parts) == t
