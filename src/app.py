import io
import contextlib
import uuid
import subprocess
from flask import Flask, jsonify, Response, send_from_directory
from flask_cors import CORS
from webargs import fields
from flask_apispec import use_kwargs, marshal_with, FlaskApiSpec, doc
from flask_caching import Cache
from marshmallow import validate, Schema
from apispec import APISpec, BasePlugin
from apispec.ext.marshmallow import MarshmallowPlugin
from config import EnvvarConfig
from werkzeug.middleware.proxy_fix import ProxyFix


app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["APISPEC_SWAGGER_URL"] = "/v0/swagger.json"
app.config["APISPEC_SWAGGER_UI_URL"] = "/"
app.config.from_object(EnvvarConfig)

# Fix access to client remote_addr when running behind proxy
setattr(app, "wsgi_app", ProxyFix(app.wsgi_app))

app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.config["CACHE_NO_NULL_WARNING"] = True

cors = CORS(app)
cache = Cache(app)

# Give everyone access to current_app
app.app_context().push()

from aws import Polly
from lib.fastspeech.align_phonemes import Aligner
from fastspeech import FastSpeech2Synthesizer, XSAMPA_IPA_MAP


g_polly = Polly()


class DisableOptionsOperationPlugin(BasePlugin):
    # See https://github.com/jmcarp/flask-apispec/issues/155#issuecomment-562542538
    def operation_helper(self, operations, **kwargs):
        # flask-apispec auto generates an options operation, which cannot handled by apispec.
        # apispec.exceptions.DuplicateParameterError: Duplicate parameter with name body and location body
        # => remove
        operations.pop("options", None)


app.config["APISPEC_SPEC"] = APISpec(
    title="TTS",
    version="v0",
    host=app.config["HOST"],
    openapi_version="2.0",
    plugins=[MarshmallowPlugin(), DisableOptionsOperationPlugin()],
    tags=[{"name": "speech", "description": "Synthesize speech from input text"},],
)
docs = FlaskApiSpec(app)


g_fastspeech = FastSpeech2Synthesizer()


SUPPORTED_VOICE_IDS = ["Dora", "Karl", "Other", "Joanna"]


class SynthesizeSpeechRequest(Schema):
    Engine = fields.Str(
        required=True,
        description="Specify which engine to use",
        validate=validate.OneOf(["standard", "neural"]),
    )
    LanguageCode = fields.Str(required=False, example="is-IS")
    LexiconNames = fields.List(
        fields.Str(),
        required=False,
        description=(
            "List of one or more pronunciation lexicon names you want the "
            + "service to apply during synthesis. Lexicons are applied only if the "
            + "language of the lexicon is the same as the language of the voice. "
            + "For information about storing lexicons, see PutLexicon. "
            + "UNIMPLEMENTED"
        ),
        example=[],
    )
    OutputFormat = fields.Str(
        required=True,
        description=(
            " The format in which the returned output will be encoded. "
            + "For audio stream, this will be mp3, ogg_vorbis, or pcm. "
            + "For speech marks, this will be json. "
        ),
        validate=validate.OneOf(["json", "pcm", "mp3", "ogg_vorbis"]),
        example="pcm",
    )
    SampleRate = fields.Str(
        required=True,
        description="The audio frequency specified in Hz.",
        validate=validate.OneOf(["8000", "16000", "22050", "24000"]),
        example="22050",
    )
    SpeechMarkTypes = fields.List(
        fields.Str(validate=validate.OneOf(["sentence", "ssml", "viseme", "word"])),
        required=False,
        description="The type of speech marks returned for the input text",
        example=[],
    )
    Text = fields.Str(
        required=True,
        description="Input text to synthesize.",
        example="Halló! Ég er gervimaður.",
    )
    TextType = fields.Str(
        required=False,
        description=(
            "Specifies whether the input text is plain text or SSML. "
            + "The default value is plain text. For more information, see Using SSML. "
        ),
        validate=validate.OneOf(["text",]),  # "ssml"
    )
    VoiceId = fields.Str(
        required=True,
        description="Voice ID to use for the synthesis",
        validate=validate.OneOf(SUPPORTED_VOICE_IDS),
        example="Other",
    )


@app.route("/v0/speech", methods=["POST"])
@use_kwargs(SynthesizeSpeechRequest)
@doc(
    description="Synthesize speech",
    tags=["speech"],
    produces=["audio/mpeg", "audio/ogg", "application/x-json-stream", "audio/x-wav"],
)
@cache.memoize()
def route_synthesize_speech(**kwargs):
    app.logger.info("Got request: %s", kwargs)

    output_content_type = "application/x-json-stream"
    if kwargs["OutputFormat"] == "mp3":
        output_content_type = "audio/mpeg"
    elif kwargs["OutputFormat"] == "ogg_vorbis":
        output_content_type = "audio/ogg"
    elif kwargs["OutputFormat"] == "pcm":
        output_content_type = "audio/x-wav"

    if kwargs["VoiceId"] in ("Dora", "Karl", "Joanna"):
        polly_resp = g_polly.synthesize_speech(**kwargs)
        try:
            if "AudioStream" in polly_resp:
                with contextlib.closing(polly_resp["AudioStream"]) as stream:
                    content = stream.read()
                return Response(content, content_type=output_content_type)
            else:
                return {"error": 1}, 400
        except:
            return {"error": 1}, 400
    else:
        if kwargs["OutputFormat"] != "pcm" or kwargs["TextType"] != "text":
            return {"error": 1, "message": "Unsupported arguments"}, 400
        content = io.BytesIO()
        g_fastspeech.synthesize(kwargs["Text"], content)
        return Response(content, content_type=output_content_type)


docs.register(route_synthesize_speech)

if __name__ == "__main__":
    app.run()
