import json

import pytest

from eidolon.engine.presentation import PresentationParser


def test_json_every_character_boundary_streams_cues_before_text():
    reply = 'Markdown **hello** "Alex"\\\n🌸 (╹◡╹)'
    raw = json.dumps({"expression": "happy", "gesture": "wave", "reply": reply})
    parser = PresentationParser("json", expressions=["happy"], gestures=["wave"])
    emitted, cues, first_text = [], [], None
    for index, char in enumerate(raw):
        update = parser.feed(char)
        emitted.append(update.text)
        cues.extend(update.cues)
        if update.text and first_text is None:
            first_text = index
    assert not parser.finish().errors
    assert "".join(emitted) == reply
    assert cues == [{"kind": "expression", "name": "happy"}, {"kind": "gesture", "name": "wave"}]
    assert first_text < len(raw) - 5


@pytest.mark.parametrize("split", range(1, 100))
def test_prefix_handles_arbitrary_boundaries(split):
    raw = '<eidolon-cues>{"expression":"happy","gesture":null}</eidolon-cues>\nHello **there**.'
    parser = PresentationParser("prefix", expressions=["happy"])
    first = parser.feed(raw[:split])
    second = parser.feed(raw[split:])
    assert not parser.finish().errors
    assert first.text + second.text == "Hello **there**."
    assert first.cues + second.cues == [{"kind": "expression", "name": "happy"}]


@pytest.mark.parametrize("split", range(1, 70))
def test_omitted_prefix_streams_plain_reply_without_cues(split):
    raw = '\n  I remember our **garden**. 🌸\nNo cue was needed. (╹◡╹)'
    parser = PresentationParser("prefix", expressions=["happy"], gestures=["wave"])
    first = parser.feed(raw[:split])
    second = parser.feed(raw[split:])
    assert not parser.finish().errors
    assert first.text + second.text == parser.clean == raw
    assert parser.valid and parser.plain_fallback
    assert len(parser.errors) == 1
    assert not parser.cues


def test_prefix_allows_leading_whitespace_across_stream_chunks():
    parser = PresentationParser("prefix", expressions=["happy"])
    raw = '\n  <eidolon-cues>{"expression":"happy","gesture":null}</eidolon-cues>\nHi'
    emitted, cues = [], []
    for char in raw:
        update = parser.feed(char)
        emitted.append(update.text)
        cues.extend(update.cues)
    assert not parser.finish().errors
    assert "".join(emitted) == "Hi"
    assert cues == [{"kind": "expression", "name": "happy"}]
    assert not parser.plain_fallback


@pytest.mark.parametrize("chunk_size", [1, 7, 10_000])
@pytest.mark.parametrize("separator", ["\n", "\r\n"])
@pytest.mark.parametrize("reply", [
    '<eidolon-cues>{"expression":"happy","gesture":null}</eidolon-cues>\nHello.',
    'Hello.',
    '',
])
def test_prefix_omits_echoed_chat_timestamp_before_parsing_reply(chunk_size, separator, reply):
    raw = '  [2026-01-02T03:04:05+08:00]' + separator + reply
    parser = PresentationParser("prefix", expressions=["happy"])
    emitted = []
    for offset in range(0, len(raw), chunk_size):
        emitted.append(parser.feed(raw[offset:offset + chunk_size]).text)
    assert not parser.finish().errors
    assert parser.valid
    assert ''.join(emitted) == parser.clean == ('Hello.' if reply else '')
    assert parser.raw == raw
    assert parser.cues == ([{"kind": "expression", "name": "happy"}]
                          if reply.startswith('<eidolon-cues>') else [])


@pytest.mark.parametrize("raw", [
    '[2026-01-02T03:04:05]\nHello.',
    '[2026-99-99T03:04:05+00:00]\nHello.',
    '[a Markdown link](https://example.org)\nHello.',
    '[2026-01-02T03:04:05+00:00]',
    '[2026-01-02T03:04:05+00:00]\n<eidolon-cues>{"expression":',
    '[2026-01-02T03:04:05+00:00]\n[2026-01-02T03:04:05+00:00]\nHello.',
])
def test_timestamp_recovery_does_not_accept_ambiguous_or_malformed_headers(raw):
    parser = PresentationParser("prefix")
    for char in raw:
        parser.feed(char)
    parser.finish()
    assert not parser.valid
    assert not parser.clean and not parser.cues


@pytest.mark.parametrize("chunk_size", [1, 10_000])
def test_echoed_timestamp_cannot_bypass_header_limit(chunk_size):
    raw = '[2026-01-02T03:04:05+00:00]\nHello.'
    parser = PresentationParser("prefix", header_limit=12)
    for offset in range(0, len(raw), chunk_size):
        parser.feed(raw[offset:offset + chunk_size])
    parser.finish()
    assert not parser.valid and not parser.clean


@pytest.mark.parametrize("chunk_size", [1, 7, 10_000])
@pytest.mark.parametrize("overflow", [False, True])
def test_prefix_header_limit_counts_whitespace_and_closing_tag(chunk_size, overflow):
    header = '\n  <eidolon-cues>{"expression":"happy","gesture":null}</eidolon-cues>'
    raw = header + '\nHi'
    parser = PresentationParser("prefix", expressions=["happy"],
                                header_limit=len(header) - int(overflow))
    for offset in range(0, len(raw), chunk_size):
        parser.feed(raw[offset:offset + chunk_size])
    parser.finish()
    assert parser.valid == (not overflow)
    assert parser.clean == ("" if overflow else "Hi")
    assert parser.cues == ([] if overflow else [{"kind": "expression", "name": "happy"}])


@pytest.mark.parametrize("chunk_size", [1, 10_000])
def test_prefix_rejects_excessive_leading_whitespace_before_prose(chunk_size):
    raw = ' ' * 4097 + 'Hi'
    parser = PresentationParser("prefix")
    for offset in range(0, len(raw), chunk_size):
        parser.feed(raw[offset:offset + chunk_size])
    parser.finish()
    assert not parser.valid and not parser.plain_fallback
    assert not parser.clean and not parser.cues


def test_empty_chunks_preserve_prefix_separator_state():
    parser = PresentationParser("prefix")
    chunks = ['<eidolon-cues>{"expression":null,"gesture":null}</eidolon-cues>',
              '', '\r', '', '\nHi']
    text = ''.join(parser.feed(chunk).text for chunk in chunks)
    assert not parser.finish().errors
    assert text == parser.clean == 'Hi'


@pytest.mark.parametrize("raw", [
    '<eidolon-cue>{"expression":"happy","gesture":"wave"}</eidolon-cue>Hi',
    '{"expression":"happy","gesture":"wave","reply":"Hi"}',
    '```json\n{"expression":"happy","gesture":"wave","reply":"Hi"}\n```',
    '<eidolon-cues>{"expression":"happy","gesture":null}',
    ' ' * 4097,
])
def test_ambiguous_or_incomplete_prefix_does_not_fall_back(raw):
    parser = PresentationParser("prefix", expressions=["happy"], gestures=["wave"])
    for char in raw:
        parser.feed(char)
    parser.finish()
    assert not parser.valid and not parser.plain_fallback
    assert parser.clean == "" and not parser.cues


def test_reply_first_remains_incremental_and_later_cues_are_valid():
    parser = PresentationParser("json", expressions=["happy"], gestures=["wave"])
    update = parser.feed('{"reply":"Hi')
    assert update.text == "Hi"
    assert not update.cues
    update = parser.feed(' Alex","gesture":"wave","expression":"happy"}')
    assert update.text == " Alex"
    assert len(update.cues) == 2
    assert not parser.finish().errors


@pytest.mark.parametrize("raw", [
    '<eidolon-cues>{"expression":"happy","gesture":"wave","extra":null}</eidolon-cues>Hi',
    '<eidolon-cues>{"expression":"happy","expression":"happy","gesture":null}</eidolon-cues>Hi',
    '<eidolon-cues>{"expression":"happy","gesture":42}</eidolon-cues>Hi',
    '<eidolon-cues>{"expression":"happy","gesture":',
])
def test_invalid_prefix_never_admits_cues_or_leaks_controls(raw):
    parser = PresentationParser("prefix", expressions=["happy"], gestures=["wave"])
    update = parser.feed(raw)
    parser.finish()
    assert not update.cues
    assert parser.clean == ""
    assert parser.errors
    assert parser.raw == raw


def test_unknown_catalog_cue_is_quiet_error_and_reply_survives():
    parser = PresentationParser("json", gestures=["wave"])
    update = parser.feed('{"expression":"invented","gesture":null,"reply":"Hello"}')
    assert not update.cues
    assert update.errors
    assert update.text == "Hello"
    assert parser.valid


def test_control_strings_in_plain_text_are_never_actions():
    parser = PresentationParser("plain", expressions=["happy"], gestures=["wave"])
    raw = '```json\n{"expression":"happy","gesture":"wave"}\n```'
    assert parser.feed(raw).text == raw
    assert not parser.cues


def test_header_limit_and_invalid_surrogates():
    parser = PresentationParser("json", header_limit=12)
    assert parser.feed('{"expression":"').errors
    parser = PresentationParser("json")
    update = parser.feed('{"expression":null,"gesture":null,"reply":"\\ud83cX"}')
    assert update.errors
    assert parser.clean == ""


def test_incomplete_cue_never_dispatches():
    parser = PresentationParser("json", gestures=["wave"])
    assert not parser.feed('{"expression":null,"gesture":"wav').cues
    assert parser.finish().errors
    assert parser.cues == []


def test_prefix_crlf_separator_can_split_between_carriage_return_and_newline():
    parser = PresentationParser("prefix")
    first = parser.feed('<eidolon-cues>{"expression":null,"gesture":null}</eidolon-cues>\r')
    second = parser.feed("\nHello")
    assert first.text + second.text == "Hello"
    assert parser.clean == "Hello"
