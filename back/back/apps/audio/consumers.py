import asyncio
import base64
import json
import logging
import os

import httpx
import websockets
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from dotenv import load_dotenv

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

logger = logging.getLogger(__name__)


class AudioTranscriptionConsumer(AsyncJsonWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.openai_ws = None
        self.openai_listener_task = None
        self.ephemeral_token = None

    async def _create_transcription_session_token(self):
        """
        Create a transcription session via the REST API to obtain an ephemeral token.
        """
        if not OPENAI_API_KEY:
            logger.error("OpenAI API key not configured.")
            await self.send_error_to_client("OpenAI API key not configured.")
            return None

        url = "https://api.openai.com/v1/realtime/transcription_sessions"
        payload = {
            "input_audio_format": "pcm16", # Assuming 16-bit PCM from client
            "input_audio_transcription": {
                "model": "gpt-4o-mini-transcribe",
                "language": "es", # Or make this configurable from client
                "prompt": "Transcribe the incoming audio in real time."
            },
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 800}
        }
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "assistants=v2"
        }
        try:
            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                token = data["client_secret"]["value"]
                logger.info("OpenAI Transcription session created; ephemeral token obtained.")
                return token
        except httpx.HTTPStatusError as e:
            error_msg = f"Failed to create OpenAI transcription session: {e.response.status_code} {e.response.reason_phrase} - {e.response.text}"
            logger.error(error_msg)
            await self.send_error_to_client(error_msg)
            return None
        except Exception as e:
            error_msg = f"An unexpected error occurred during OpenAI token creation: {e}"
            logger.error(error_msg, exc_info=True)
            await self.send_error_to_client(error_msg)
            return None

    async def _listen_to_openai(self):
        if not self.openai_ws:
            return
        try:
            async for message_str in self.openai_ws:
                try:
                    event = json.loads(message_str)
                    event_type = event.get("type")
                    # logger.debug(f"Received from OpenAI: {event}") # For debugging

                    if event_type == "input_audio_buffer.speech_started":
                        logger.info("OpenAI VAD: Speech started.")
                        await self.send_json({"type": "speech_started"})
                    
                    elif event_type == "conversation.item.input_audio_transcription.delta":
                        # Frontend handles TTS, so we don't need to send deltas unless specifically requested for UI updates
                        # delta = event.get("delta", "")
                        # if delta: await self.send_json({"type": "transcript_delta", "delta": delta})
                        pass # Not sending deltas for now as per requirements

                    elif event_type == "conversation.item.input_audio_transcription.completed":
                        transcript = event.get("transcript", "")
                        logger.info(f"OpenAI VAD: Transcription completed: {transcript}")
                        await self.send_json({"type": "transcription_completed", "transcript": transcript})
                    
                    elif event_type == "transcription_session.created":
                        logger.info(f"OpenAI session confirmed created: {event.get('session', {}).get('id')}")
                    
                    elif event_type == "transcription_session.updated":
                         logger.info(f"OpenAI session confirmed updated: {event.get('session')}")
                    
                    elif event_type == "error":
                        error_payload = event.get("error", "Unknown OpenAI error")
                        logger.error(f"Error event from OpenAI Realtime API: {error_payload}")
                        await self.send_error_to_client(f"OpenAI API Error: {error_payload}")
                        # Depending on severity, might want to close connections
                        break # Stop listening on OpenAI error

                except json.JSONDecodeError:
                    logger.error(f"Failed to decode JSON message from OpenAI: {message_str}")
                except Exception as ex:
                    logger.error(f"Error processing message from OpenAI: {ex}", exc_info=True)
                    # Potentially send error to client or break

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"OpenAI WebSocket connection closed: {e}")
        except asyncio.CancelledError:
            logger.info("OpenAI listener task cancelled.")
        except Exception as e:
            logger.error(f"Error in OpenAI listener task: {e}", exc_info=True)
        finally:
            logger.info("OpenAI listener task finished.")
            # Ensure client connection is also closed if OpenAI connection dies unexpectedly
            if self.openai_ws and self.openai_ws.closed:
                 await self.close(code=1011) # Internal server error

    async def connect(self):
        await self.accept()
        logger.info(f"AudioTranscriptionConsumer connected: {self.channel_name}")

        self.ephemeral_token = await self._create_transcription_session_token()
        if not self.ephemeral_token:
            await self.close(code=4001) # Custom code for auth failure
            return

        websocket_uri = "wss://api.openai.com/v1/realtime?intent=transcription"
        connection_headers = {
            "Authorization": f"Bearer {self.ephemeral_token}",
            "OpenAI-Beta": "realtime=v1"
        }

        try:
            logger.info(f"Connecting to OpenAI WebSocket: {websocket_uri}")
            self.openai_ws = await websockets.connect(
                websocket_uri,
                extra_headers=connection_headers
            )
            logger.info("Successfully connected to OpenAI Realtime API via WebSocket.")

            # Send initial configuration update to OpenAI WebSocket
            update_event_payload = {
                "type": "transcription_session.update",
                "session": {
                    "input_audio_format": "pcm16",
                    "input_audio_transcription": {
                        "model": "gpt-4o-mini-transcribe",
                        "language": "en",
                        "prompt": "Transcribe the incoming audio in real time."
                    },
                    "turn_detection": {"type": "server_vad", "silence_duration_ms": 800}
                }
            }
            await self.openai_ws.send(json.dumps(update_event_payload))
            logger.info("Sent transcription_session.update event to OpenAI.")

            self.openai_listener_task = asyncio.create_task(self._listen_to_openai())
            await self.send_json({"type": "connection_established", "message": "Connected to transcription service."})

        except websockets.exceptions.InvalidStatusCode as e:
            error_msg = f"OpenAI WebSocket connection failed with status {e.status_code}: {e.headers.get('www-authenticate') or 'No auth details'}"
            logger.error(error_msg)
            await self.send_error_to_client(error_msg)
            await self.close(code=4002) # Custom code for upstream connection failure
        except websockets.exceptions.WebSocketException as e:
            error_msg = f"OpenAI WebSocket connection error: {e}"
            logger.error(error_msg, exc_info=True)
            await self.send_error_to_client(error_msg)
            await self.close(code=4002)
        except Exception as e:
            error_msg = f"Unexpected error during connect: {e}"
            logger.error(error_msg, exc_info=True)
            await self.send_error_to_client(error_msg)
            await self.close(code=4000) # Generic error

    async def disconnect(self, close_code):
        logger.info(f"AudioTranscriptionConsumer disconnecting: {self.channel_name} with code {close_code}")
        if self.openai_listener_task and not self.openai_listener_task.done():
            self.openai_listener_task.cancel()
            try:
                await self.openai_listener_task
            except asyncio.CancelledError:
                logger.info("OpenAI listener task successfully cancelled during disconnect.")
        
        if self.openai_ws and not self.openai_ws.closed:
            try:
                await self.openai_ws.close()
                logger.info("OpenAI WebSocket connection closed during disconnect.")
            except Exception as e:
                logger.error(f"Error closing OpenAI WebSocket during disconnect: {e}")
        self.openai_ws = None
        self.openai_listener_task = None

    async def receive_json(self, content):
        # Expecting messages like: {"type": "audio_chunk", "data": "<base64_audio_string>"}
        # Or {"type": "control", "command": "end_stream"} (optional)
        message_type = content.get("type")
        # logger.debug(f"Received from client: {message_type}") # For debugging

        if message_type == "audio_chunk":
            audio_base64 = content.get("data")
            if not audio_base64:
                logger.warning("Received audio_chunk with no data.")
                return

            if self.openai_ws and not self.openai_ws.closed:
                try:
                    openai_payload = {
                        "type": "input_audio_buffer.append",
                        "audio": audio_base64
                    }
                    await self.openai_ws.send(json.dumps(openai_payload))
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("Attempted to send to closed OpenAI WebSocket. Client might disconnect soon.")
                    # Error will likely be caught by listener task or next connect attempt
                except Exception as e:
                    logger.error(f"Error sending audio chunk to OpenAI: {e}", exc_info=True)
                    await self.send_error_to_client("Error processing your audio.")
            else:
                logger.warning("OpenAI WebSocket is not connected or closed. Cannot send audio chunk.")
                await self.send_error_to_client("Not connected to transcription service. Please try reconnecting.")
        
        elif message_type == "control": # Example for control messages like end of stream
            command = content.get("command")
            if command == "end_user_audio": # Client signals end of its audio stream
                logger.info("Client signalled end of audio stream. Sending commit to OpenAI.")
                if self.openai_ws and not self.openai_ws.closed:
                    try:
                        await self.openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    except Exception as e:
                         logger.error(f"Error sending commit to OpenAI: {e}")
            else:
                logger.warning(f"Received unknown control command: {command}")

        else:
            logger.warning(f"Received unknown message type from client: {message_type}")
            await self.send_error_to_client(f"Unknown message type: {message_type}")

    async def send_error_to_client(self, error_message):
        try:
            await self.send_json({"type": "error", "message": str(error_message)})
        except Exception as e:
            logger.error(f"Failed to send error to client: {e}") 