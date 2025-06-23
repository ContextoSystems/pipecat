#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#
import base64
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from loguru import logger
from pydantic import BaseModel, Field

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InputImageRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMMessagesFrame,
    LLMSetToolsFrame,
    LLMTextFrame,
    LLMUpdateSettingsFrame,
    StartFrame,
    StartInterruptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserImageRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import LLMTokenUsage
from pipecat.processors.aggregators.llm_response import (
    LLMAssistantAggregatorParams,
    LLMUserAggregatorParams,
)
from pipecat.processors.aggregators.openai_llm_context import (
    OpenAILLMContext,
    OpenAILLMContextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.google import GoogleLLMContext
from pipecat.services.google.llm_vertex import GoogleVertexLLMService
from pipecat.services.llm_service import FunctionCallFromLLM, LLMService
from pipecat.services.openai.llm import (
    OpenAIAssistantContextAggregator,
    OpenAIUserContextAggregator,
)
from pipecat.transcriptions.language import Language
from pipecat.utils.string import match_endofsentence
from pipecat.utils.time import time_now_iso8601
from pipecat.utils.tracing.service_decorators import traced_gemini_live, traced_stt, traced_tts

from . import events

try:
    from google import genai
    from google.genai.types import Blob, Content, LiveClientContent, LiveConnectConfig, Modality, Part
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error("In order to use Google AI, you need to `pip install pipecat-ai[google]`.")
    raise Exception(f"Missing module: {e}")


def language_to_gemini_language(language: Language) -> Optional[str]:
    """Maps a Language enum value to a Gemini Live supported language code.

    Source:
    https://ai.google.dev/api/generate-content#MediaResolution

    Returns None if the language is not supported by Gemini Live.
    """
    language_map = {
        # Arabic
        Language.AR: "ar-XA",
        # Bengali
        Language.BN_IN: "bn-IN",
        # Chinese (Mandarin)
        Language.CMN: "cmn-CN",
        Language.CMN_CN: "cmn-CN",
        Language.ZH: "cmn-CN",  # Map general Chinese to Mandarin for Gemini
        Language.ZH_CN: "cmn-CN",  # Map Simplified Chinese to Mandarin for Gemini
        # German
        Language.DE: "de-DE",
        Language.DE_DE: "de-DE",
        # English
        Language.EN: "en-US",  # Default to US English (though not explicitly listed in supported codes)
        Language.EN_US: "en-US",
        Language.EN_AU: "en-AU",
        Language.EN_GB: "en-GB",
        Language.EN_IN: "en-IN",
        # Spanish
        Language.ES: "es-ES",  # Default to Spain Spanish
        Language.ES_ES: "es-ES",
        Language.ES_US: "es-US",
        # French
        Language.FR: "fr-FR",  # Default to France French
        Language.FR_FR: "fr-FR",
        Language.FR_CA: "fr-CA",
        # Gujarati
        Language.GU: "gu-IN",
        Language.GU_IN: "gu-IN",
        # Hindi
        Language.HI: "hi-IN",
        Language.HI_IN: "hi-IN",
        # Indonesian
        Language.ID: "id-ID",
        Language.ID_ID: "id-ID",
        # Italian
        Language.IT: "it-IT",
        Language.IT_IT: "it-IT",
        # Japanese
        Language.JA: "ja-JP",
        Language.JA_JP: "ja-JP",
        # Kannada
        Language.KN: "kn-IN",
        Language.KN_IN: "kn-IN",
        # Korean
        Language.KO: "ko-KR",
        Language.KO_KR: "ko-KR",
        # Malayalam
        Language.ML: "ml-IN",
        Language.ML_IN: "ml-IN",
        # Marathi
        Language.MR: "mr-IN",
        Language.MR_IN: "mr-IN",
        # Dutch
        Language.NL: "nl-NL",
        Language.NL_NL: "nl-NL",
        # Polish
        Language.PL: "pl-PL",
        Language.PL_PL: "pl-PL",
        # Portuguese (Brazil)
        Language.PT_BR: "pt-BR",
        # Russian
        Language.RU: "ru-RU",
        Language.RU_RU: "ru-RU",
        # Tamil
        Language.TA: "ta-IN",
        Language.TA_IN: "ta-IN",
        # Telugu
        Language.TE: "te-IN",
        Language.TE_IN: "te-IN",
        # Thai
        Language.TH: "th-TH",
        Language.TH_TH: "th-TH",
        # Turkish
        Language.TR: "tr-TR",
        Language.TR_TR: "tr-TR",
        # Vietnamese
        Language.VI: "vi-VN",
        Language.VI_VN: "vi-VN",
    }
    return language_map.get(language)


class GeminiMultimodalLiveContext(OpenAILLMContext):
    @staticmethod
    def upgrade(obj: OpenAILLMContext) -> "GeminiMultimodalLiveContext":
        if isinstance(obj, OpenAILLMContext) and not isinstance(obj, GeminiMultimodalLiveContext):
            logger.debug(f"Upgrading to Gemini Multimodal Live Context: {obj}")
            obj.__class__ = GeminiMultimodalLiveContext
            obj._restructure_from_openai_messages()
        return obj

    def set_messages(self, messages: List):
        self._messages[:] = messages
        self._restructure_from_openai_messages()

    def add_messages(self, messages: List):
        # Convert each message individually
        converted_messages = []
        for msg in messages:
            if isinstance(msg, Content):
                # Already in Gemini format
                converted_messages.append(msg)
            else:
                # Convert from standard format to Gemini format
                converted = self.from_standard_message(msg)
                if converted is not None:
                    converted_messages.append(converted)

        # Add the converted messages to our existing messages
        # self._messages.append(converted_messages)
        self._messages.extend(converted_messages)
        self._restructure_from_openai_messages()

    def from_standard_message(self, message):
        """Convert standard format message to Google Content object.

        Handles conversion of text, images, and function calls to Google's format.

        Args:
            message: Message in standard format:
                {
                    "role": "user/assistant/system/tool",
                    "content": str | [{"type": "text/image_url", ...}] | None,
                    "tool_calls": [{"function": {"name": str, "arguments": str}}]
                }

        Returns:
            Content object with:
                - role: "user" or "model" (converted from "assistant")
                - parts: List[Part] containing text, inline_data, or function calls
            Returns None for system messages.
        """
        role = message["role"]
        content = message.get("content", [])
        if role == "system":
            # don't think this is needed anymore
            self.system_message = content
        elif role == "assistant":
            role = "model"

        parts = []
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                parts.append(
                    Part(
                        function_call=FunctionCall(
                            name=tc["function"]["name"],
                            args=json.loads(tc["function"]["arguments"]),
                        )
                    )
                )
        elif role == "tool":
            role = "model"
            parts.append(
                Part(
                    function_response=FunctionResponse(
                        name="tool_call_result",  # seems to work to hard-code the same name every time
                        response=json.loads(message["content"]),
                    )
                )
            )
        elif isinstance(content, str):
            parts.append(Part(text=content))
        elif isinstance(content, list):
            for c in content:
                if c["type"] == "text":
                    parts.append(Part(text=c["text"]))
                elif c["type"] == "image_url":
                    parts.append(
                        Part(
                            inline_data=Blob(
                                mime_type="image/jpeg",
                                data=base64.b64decode(c["image_url"]["url"].split(",")[1]),
                            )
                        )
                    )

        message = Content(role=role, parts=parts)
        return message

    def to_standard_messages(self, obj) -> list:
        """Convert Google Content object to standard structured format.

        Handles text, images, and function calls from Google's Content/Part objects.

        Args:
            obj: Google Content object with:
                - role: "model" (converted to "assistant") or "user"
                - parts: List[Part] containing text, inline_data, or function calls

        Returns:
            List of messages in standard format:
            [
                {
                    "role": "user/assistant/tool",
                    "content": [
                        {"type": "text", "text": str} |
                        {"type": "image_url", "image_url": {"url": str}}
                    ]
                }
            ]
        """
        msg = {"role": obj.role, "content": []}
        if msg["role"] == "model":
            msg["role"] = "assistant"

        for part in obj.parts:
            if part.text:
                msg["content"].append({"type": "text", "text": part.text})
            elif part.inline_data:
                encoded = base64.b64encode(part.inline_data.data).decode("utf-8")
                msg["content"].append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{part.inline_data.mime_type};base64,{encoded}"},
                    }
                )
            elif part.function_call:
                args = part.function_call.args if hasattr(part.function_call, "args") else {}
                msg["tool_calls"] = [
                    {
                        "id": part.function_call.name,
                        "type": "function",
                        "function": {
                            "name": part.function_call.name,
                            "arguments": json.dumps(args),
                        },
                    }
                ]

            elif part.function_response:
                msg["role"] = "tool"
                resp = (
                    part.function_response.response
                    if hasattr(part.function_response, "response")
                    else {}
                )
                msg["tool_call_id"] = part.function_response.name
                msg["content"] = json.dumps(resp)

        # there might be no content parts for tool_calls messages
        if not msg["content"]:
            del msg["content"]
        return [msg]

    def _restructure_from_openai_messages(self):
        """Restructures messages to ensure proper Google format and message ordering.

        This method handles conversion of OpenAI-formatted messages to Google format,
        with special handling for function calls, function responses, and system messages.
        System messages are added back to the context as user messages when needed.

        The final message order is preserved as:
        1. Function calls (from model)
        2. Function responses (from user)
        3. Text messages (converted from system messages)

        Note:
            System messages are only added back when there are no regular text
            messages in the context, ensuring proper conversation continuity
            after function calls.
        """
        self.system_message = None
        converted_messages = []

        # Process each message, preserving Google-formatted messages and converting others
        for message in self._messages:
            if isinstance(message, Content):
                # Keep existing Google-formatted messages (e.g., function calls/responses)
                converted_messages.append(message)
                continue

            # Convert OpenAI format to Google format, system messages return None
            converted = self.from_standard_message(message)
            if converted is not None:
                converted_messages.append(converted)

        # Update message list
        self._messages[:] = converted_messages

        ## this is broken... ?
        # # Check if we only have function-related messages (no regular text)
        # has_regular_messages = any(
        #     len(msg.parts) == 1
        #     and not getattr(msg.parts[0], "text", None)
        #     and getattr(msg.parts[0], "function_call", None)
        #     and getattr(msg.parts[0], "function_response", None)
        #     for msg in self._messages
        # )
        # # Add system message back as a user message if we only have function messages
        # if self.system_message and not has_regular_messages:
        #     self._messages.append(Content(role="user", parts=[Part(text=self.system_message)]))

        # Remove any empty messages
        self._messages = [m for m in self._messages if m.parts]

    def extract_system_instructions(self):
        system_instruction = ""
        for item in self.messages:
            if item.get("role") == "system":
                content = item.get("content", "")
                if content:
                    if system_instruction and not system_instruction.endswith("\n"):
                        system_instruction += "\n"
                    system_instruction += str(content)
        return system_instruction

    def get_messages_for_initializing_history(self):
        messages = []
        for item in self.messages:
            role = item.get("role")

            if role == "system":
                continue

            elif role == "assistant":
                role = "model"

            content = item.get("content")
            parts = []
            if isinstance(content, str):
                parts = [{"text": content}]
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        parts.append({"text": part.get("text")})
                    else:
                        logger.warning(f"Unsupported content type: {str(part)[:80]}")
            else:
                logger.warning(f"Unsupported content type: {str(content)[:80]}")
            messages.append({"role": role, "parts": parts})
        return messages


class GeminiMultimodalLiveUserContextAggregator(OpenAIUserContextAggregator):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        # kind of a hack just to pass the LLMMessagesAppendFrame through, but it's fine for now
        if isinstance(frame, LLMMessagesAppendFrame):
            await self.push_frame(frame, direction)


class GeminiMultimodalLiveAssistantContextAggregator(OpenAIAssistantContextAggregator):
    # The LLMAssistantContextAggregator uses TextFrames to aggregate the LLM output,
    # but the GeminiMultimodalLiveAssistantContextAggregator pushes LLMTextFrames and TTSTextFrames. We
    # need to override this proces_frame for LLMTextFrame, so that only the TTSTextFrames
    # are process. This ensures that the context gets only one set of messages.
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if not isinstance(frame, LLMTextFrame):
            await super().process_frame(frame, direction)

    async def handle_user_image_frame(self, frame: UserImageRawFrame):
        # We don't want to store any images in the context. Revisit this later
        # when the API evolves.
        pass


@dataclass
class GeminiMultimodalLiveContextAggregatorPair:
    _user: GeminiMultimodalLiveUserContextAggregator
    _assistant: GeminiMultimodalLiveAssistantContextAggregator

    def user(self) -> GeminiMultimodalLiveUserContextAggregator:
        return self._user

    def assistant(self) -> GeminiMultimodalLiveAssistantContextAggregator:
        return self._assistant


class GeminiMultimodalModalities(Enum):
    TEXT = "TEXT"
    AUDIO = "AUDIO"


class GeminiMediaResolution(str, Enum):
    """Media resolution options for Gemini Multimodal Live."""

    UNSPECIFIED = "MEDIA_RESOLUTION_UNSPECIFIED"  # Use default
    LOW = "MEDIA_RESOLUTION_LOW"  # 64 tokens
    MEDIUM = "MEDIA_RESOLUTION_MEDIUM"  # 256 tokens
    HIGH = "MEDIA_RESOLUTION_HIGH"  # Zoomed reframing with 256 tokens


class GeminiVADParams(BaseModel):
    """Voice Activity Detection parameters."""

    disabled: Optional[bool] = Field(default=None)
    start_sensitivity: Optional[events.StartSensitivity] = Field(default=None)
    end_sensitivity: Optional[events.EndSensitivity] = Field(default=None)
    prefix_padding_ms: Optional[int] = Field(default=None)
    silence_duration_ms: Optional[int] = Field(default=None)


class ContextWindowCompressionParams(BaseModel):
    """Parameters for context window compression."""

    enabled: bool = Field(default=False)
    trigger_tokens: Optional[int] = Field(
        default=None
    )  # None = use default (80% of context window)


class InputParams(BaseModel):
    frequency_penalty: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=4096, ge=1)
    presence_penalty: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_k: Optional[int] = Field(default=None, ge=0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    modalities: Optional[GeminiMultimodalModalities] = Field(
        default=GeminiMultimodalModalities.AUDIO
    )
    language: Optional[Language] = Field(default=Language.EN_US)
    media_resolution: Optional[GeminiMediaResolution] = Field(
        default=GeminiMediaResolution.UNSPECIFIED
    )
    vad: Optional[GeminiVADParams] = Field(default=None)
    context_window_compression: Optional[ContextWindowCompressionParams] = Field(default=None)
    extra: Optional[Dict[str, Any]] = Field(default_factory=dict)


## TODO: GeminiMultimodalLiveLLMService
class GeminiMultimodalLiveLLMService(LLMService):
    pass


class VertexAIGeminiMultimodalLiveLLMService(LLMService):
    """Provides access to Google's Gemini Multimodal Live API via Vertex AI.

    This service enables real-time conversations with Gemini, supporting both
    text and audio modalities. It handles voice transcription, streaming audio
    responses, and tool usage.

    Args:
        model (str, optional): Model identifier to use. Defaults to
            "models/gemini-2.0-flash-live-001".
        voice_id (str, optional): TTS voice identifier. Defaults to "Charon".
        start_audio_paused (bool, optional): Whether to start with audio input paused.
            Defaults to False.
        start_video_paused (bool, optional): Whether to start with video input paused.
            Defaults to False.
        system_instruction (str, optional): System prompt for the model. Defaults to None.
        tools (Union[List[dict], ToolsSchema], optional): Tools/functions available to the model.
            Defaults to None.
        params (InputParams, optional): Configuration parameters for the model.
            Defaults to InputParams().
        inference_on_context_initialization (bool, optional): Whether to generate a response
            when context is first set. Defaults to True.
    """

    # Overriding the default adapter to use the Gemini one.
    adapter_class = GeminiLLMAdapter

    def __init__(
        self,
        *,
        credentials: Optional[str] = None,
        credentials_path: Optional[str] = None,
        vertex_params: Optional[GoogleVertexLLMService.InputParams] = None,
        model: str = "gemini-2.0-flash-live-preview-04-09",
        voice_id: str = "Charon",
        start_audio_paused: bool = False,
        start_video_paused: bool = False,
        system_instruction: Optional[str] = None,
        tools: Optional[Union[List[dict], ToolsSchema]] = None,
        params: Optional[InputParams] = None,
        inference_on_context_initialization: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)

        params = params or InputParams()

        self._last_sent_time = 0
        self._client = self._create_client(vertex_params)
        self.set_model_name(model)
        self._voice_id = voice_id
        self._language_code = params.language

        self._system_instruction = system_instruction
        self._tools = tools
        self._inference_on_context_initialization = inference_on_context_initialization
        self._needs_turn_complete_message = False

        self._audio_input_paused = start_audio_paused
        self._video_input_paused = start_video_paused
        self._context = None
        self._websocket = None
        self._receive_task = None

        self._disconnecting = False
        self._api_session_ready = False
        self._run_llm_when_api_session_ready = False

        self._user_is_speaking = False
        self._bot_is_speaking = False
        self._user_audio_buffer = bytearray()
        self._user_transcription_buffer = ""
        self._last_transcription_sent = ""
        self._bot_audio_buffer = bytearray()
        self._bot_text_buffer = ""
        self._llm_output_buffer = ""

        self._sample_rate = 24000

        self._language = params.language
        self._language_code = (
            language_to_gemini_language(params.language) if params.language else "en-US"
        )
        self._vad_params = params.vad

        self._is_text_modality = GeminiMultimodalModalities.TEXT == params.modalities
        self._is_audio_modality = GeminiMultimodalModalities.AUDIO == params.modalities

        self._settings = {
            "frequency_penalty": params.frequency_penalty,
            "max_tokens": params.max_tokens,
            "presence_penalty": params.presence_penalty,
            "temperature": params.temperature,
            "top_k": params.top_k,
            "top_p": params.top_p,
            "modalities": params.modalities,
            "language": self._language_code,
            "media_resolution": params.media_resolution,
            "vad": params.vad,
            "context_window_compression": params.context_window_compression.model_dump()
            if params.context_window_compression
            else {},
            "extra": params.extra if isinstance(params.extra, dict) else {},
        }

        # self._config = LiveConnectConfig(
        #     response_modalities=[params.modalities], ["AUDIO"],  # We want spoken responses
        #         speech_config=SpeechConfig(
        #         voice_config=VoiceConfig(
        #             prebuilt_voice_config=PrebuiltVoiceConfig(
        #             voice_name=self._voice_id,
        #             )
        #         ),
        #     ),
        # )

    def can_generate_metrics(self) -> bool:
        return True

    def _create_client(self, vertex_params: GoogleVertexLLMService.InputParams):
        return genai.Client(
            vertexai=True,
            project=vertex_params.project_id,
            # "gemini-2.0-flash-live-preview-04-09" is only available in us-central1
            location="us-central1", # location=vertex_params.location,
        )

    def set_audio_input_paused(self, paused: bool):
        self._audio_input_paused = paused

    def set_video_input_paused(self, paused: bool):
        self._video_input_paused = paused

    def set_model_modalities(self, modalities: GeminiMultimodalModalities):
        self._settings["modalities"] = modalities

    def set_language(self, language: Language):
        """Set the language for generation."""
        self._language = language
        self._language_code = language_to_gemini_language(language) or "en-US"
        self._settings["language"] = self._language_code
        logger.info(f"Set Gemini language to: {self._language_code}")

    async def set_context(self, context: OpenAILLMContext):
        """Set the context explicitly from outside the pipeline.

        This is useful when initializing a conversation because in server-side VAD mode we might not have a
        way to trigger the pipeline. This sends the history to the server. The `inference_on_context_initialization`
        flag controls whether to set the turnComplete flag when we do this. Without that flag, the model will
        not respond. This is often what we want when setting the context at the beginning of a conversation.
        """
        if self._context:
            logger.error(
                "Context already set. Can only set up Gemini Multimodal Live context once."
            )
            return
        self._context = GeminiMultimodalLiveContext.upgrade(context)
        await self._create_initial_response()

    #
    # standard AIService frame handling
    #

    async def start(self, frame: StartFrame):
        await super().start(frame)
        # await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def _disconnect(self):
        self._disconnecting = True
        self._api_session_ready = False
        await self.stop_all_metrics()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        self._disconnecting = False

    #
    # speech and interruption handling
    #

    async def _handle_interruption(self):
        self._bot_is_speaking = False
        await self.push_frame(TTSStoppedFrame())
        await self.push_frame(LLMFullResponseEndFrame())

    async def _handle_user_started_speaking(self, frame):
        self._user_is_speaking = True
        pass

    async def _handle_user_stopped_speaking(self, frame):
        self._user_is_speaking = False
        self._user_audio_buffer = bytearray()
        await self.start_ttfb_metrics()
        if self._needs_turn_complete_message:
            self._needs_turn_complete_message = False
            evt = events.ClientContentMessage.model_validate(
                {"clientContent": {"turnComplete": True}}
            )
            await self.send_client_event(evt)

    async def send_client_event(self, evt):
        pass
        # print(f"_____gemini.py * send_client_event evt: {evt}")

    #
    # frame processing
    #
    # StartFrame, StopFrame, CancelFrame implemented in base class
    #
    async def _process_context(self, context: OpenAILLMContext):
        print(f"_____gemini.py * : _process_context")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            print(f"___#### # # # # #######__gemini.py * TranscriptionFrame frame.text: {frame.text}")
            print(f"_____                                    gemini.py * self._context: {self._context}")
            self._receive_task = self.create_task(self._receive_task_handler(self._context))
            await self.push_frame(frame, direction)

        elif isinstance(frame, OpenAILLMContextFrame):
            print(f"_____gemini.py * Vertex AI OpenAILLMContextFrame: {frame.context}")
            context: GeminiMultimodalLiveContext = GeminiMultimodalLiveContext.upgrade(
                frame.context
            )

            # For now, we'll only trigger inference here when either:
            #   1. We have not seen a context frame before
            #   2. The last message is a tool call result
            if not self._context:
                self._context = context
                if not self._receive_task:
                    self._receive_task = self.create_task(self._receive_task_handler(self._context))
                if frame.context.tools:
                    self._tools = frame.context.tools
                await self._create_initial_response()
            elif context.messages and context.messages[-1].get("role") == "tool":
                # Support just one tool call per context frame for now
                tool_result_message = context.messages[-1]
                await self._tool_result(tool_result_message)
            elif context.messages and context.messages[-1].get("role") == "model":
                self._context = context
                # await self.push_frame(frame, direction)

        elif isinstance(frame, LLMMessagesFrame):
            if frame.messages and frame.messages[-1].get("role") == "system":
                self._context.add_messages(frame.messages)
                self._receive_task = self.create_task(self._receive_task_handler(self._context))
            await self.push_frame(frame, direction)

        ### audio parameter is not supported in Vertex AI.
        ### maybe use 'media' ?
        elif isinstance(frame, InputAudioRawFrame):
            # await self._send_user_audio(frame)
            await self.push_frame(frame, direction)

        elif isinstance(frame, InputImageRawFrame):
            await self._send_user_video(frame)
            await self.push_frame(frame, direction)
        elif isinstance(frame, StartInterruptionFrame):
            await self._handle_interruption()
            await self.push_frame(frame, direction)

        elif isinstance(frame, UserStartedSpeakingFrame):
            await self._handle_user_started_speaking(frame)
            await self.push_frame(frame, direction)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self._handle_user_stopped_speaking(frame)
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStartedSpeakingFrame):
            # Ignore this frame. Use the serverContent API message instead
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # ignore this frame. Use the serverContent.turnComplete API message
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMMessagesAppendFrame):
            await self._create_single_response(frame.messages)
        elif isinstance(frame, LLMUpdateSettingsFrame):
            await self._update_settings(frame.settings)
        elif isinstance(frame, LLMSetToolsFrame):
            await self._update_settings()
        else:
            await self.push_frame(frame, direction)

    # https://github.com/google-gemini/cookbook/issues/781
    # https://github.com/google-gemini/cookbook/blob/cb04a04359ac7937c4b22e8b4c381451ba1e5d93/quickstarts/Get_started_LiveAPI.py

    async def _receive_task_handler(self, context):
        print(f"_____gemini.py * _receive_task_handler::  ::_receive_task_handler")
        async with self._client.aio.live.connect(
            model=self._model_name,
            # config=self._config,
            config=LiveConnectConfig(response_modalities=[self._settings["modalities"]]),
        ) as session:

            try:
                if self._is_text_modality:
                    print(f"_____gemini.py * GeminiMultimodalModalities.TEXT - send_client_content")
                    await session.send_client_content(turns=self._context.messages)
                else:
                    # audio modality not supported in vertex yet :thinking:
                    pass

                async for message in session.receive():
                    if message.text:
                        await self.push_frame(LLMTextFrame(message.text))

                    elif message.server_content:
                        if message.server_content.turn_complete:
                            # if self._is_text_modality:
                            # if self._is_audio_modality:
                            #     await self.push_frame(TTSStoppedFrame())
                            await self.push_frame(LLMFullResponseEndFrame())
                            self._bot_is_speaking = False

                    else:
                        pass
            except Exception as e:
                print(f"_____gemini.py * _receive_task_handler error: {e}")

    #
    #
    #

    ### audio parameter is not supported in Vertex AI.
    async def _send_user_audio(self, frame):
        print(f"_____gemini.py * _send_user_audio::::")
        # return
        # if self._audio_input_paused:
        #     return
        self._user_audio_buffer.extend(frame.audio)
        
        async with self._client.aio.live.connect(
            model=self._model_name,
            # config=self._config,
            config=LiveConnectConfig(response_modalities=[GeminiMultimodalModalities.AUDIO]),
        ) as session:
            self._is_text_modality = GeminiMultimodalModalities.TEXT == self._settings["modalities"]
            self._is_audio_modality = GeminiMultimodalModalities.AUDIO == self._settings["modalities"]

            data = base64.b64encode(self._user_audio_buffer).decode("utf-8")
            try:
                await session.send_realtime_input(
                    media=Blob(data=data, mime_type=f"audio/pcm;rate={self._sample_rate}")
                    # audio=Blob(data=frame.audio, mime_type="audio/pcm;rate=16000")
                )

                async for message in session.receive():
                    print(f"_____gemini.py * _send_user_audio message: {message}")
                    if message.text:
                        await self.push_frame(LLMTextFrame(message.text))

                    elif message.server_content:
                        if message.server_content.turn_complete:
                            if audio_modality:
                                await self.push_frame(TTSStoppedFrame())
                            await self.push_frame(LLMFullResponseEndFrame())
                            self._bot_is_speaking = False

                    # TODO/WIP audio
                    # https://cloud.google.com/vertex-ai/generative-ai/docs/live-api#:~:text=Vertex%20AI%20Studio.-,Context%20window,inputs%2C%20model%20outputs%2C%20etc.
                    # elif message.media:#?
                    elif message.audio:
                        print(f"_____gemini.py * message.audio: {message.audio}")

                        audio = base64.b64decode(message.audio)
                        if not audio:
                            return

                        if not self._bot_is_speaking:
                            self._bot_is_speaking = True
                            await self.push_frame(TTSStartedFrame())
                            await self.push_frame(LLMFullResponseStartFrame())

                        self._bot_audio_buffer.extend(audio)
                        frame = TTSAudioRawFrame(
                            audio=audio,
                            sample_rate=self._sample_rate,
                            num_channels=1,
                        )
                        await self.push_frame(frame)

                    else:
                        pass
            except Exception as e:
                print(f"_____gemini.py * _send_user_audio error: {e}")
        print(f"_____gemini.py * : _send_user_audio end end end end end end end _____")

        # # Manage a buffer of audio to use for transcription
        # audio = frame.audio
        # if self._user_is_speaking:
        #     self._user_audio_buffer.extend(audio)
        # else:
        #     # Keep 1/2 second of audio in the buffer even when not speaking.
        #     self._user_audio_buffer.extend(audio)
        #     length = int((frame.sample_rate * frame.num_channels * 2) * 0.5)
        #     self._user_audio_buffer = self._user_audio_buffer[-length:]

    async def _send_user_video(self, frame):
        if self._video_input_paused:
            return

        now = time.time()
        if now - self._last_sent_time < 1:
            return  # Ignore if less than 1 second has passed

        self._last_sent_time = now  # Update last sent time
        logger.debug(f"Sending video frame to Gemini: {frame}")
        evt = events.VideoInputMessage.from_image_frame(frame)
        await self.send_client_event(evt)

    async def _create_initial_response(self):
        if not self._api_session_ready:
            self._run_llm_when_api_session_ready = True
            return

        messages = self._context.get_messages_for_initializing_history()
        if not messages:
            return

        logger.debug(f"Creating initial response: {messages}")

        await self.start_ttfb_metrics()

        evt = events.ClientContentMessage.model_validate(
            {
                "clientContent": {
                    "turns": messages,
                    "turnComplete": self._inference_on_context_initialization,
                }
            }
        )
        await self.send_client_event(evt)
        if not self._inference_on_context_initialization:
            self._needs_turn_complete_message = True

    async def _create_single_response(self, messages_list):
        # refactor to combine this logic with same logic in GeminiMultimodalLiveContext
        messages = []
        for item in messages_list:
            role = item.get("role")

            if role == "system":
                continue

            elif role == "assistant":
                role = "model"

            content = item.get("content")
            parts = []
            if isinstance(content, str):
                parts = [{"text": content}]
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        parts.append({"text": part.get("text")})
                    else:
                        logger.warning(f"Unsupported content type: {str(part)[:80]}")
            else:
                logger.warning(f"Unsupported content type: {str(content)[:80]}")
            messages.append({"role": role, "parts": parts})
        if not messages:
            return
        logger.debug(f"Creating response: {messages}")

        await self.start_ttfb_metrics()

        evt = events.ClientContentMessage.model_validate(
            {
                "clientContent": {
                    "turns": messages,
                    "turnComplete": True,
                }
            }
        )
        # await self.send_client_event(evt)

    @traced_gemini_live(operation="llm_tool_result")
    async def _tool_result(self, tool_result_message):
        # For now we're shoving the name into the tool_call_id field, so this
        # will work until we revisit that.
        id = tool_result_message.get("tool_call_id")
        name = tool_result_message.get("tool_call_name")
        result = json.loads(tool_result_message.get("content") or "")
        response_message = json.dumps(
            {
                "toolResponse": {
                    "functionResponses": [
                        {
                            "id": id,
                            "name": name,
                            "response": {
                                "result": result,
                            },
                        }
                    ],
                }
            }
        )
        await self._websocket.send(response_message)
        # await self._websocket.send(json.dumps({"clientContent": {"turnComplete": True}}))

    @traced_gemini_live(operation="llm_setup")
    async def _handle_evt_setup_complete(self, evt):
        # If this is our first context frame, run the LLM
        self._api_session_ready = True
        # Now that we've configured the session, we can run the LLM if we need to.
        if self._run_llm_when_api_session_ready:
            self._run_llm_when_api_session_ready = False
            await self._create_initial_response()

    async def _handle_evt_model_turn(self, evt):
        part = evt.serverContent.modelTurn.parts[0]
        if not part:
            return

        await self.stop_ttfb_metrics()

        # part.text is added when `modalities` is set to TEXT; otherwise, it's None
        text = part.text
        if text:
            if not self._bot_text_buffer:
                await self.push_frame(LLMFullResponseStartFrame())

            self._bot_text_buffer += text
            await self.push_frame(LLMTextFrame(text=text))

        inline_data = part.inlineData
        if not inline_data:
            return
        if inline_data.mimeType != f"audio/pcm;rate={self._sample_rate}":
            logger.warning(f"Unrecognized server_content format {inline_data.mimeType}")
            return

        audio = base64.b64decode(inline_data.data)
        if not audio:
            return

        if not self._bot_is_speaking:
            self._bot_is_speaking = True
            await self.push_frame(TTSStartedFrame())
            await self.push_frame(LLMFullResponseStartFrame())

        self._bot_audio_buffer.extend(audio)
        frame = TTSAudioRawFrame(
            audio=audio,
            sample_rate=self._sample_rate,
            num_channels=1,
        )
        await self.push_frame(frame)

    @traced_gemini_live(operation="llm_tool_call")
    async def _handle_evt_tool_call(self, evt):
        function_calls = evt.toolCall.functionCalls
        if not function_calls:
            return
        if not self._context:
            logger.error("Function calls are not supported without a context object.")

        function_calls_llm = [
            FunctionCallFromLLM(
                context=self._context,
                tool_call_id=f.id,
                function_name=f.name,
                arguments=f.args,
            )
            for f in function_calls
        ]

        await self.run_function_calls(function_calls_llm)

    @traced_gemini_live(operation="llm_response")
    async def _handle_evt_turn_complete(self, evt):
        self._bot_is_speaking = False
        text = self._bot_text_buffer

        # Determine output and modality for tracing
        if text:
            # TEXT modality
            output_text = text
            output_modality = "TEXT"
        else:
            # AUDIO modality
            output_text = self._llm_output_buffer
            output_modality = "AUDIO"

        # Trace the complete LLM response (this will be handled by the decorator)
        # The decorator will extract the output text and usage metadata from the event

        self._bot_text_buffer = ""
        self._llm_output_buffer = ""

        # Only push the TTSStoppedFrame if the bot is outputting audio
        # when text is found, modalities is set to TEXT and no audio
        # is produced.
        if not text:
            await self.push_frame(TTSStoppedFrame())

        await self.push_frame(LLMFullResponseEndFrame())

    @traced_stt
    async def _handle_user_transcription(
        self, transcript: str, is_final: bool, language: Optional[Language] = None
    ):
        """Handle a transcription result with tracing."""
        pass

    async def _handle_evt_input_transcription(self, evt):
        """Handle the input transcription event.

        Gemini Live sends user transcriptions in either single words or multi-word
        phrases. As a result, we have to aggregate the input transcription. This handler
        aggregates into sentences, splitting on the end of sentence markers.
        """
        if not evt.serverContent.inputTranscription:
            return

        text = evt.serverContent.inputTranscription.text

        if not text:
            return

        # Strip leading space from sentence starts if buffer is empty
        if text.startswith(" ") and not self._user_transcription_buffer:
            text = text.lstrip()

        # Accumulate text in the buffer
        self._user_transcription_buffer += text

        # Check for complete sentences
        while True:
            eos_end_marker = match_endofsentence(self._user_transcription_buffer)
            if not eos_end_marker:
                break

            # Extract the complete sentence
            complete_sentence = self._user_transcription_buffer[:eos_end_marker]
            # Keep the remainder for the next chunk
            self._user_transcription_buffer = self._user_transcription_buffer[eos_end_marker:]

            # Send a TranscriptionFrame with the complete sentence
            logger.debug(f"[Transcription:user] [{complete_sentence}]")
            await self._handle_user_transcription(
                complete_sentence, True, self._settings["language"]
            )
            await self.push_frame(
                TranscriptionFrame(
                    text=complete_sentence,
                    user_id="",
                    timestamp=time_now_iso8601(),
                    result=evt,
                ),
                FrameDirection.UPSTREAM,
            )

    async def _handle_evt_output_transcription(self, evt):
        if not evt.serverContent.outputTranscription:
            return

        # This is the output transcription text when modalities is set to AUDIO.
        # In this case, we push LLMTextFrame and TTSTextFrame to be handled by the
        # downstream assistant context aggregator.
        text = evt.serverContent.outputTranscription.text

        if not text:
            return

        # Collect text for tracing
        self._llm_output_buffer += text

        await self.push_frame(LLMTextFrame(text=text))
        await self.push_frame(TTSTextFrame(text=text))

    async def _handle_evt_usage_metadata(self, evt):
        if not evt.usageMetadata:
            return

        usage = evt.usageMetadata

        # Ensure we have valid integers for all token counts
        prompt_tokens = usage.promptTokenCount or 0
        completion_tokens = usage.responseTokenCount or 0
        total_tokens = usage.totalTokenCount or (prompt_tokens + completion_tokens)

        tokens = LLMTokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        await self.start_llm_usage_metrics(tokens)

    def create_context_aggregator(
        self,
        context: OpenAILLMContext,
        *,
        user_params: LLMUserAggregatorParams = LLMUserAggregatorParams(),
        assistant_params: LLMAssistantAggregatorParams = LLMAssistantAggregatorParams(),
    ) -> GeminiMultimodalLiveContextAggregatorPair:
        """Create an instance of GeminiMultimodalLiveContextAggregatorPair from
        an OpenAILLMContext. Constructor keyword arguments for both the user and
        assistant aggregators can be provided.

        Args:
            context (OpenAILLMContext): The LLM context.
            user_params (LLMUserAggregatorParams, optional): User aggregator
                parameters.
            assistant_params (LLMAssistantAggregatorParams, optional): User
                aggregator parameters.

        Returns:
            GeminiMultimodalLiveContextAggregatorPair: A pair of context
            aggregators, one for the user and one for the assistant,
            encapsulated in an GeminiMultimodalLiveContextAggregatorPair.

        """
        context.set_llm_adapter(self.get_llm_adapter())

        GeminiMultimodalLiveContext.upgrade(context)
        user = GeminiMultimodalLiveUserContextAggregator(context, params=user_params)

        assistant_params.expect_stripped_words = False
        assistant = GeminiMultimodalLiveAssistantContextAggregator(context, params=assistant_params)
        return GeminiMultimodalLiveContextAggregatorPair(_user=user, _assistant=assistant)
