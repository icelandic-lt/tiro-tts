from abc import ABC, abstractmethod
from typing import (
    Union,
    TextIO,
    BinaryIO,
    NewType,
    Literal,
    Optional,
    Iterator,
    List,
    Tuple,
)


class OutputFormat:
    output_format: Literal["mp3", "pcm"]
    supported_sample_rates: List[str]

    def __init__(self, output_format, supported_sample_rates):
        self.output_format = output_format
        self.supported_sample_rates = supported_sample_rates

    def __eq__(self, other) -> bool:
        if isinstance(other, tuple):
            return (
                self.output_format == other[0]
                and other[1] in self.supported_sample_rates
            )
        return other.output_format == self.output_format

    def __repr__(self):
        return "<OutputFormat('{}', {})>".format(
            self.output_format, self.supported_sample_rates
        )

    @property
    def content_type(self):
        if self.output_format == "mp3":
            return "audio/mpeg"
        elif self.output_format == "ogg_vorbis":
            return "audio/ogg"
        elif self.output_format == "pcm":
            return "audio/x-wav"


class VoiceProperties:
    voice_id: str
    name: Optional[str]
    gender: Optional[Literal["Female", "Male"]]
    language_code: Optional[str]
    language_name: Optional[str]
    supported_output_formats: List[OutputFormat]

    def __init__(
        self,
        voice_id: str,
        name: Optional[str] = None,
        gender: Optional[Literal["Female", "Male"]] = None,
        language_code: Optional[str] = None,
        language_name: Optional[str] = None,
        supported_output_formats=[],
    ):
        self.voice_id = voice_id
        self.name = name
        self.gender = gender
        self.language_code = language_code
        self.language_name = language_name
        self.supported_output_formats = supported_output_formats


class VoiceBase(ABC):
    @abstractmethod
    def synthesize(self, text: str, **kwargs) -> bytes:
        return NotImplemented

    @abstractmethod
    def synthesize_from_ssml(self, ssml: Union[str, TextIO], **kwargs) -> bytes:
        return NotImplemented

    @property
    @abstractmethod
    def properties(self) -> VoiceProperties:
        return NotImplemented