import asyncio
import base64
import json
import logging
import os

import httpx
import pyaudio
import websockets
from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai.helpers import LocalAudioPlayer

# Load environment variables
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("Missing OpenAI API key. Please set it in your .env file.")

# Initialize OpenAI client
client = AsyncOpenAI()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Audio stream parameters for input (microphone)
INPUT_RATE = 16000  # OpenAI recommends 16kHz for STT
INPUT_CHANNELS = 1
INPUT_FORMAT = pyaudio.paInt16  # 16-bit PCM
INPUT_CHUNK_DURATION_MS = 100  # Send audio in 100ms chunks
INPUT_CHUNK_SIZE = int(INPUT_RATE * (INPUT_CHUNK_DURATION_MS / 1000.0)) * (pyaudio.get_sample_size(INPUT_FORMAT) * INPUT_CHANNELS)

# Audio stream parameters for output (TTS playback)
OUTPUT_RATE = 24000 # OpenAI TTS default for PCM
OUTPUT_CHANNELS = 1
OUTPUT_FORMAT = pyaudio.paInt16 # For pcm_s16le from OpenAI


FINAL_TRANSCRIPTION = ""
DEFAULT_RESPONSE_TEXT = "I am very sad that Dave doesn't play in today's football match. I hope he will be able to play in the next match. Although he is probably very bad and I wouldn't like him in my team and Dani is a coward for not playing also."
DEFAULT_RESPONSE_TEXT = "I hope Mar and Irene play very bad today so I can laugh at them while Andres gets mad at them."
# Event to signal that TTS is playing, to pause mic input if needed (optional enhancement)
tts_playing_event = asyncio.Event()
user_interruption_flag = asyncio.Event() # New event for handling user interruptions


async def create_transcription_session_token():
    """
    Create a transcription session via the REST API to obtain an ephemeral token.
    Uses httpx for asynchronous request, as it's a dependency of the openai client.
    """
    url = "https://api.openai.com/v1/realtime/transcription_sessions"
    payload = {
        "input_audio_format": "pcm16",
        "input_audio_transcription": {
            "model": "gpt-4o-mini-transcribe",
            "language": "en",
            "prompt": "Transcribe the incoming audio in real time."
        },
        "turn_detection": {"type": "server_vad", "silence_duration_ms": 800}
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "assistants=v2"
    }
    async with httpx.AsyncClient() as http_client:
        try:
            response = await http_client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            ephemeral_token = data["client_secret"]["value"]
            logger.info("Transcription session created; ephemeral token obtained.")
            return ephemeral_token
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to create transcription session: {e.response.status_code} {e.response.reason_phrase}")
            logger.error(f"Response content: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred during token creation: {e}")
            raise


async def send_audio_from_mic(ws, stop_event: asyncio.Event):
    """
    Capture audio from the microphone and send it in chunks over the WebSocket.
    Listens continuously, regardless of TTS state, for interruption capability.
    """
    audio_interface = pyaudio.PyAudio()
    try:
        stream = audio_interface.open(format=INPUT_FORMAT,
                                      channels=INPUT_CHANNELS,
                                      rate=INPUT_RATE,
                                      input=True,
                                      frames_per_buffer=INPUT_CHUNK_SIZE)
        logger.info("Microphone stream opened. Always listening...")

        while not stop_event.is_set():
            # Removed tts_playing_event check to always listen
            try:
                audio_data = stream.read(INPUT_CHUNK_SIZE, exception_on_overflow=False)
                audio_base64 = base64.b64encode(audio_data).decode('utf-8')
                
                audio_event = {
                    "type": "input_audio_buffer.append",
                    "audio": audio_base64
                }
                await ws.send(json.dumps(audio_event))
                await asyncio.sleep(float(INPUT_CHUNK_DURATION_MS) / 2000.0)
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received, stopping audio send.")
                break
            except Exception as e:
                logger.error(f"Error sending audio: {e}")
                break
        
        logger.info("Finished sending audio from microphone.")
    except Exception as e:
        logger.error(f"Error in send_audio_from_mic: {e}")
    finally:
        if 'stream' in locals() and stream.is_active():
            stream.stop_stream()
            stream.close()
        audio_interface.terminate()
        logger.info("Microphone stream closed.")


async def speak_response(text_to_speak: str, current_user_interruption_flag: asyncio.Event):
    """Generates and plays TTS audio; can be interrupted by current_user_interruption_flag."""
    logger.info(f"Attempting to speak: '{text_to_speak[:50]}...' using OpenAI client TTS.")
    tts_playing_event.set()
    current_user_interruption_flag.clear() # Clear at the start of a new TTS attempt

    player = LocalAudioPlayer()
    playback_task = None

    async def play_audio_with_player_interruptible(audio_response):
        nonlocal player # Ensure we are using the player instance from the outer scope
        try:
            await player.play(audio_response)
        except asyncio.CancelledError:
            logger.info("TTS playback (player.play) was cancelled.")
        except Exception as e_playback:
            logger.error(f"Error within player.play: {e_playback}", exc_info=True)

    try:
        async with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts", 
            voice="alloy",
            input=text_to_speak,
            response_format="pcm"
        ) as response:
            logger.info("OpenAI TTS stream obtained. Starting interruptible playback.")
            
            playback_task = asyncio.create_task(play_audio_with_player_interruptible(response))

            while not playback_task.done():
                if current_user_interruption_flag.is_set():
                    logger.info("Interruption detected by speak_response! Cancelling TTS playback task.")
                    if not playback_task.done(): # Check again before cancelling
                        playback_task.cancel()
                    break 
                await asyncio.sleep(0.05) # Check for interruption periodically
            
            await playback_task # Wait for the task to naturally finish or be fully cancelled

        if current_user_interruption_flag.is_set():
            logger.info("TTS playback was interrupted by user (flag was set).")
        elif playback_task and playback_task.cancelled():
            logger.info("TTS playback task was explicitly cancelled.")
        else:
            logger.info("TTS playback finished normally.")

    except asyncio.CancelledError:
        logger.info("speak_response task itself was cancelled (e.g. app shutdown).")
        if playback_task and not playback_task.done():
            playback_task.cancel()
            await asyncio.gather(playback_task, return_exceptions=True)
    except Exception as e:
        logger.error(f"Error during OpenAI client TTS setup or outer playback loop: {e}", exc_info=True)
        if playback_task and not playback_task.done():
            playback_task.cancel()
            await asyncio.gather(playback_task, return_exceptions=True)
    finally:
        tts_playing_event.clear()
        current_user_interruption_flag.clear() # Ensure cleared on exit
        logger.info("speak_response finished.")


async def receive_transcription_events(ws, stop_event: asyncio.Event, current_user_interruption_flag: asyncio.Event):
    """
    Listen for events from the realtime endpoint and process transcription results.
    Triggers TTS response and handles interruptions.
    """
    global FINAL_TRANSCRIPTION
    FINAL_TRANSCRIPTION = ""
    active_tts_task = None

    try:
        async for message_str in ws:
            if stop_event.is_set():
                break
            try:
                event = json.loads(message_str)
                event_type = event.get("type")

                if event_type == "input_audio_buffer.speech_started":
                    logger.info("Speech started (VAD).")
                    if tts_playing_event.is_set(): # If TTS is playing when user starts speaking
                        logger.info("User speech detected during TTS. Setting interruption flag.")
                        current_user_interruption_flag.set() # Signal to interrupt TTS
                
                elif event_type == "transcription_session.created":
                    logger.info(f"Session created: {event.get('session', {}).get('id')}")
                elif event_type == "transcription_session.updated":
                    logger.info(f"Session updated: {event.get('session')}")
                elif event_type == "conversation.item.input_audio_transcription.delta":
                    delta = event.get("delta", "")
                    if delta:
                        logger.info(delta) 
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    completed_text = event.get("transcript", "")
                    FINAL_TRANSCRIPTION = completed_text
                    logger.info(f"User: {completed_text}")
                    
                    if current_user_interruption_flag.is_set():
                        logger.info("TTS was interrupted. Not playing default response for this completed transcription.")
                        current_user_interruption_flag.clear() # Reset for the next potential TTS turn
                    elif FINAL_TRANSCRIPTION.strip():
                        if tts_playing_event.is_set():
                            logger.warning("TTS playing event is set, but trying to start new TTS. Possible race or missed clear. Skipping new TTS.")
                        else:
                            logger.info("Transcription complete. Preparing TTS response.")
                            # Clear flag before starting new TTS, ensuring it's for *this* playback attempt
                            current_user_interruption_flag.clear() 
                            active_tts_task = asyncio.create_task(speak_response(DEFAULT_RESPONSE_TEXT, current_user_interruption_flag))
                    
                elif event_type == "input_audio_buffer.speech_stopped":
                    logger.info("Speech stopped by VAD.")
                elif event_type == "error":
                    logger.error(f"Error event from OpenAI: {event.get('error')}")
                    stop_event.set()
                    break
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON message: {message_str}")
            except Exception as ex:
                logger.error(f"Error processing message: {ex}", exc_info=True)
                stop_event.set()
                break
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"WebSocket connection closed: {e}")
    except Exception as e:
        logger.error(f"Error receiving events: {e}", exc_info=True)
    finally:
        if active_tts_task and not active_tts_task.done():
            logger.info("Receiver loop ending, cancelling any active TTS task.")
            active_tts_task.cancel()
        if not stop_event.is_set():
            stop_event.set()
        tts_playing_event.clear()
        current_user_interruption_flag.clear() # Ensure cleared on exit
        logger.info("Stopped receiving transcription events.")


async def transcribe_and_respond():
    """
    Main function to manage the real-time transcription and response process.
    """
    global FINAL_TRANSCRIPTION
    stop_event = asyncio.Event()
    current_user_interruption_flag = asyncio.Event() # Create the event here

    try:
        logger.info("Attempting to create transcription session token...")
        ephemeral_token = await create_transcription_session_token()
        if not ephemeral_token:
            logger.error("Failed to obtain ephemeral token. Aborting.")
            return

        websocket_uri = "wss://api.openai.com/v1/realtime?intent=transcription"
        connection_headers = {
            "Authorization": f"Bearer {ephemeral_token}",
            "OpenAI-Beta": "realtime=v1"
        }
        
        logger.info(f"Connecting to WebSocket: {websocket_uri}")
        async with websockets.connect(
            websocket_uri,
            extra_headers=connection_headers
        ) as ws:
            logger.info("Successfully connected to OpenAI Realtime API via WebSocket.")

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
            await ws.send(json.dumps(update_event_payload))
            logger.info("Sent transcription_session.update event.")

            logger.info("Starting audio sending and event receiving tasks...")
            sender_task = asyncio.create_task(send_audio_from_mic(ws, stop_event))
            receiver_task = asyncio.create_task(receive_transcription_events(ws, stop_event, current_user_interruption_flag))

            done, pending = await asyncio.wait(
                [sender_task, receiver_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            logger.info("Real-time transcription and response cycle finished.")
            if FINAL_TRANSCRIPTION:
                logger.info(f"Last user transcription: {FINAL_TRANSCRIPTION}")

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"WebSocket connection failed with status {e.status_code}: {e.headers.get('www-authenticate') or 'No auth details'}")
    except websockets.exceptions.WebSocketException as e:
        logger.error(f"WebSocket connection error: {e}")
    except Exception as e:
        logger.error(f"An error occurred during real-time transcription/response: {e}", exc_info=True)
    finally:
        stop_event.set()
        tts_playing_event.clear()
        current_user_interruption_flag.clear() # Ensure cleared on exit
        logger.info("Transcription and response session stopped.")


if __name__ == "__main__":
    try:
        # Renamed main async function for clarity
        asyncio.run(transcribe_and_respond())
    except KeyboardInterrupt:
        logger.info("Application terminated by user.")
    except Exception as e:
        # This logger.error was duplicated in the original user diff, keeping one.
        logger.error(f"Unhandled exception in main: {e}", exc_info=True)
        logger.error(f"Unhandled exception in main: {e}", exc_info=True)
