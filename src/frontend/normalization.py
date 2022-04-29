# Copyright 2021 Tiro ehf.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from itertools import islice
import re
import string
import unicodedata
import urllib.parse
from abc import ABC, abstractmethod
from typing import Iterable, List, Tuple, Union, cast

import grpc
import tokenizer
from messages import tts_frontend_message_pb2
from services import tts_frontend_service_pb2, tts_frontend_service_pb2_grpc

from src.frontend.common import consume_whitespace, utf8_byte_length
from src.frontend.ssml import OldSSMLParser as SSMLParser
from src.frontend.words import WORD_SENTENCE_SEPARATOR, Word


class NormalizerBase(ABC):
    @abstractmethod
    def normalize(self, text: str, text_is_ssml: bool) -> Iterable[Word]:
        return NotImplemented


def add_token_offsets(
    tokens: Iterable[tokenizer.Tok],
) -> List[Tuple[tokenizer.Tok, int, int]]:
    """Calculate byte offsets of each token

    Args:
      tokens: an Iterable of Tokenizer tokens

    Returns:
      A list of tuples (token, start_byte_offset, end_byte_offset)
    """
    # can't throw away sentence end/start info
    n_bytes_consumed: int = 0
    byte_offsets: List[Tuple[tokenizer.Tok, int, int]] = []
    for tok in tokens:
        if tok.kind == tokenizer.TOK.S_END:
            byte_offsets.append((tok, 0, 0))
            continue
        if not tok.origin_spans or not tok.original:
            continue
        # if is_token_spoken(tok):
        start_offset = n_bytes_consumed + utf8_byte_length(
            tok.original[: tok.origin_spans[0]]
        )
        end_offset = start_offset + utf8_byte_length(
            tok.original[tok.origin_spans[0] : tok.origin_spans[-1] + 1]
        )

        byte_offsets.append((tok, start_offset, end_offset))
        n_bytes_consumed += utf8_byte_length(tok.original)
    return byte_offsets


def _tokenize(text: str) -> Iterable[Word]:
    """Split input into tokens.

    Args:
      text: Input text to be tokenized.

    Yields:
      Word: word symbols, including their *byte* offsets in `text`.  A
        default initialized Word represents a sentence boundary.

    """
    # TODO(rkjaran): This doesn't handle embedded phonemes properly, but the
    #                previous version didn't either.
    tokens = list(tokenizer.tokenize_without_annotation(text))

    current_word_segments = []
    phoneme_str_open = False
    for tok, start_byte_offset, end_byte_offset in add_token_offsets(tokens):
        if tok.kind == tokenizer.TOK.S_END:
            # yield WORD_SENTENCE_SEPARATOR
            continue

        w = cast(str, tok.original).strip()
        if phoneme_str_open:
            current_word_segments.append(w)
            if w.endswith("}"):
                yield Word(
                    original_symbol="".join(current_word_segments),
                    symbol="".join(current_word_segments),
                    start_byte_offset=start_byte_offset,
                    end_byte_offset=end_byte_offset,
                )
                phoneme_str_open = False
                current_word_segments = []
        elif not phoneme_str_open:
            if w.startswith("{") and not w.endswith("}"):
                current_word_segments.append(w)
                phoneme_str_open = True
            else:
                yield Word(
                    original_symbol=w,
                    symbol=w,
                    start_byte_offset=start_byte_offset,
                    end_byte_offset=end_byte_offset,
                )


def parse_ssml(ssml) -> Tuple[str, List[Word]]:
    parser = SSMLParser()
    parser.feed(ssml)
    text: str = parser.get_text()
    parsed: List[Word] = parser.get_words()
    parser.close()
    return text, parsed


class BasicNormalizer(NormalizerBase):
    def normalize(self, text: str, text_is_ssml: bool = False):
        return _tokenize(text)


class GrammatekNormalizer(NormalizerBase):
    _stub: tts_frontend_service_pb2_grpc.TTSFrontendStub
    _channel: grpc.Channel

    def __init__(self, address: str):
        parsed_url = urllib.parse.urlparse(address)
        if parsed_url.scheme == "grpc":
            self._channel = grpc.insecure_channel(parsed_url.netloc)
        else:
            raise ValueError("Unsupported scheme in address '%s'", address)
        self._stub = tts_frontend_service_pb2_grpc.TTSFrontendStub(self._channel)

    def normalize(self, text: str, text_is_ssml: bool):
        if text_is_ssml:
            text_ssml = text
            text, parsed_text = parse_ssml(text)

        response: tts_frontend_message_pb2.TokenBasedNormalizedResponse = (
            self._stub.NormalizeTokenwise(
                tts_frontend_message_pb2.NormalizeRequest(content=text)
            )
        )
        # TODO(rkjaran): Here we assume that the normalization process does not change
        #   the order of tokens, so that the order of original_tokens and normalized
        #   tokens is the same.  Fix this once it doesn't hold true any more.
        sentences_with_pairs: List[List[Tuple[str, str]]] = []
        for sent in response.sentence:
            sentences_with_pairs.append(
                [
                    (token_info.original_token, token_info.normalized_token)
                    for token_info in sent.token_info
                ]
            )
        n_bytes_consumed = 0
        text_view = text_ssml if text_is_ssml else text
        p_idx: int = 0
        for sent in sentences_with_pairs:
            sent_iter = iter(sent)
            for original, normalized in sent_iter:
                spoken_ssml: bool = text_is_ssml and Word(original_symbol=original).is_spoken()

                n_chars_whitespace, n_bytes_whitespace = consume_whitespace(text_view, text_is_ssml)
                n_bytes_consumed += n_bytes_whitespace

                if spoken_ssml:
                    phoneme_multi = parsed_text[p_idx].ssml_props.tag_type == "phoneme" and parsed_text[p_idx].ssml_props.is_multi()
                    if phoneme_multi:
                        original = parsed_text[p_idx].original_symbol

                token_byte_len = utf8_byte_length(original)

                yield Word(
                    original_symbol=original,
                    symbol=normalized,  # TODO(Smári): Make none for SSML phoneme tag words
                    phone_sequence=parsed_text[p_idx].phone_sequence if spoken_ssml else [],
                    start_byte_offset=n_bytes_consumed,
                    end_byte_offset=n_bytes_consumed + token_byte_len,
                )
                n_bytes_consumed += token_byte_len
                text_view = text_view[n_chars_whitespace + len(original) :]
                
                if spoken_ssml:
                    p_idx += 1

                if spoken_ssml and phoneme_multi:
                    skip_length: int = len(original.split())
                    next(islice(sent_iter, skip_length))
                
            yield WORD_SENTENCE_SEPARATOR
