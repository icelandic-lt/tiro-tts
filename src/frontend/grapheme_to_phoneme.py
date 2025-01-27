# Copyright 2021-2022 Tiro ehf.
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
import re
import string
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Literal, NewType, Optional, Union

import ice_g2p.transcriber

from src.utils.version import VersionedThing, hash_from_impl

from .lexicon import LangID, LexiconBase, SimpleInMemoryLexicon, read_kaldi_lexicon
from .phonemes import (
    SHORT_PAUSE,
    Aligner,
    Alphabet,
    PhoneSeq,
    convert_ipa_to_xsampa,
    convert_xsampa_to_ipa,
    convert_xsampa_to_xsampa_with_stress,
)
from .words import WORD_SENTENCE_SEPARATOR, Word


class GraphemeToPhonemeTranslatorBase(VersionedThing, ABC):
    @abstractmethod
    def translate(
        self,
        text: str,
        lang: LangID,
        alphabet: Alphabet = "ipa",
    ) -> PhoneSeq:
        """Translate a graphemic text into a string of phones

        Args:
            text: Freeform text to be converted to graphemes

            lang: BCP-47 language code to use

        Returns:
            A list of strings, where each string is a phone from a defined set of
            phones.

        """
        ...

    def translate_words(
        self,
        words: Iterable[Word],
        lang: LangID,
        alphabet: Alphabet = "ipa",
    ) -> Iterable[Word]:
        # TODO(rkjaran): Syllabification in IceG2PTranslator does not work well with
        #   single word inputs. Need to figure out an interface that includes the
        #   necessary context.

        ssml_tag_skiplist: List[str] = ["phoneme"]
        for word in words:

            # A translation will occur iff:
            #   a) We are not dealing with SSML and the Word is not a WORD_SENTENCE_SEPARATOR.
            #   b) We are dealing with SSML and the Word is not derived from a tag which is
            #      listed in the skiplist and the Word must not be a WORD_SENTENCE_SEPARATOR.
            should_translate: bool = (
                not word.is_from_ssml() and word != WORD_SENTENCE_SEPARATOR # a)
            ) or (
                word.is_from_ssml()                                         # b)
                and word.ssml_props.tag_type not in ssml_tag_skiplist
                and word != WORD_SENTENCE_SEPARATOR
            )

            if should_translate:
                # TODO(rkjaran): Cover more punctuation (Unicode)
                punctuation = re.sub(r"[{}\[\]]", "", string.punctuation)
                g2p_word = re.sub(r"([{}])".format(punctuation), r" \1 ", word.symbol)
                word.phone_sequence = []
                for g2p_w in g2p_word.split():
                    word.phone_sequence.extend(
                        self.translate(g2p_w, lang, alphabet=alphabet)
                    )
            yield word
            if word.is_spoken() and alphabet == "x-sampa+syll+stress":
                yield Word(phone_sequence=["."])


class EmbeddedPhonemeTranslatorBase(GraphemeToPhonemeTranslatorBase):
    @abstractmethod
    def _translate(self, w: str, lang: LangID, alphabet: Alphabet = "ipa"):
        ...

    def translate(
        self,
        text: str,
        lang: LangID,
        alphabet: Alphabet = "ipa",
    ) -> PhoneSeq:
        text = text.replace(",", " ,")
        text = text.replace(".", " .")

        def translate_fn(w: str) -> PhoneSeq:
            return self._translate(w, lang, alphabet=alphabet)

        # TODO(rkjaran): Currently we only ever encounter embedded IPA, i.e. the results
        #   of calling OldSSMLParser.get_fastspeech_string()... At this point we do not
        #   have any info on what the source alphabet, only the target, unless we assume
        #   that it will always be IPA.
        return self._process_embedded(text, translate_fn, alphabet)

    def _process_embedded(
        self, text: str, translate_fn: Callable[[str], PhoneSeq], alphabet: Alphabet
    ) -> PhoneSeq:
        phone_seq = []
        phoneme_str_open = False
        aligner = Aligner()
        for w in text.split(" "):
            if phoneme_str_open:
                phone = w
                if w.endswith("}"):
                    phone = w.replace("}", "")
                    phoneme_str_open = False

                if alphabet != "ipa":
                    phone = convert_ipa_to_xsampa([phone])[0]
                    if alphabet == "x-sampa+syll+stress":
                        phone = convert_xsampa_to_xsampa_with_stress(
                            convert_ipa_to_xsampa([phone]), ""
                        )[0]

                phone_seq.append(phone)
            elif not phoneme_str_open:
                if w.startswith("{") and w.endswith("}"):
                    cur_phone_seq = aligner.align(
                        w.replace("{", "").replace("}", "")
                    ).split(" ")
                    if alphabet != "ipa":
                        cur_phone_seq = convert_ipa_to_xsampa(cur_phone_seq)
                        if alphabet == "x-sampa+syll+stress":
                            cur_phone_seq = convert_xsampa_to_xsampa_with_stress(
                                cur_phone_seq, ""
                            )
                    phone_seq.extend(cur_phone_seq)
                elif w.startswith("{"):
                    phone = w.replace("{", "")
                    if alphabet != "ipa":
                        phone = convert_ipa_to_xsampa([phone])[0]
                        if alphabet == "x-sampa+syll+stress":
                            phone = convert_xsampa_to_xsampa_with_stress(
                                convert_ipa_to_xsampa([phone]), ""
                            )[0]
                    phone_seq.append(phone)
                    phoneme_str_open = True
                elif w in [".", ","]:
                    phone_seq.append(
                        "." if alphabet == "x-sampa+syll+stress" else SHORT_PAUSE
                    )
                else:
                    phones = translate_fn(w)
                    phone_seq.extend(phones)

        return phone_seq


class ComposedTranslator(GraphemeToPhonemeTranslatorBase):
    """ComposedTranslator

    Group together one or more translators which are used in sequence until the text is
    successfully translated (or we run out of translators).

    Example:
      >>> ComposedTranslator(LexiconGraphemeToPhonemeTranslator(...), IceG2PTranslator(...))
    """

    _translators: List[GraphemeToPhonemeTranslatorBase]
    _version_hash: Optional[str] = None

    def __init__(self, *translators):
        if len(translators) < 1:
            raise ValueError("Needs at least 1 argument.")
        self._translators = list(translators)

    @property
    def version_hash(self) -> str:
        if not self._version_hash:
            self._version_hash = hash_from_impl(
                self.__class__, "-".join(t.version_hash for t in self._translators)
            )
        return self._version_hash

    def translate(
        self,
        text: str,
        lang: LangID,
        alphabet: Alphabet = "ipa",
    ) -> PhoneSeq:
        phone = []
        for t in self._translators:
            phone = t.translate(text, lang, alphabet=alphabet)
            if phone:
                break
        return phone


class LexiconGraphemeToPhonemeTranslator(EmbeddedPhonemeTranslatorBase):
    _lookup_lexicon: LexiconBase
    _language_code: LangID
    _alphabet: Alphabet
    _version_hash: str

    def __init__(
        self,
        lexicon: Path,
        language_code: LangID,
        alphabet: Alphabet,
    ):
        self._lookup_lexicon = SimpleInMemoryLexicon(lexicon, alphabet)
        self._language_code = language_code
        # TODO(rkjaran): By default LexiconBase.get(...) returns IPA, change this once
        #   we add a parameter for the alphabet to .get()
        self._alphabet = "ipa"

        self._version_hash = hash_from_impl(self.__class__, lexicon.read_bytes())

    @property
    def version_hash(self) -> str:
        return self._version_hash

    def _translate(
        self,
        w: str,
        lang: LangID,
        alphabet: Alphabet = "ipa",
    ):
        phones: PhoneSeq = []
        w_lower = w.lower()
        lexicon = self._lookup_lexicon
        if lexicon:
            phones = lexicon.get(w, [])
            if not phones:
                phones = lexicon.get(w_lower, [])
        # TODO(rkjaran): By default LexiconBase.get(...) returns IPA, change this once
        #   we add a parameter for the alphabet to .get()
        if alphabet != "ipa":
            phones = convert_ipa_to_xsampa(phones)
            if alphabet == "x-sampa+syll+stress":
                phones = convert_xsampa_to_xsampa_with_stress(phones, w)

        return phones


class IceG2PTranslator(EmbeddedPhonemeTranslatorBase):
    _transcriber: ice_g2p.transcriber.Transcriber
    _version_hash: Optional[str] = None

    def __init__(self):
        self._transcriber = ice_g2p.transcriber.Transcriber(
            use_dict=True, syllab_symbol=".", stress_label=True
        )

    @property
    def version_hash(self) -> str:
        if not self._version_hash:
            # We're relying on implementation details of ice-g2p here...
            self._version_hash = hash_from_impl(
                self.__class__,
                Path(self._transcriber.g2p.model_path)
                .joinpath(self._transcriber.g2p.model_file)
                .read_bytes()
                + b"\n".join(
                    sorted(
                        f"{k} {v}".encode()
                        for k, v in (self._transcriber.g2p.custom_dict or {}).items()
                    )
                )
                + b"\n".join(
                    sorted(
                        f"{k} {v}".encode()
                        for k, v in (self._transcriber.g2p.pron_dict or {}).items()
                    )
                ),
            )
        return self._version_hash

    def _translate(
        self,
        text: str,
        lang: LangID,
        alphabet: Alphabet = "ipa",
    ) -> PhoneSeq:
        punctuation = re.sub(r"[{}\[\]]", "", string.punctuation)
        text = re.sub(r"([{}])".format(punctuation), "", text)

        if text.strip() == "":
            return []

        if alphabet != "x-sampa+syll+stress":
            syllab_symbol = self._transcriber.syllab_symbol
            self._transcriber.syllab_symbol = ""
            out = self._transcriber.transcribe(text.lower())
            self._transcriber.syllab_symbol = syllab_symbol
        else:
            out = self._transcriber.transcribe(text.lower())

        phone_seq = out.split()
        if alphabet == "ipa":
            return convert_xsampa_to_ipa(phone_seq)

        return phone_seq
