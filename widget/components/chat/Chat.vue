<template>
    <div class="chat-wrapper" :class="{ 'dark-mode': store.darkMode, 'fit-to-parent': store.fitToParent, 'stick-input-prompt': store.stickInputPrompt }" @click="store.menuOpened = false">
        <div class="conversation-content" ref="conversationContent" :class="{'dark-mode': store.darkMode, 'fit-to-parent-conversation-content': store.fitToParent}">
            <div class="stacks" :class="{'merge-to-prev': getFirstLayerMergeToPrev(message)}" v-for="(message, index) in store.messages">
                <ChatMsgManager
                    v-if="isRenderableStackType(message)"
                    :message="message"
                    :key="message.stack_id"
                    :is-last-of-type="isLastOfType(index)"
                    :is-first="index === 0"
                    :is-last="index === store.messages.length - 1"
                ></ChatMsgManager>
            </div>
            <LoaderMsg v-if="store.waitingForResponse"></LoaderMsg>
        </div>
        <div class="alert-message" :class="{ 'fade-out': feedbackSentDisabled, 'dark-mode': store.darkMode }">
            {{ $t("feedbacksent") }}
        </div>
        <div class="alert-message"
             :class="{ 'fade-out': !store.disconnected, 'dark-mode': store.darkMode, 'pulsating': store.disconnected }">
            {{ $t("connectingtoserver") }}
        </div>
        <div v-if="store.backendTranscribing" class="alert-message" :class="{'dark-mode': store.darkMode }">
            {{ $t("listening") }} <!-- Placeholder for listening indicator -->
        </div>
        <ChatPrompt @send="(msg) => sendMessage(msg)"/>
    </div>
</template>

<script setup>
import {ref, watch, nextTick, onMounted, onBeforeUnmount} from "vue";
import {useGlobalStore} from "~/store";
import LoaderMsg from "~/components/chat/LoaderMsg.vue";
import ChatMsgManager from "~/components/chat/msgs/ChatMsgManager.vue";
import ChatPrompt from "~/components/chat/ChatPrompt.vue";

const store = useGlobalStore();

const chatInput = ref(null);
const conversationContent = ref(null)
const feedbackSentDisabled = ref(true)
const notRenderableStackTypes = ["gtm_tag", "close_conversation", undefined]

let ws = undefined // Main chat WebSocket

// --- New Audio Streaming Refs ---
const audioSocket = ref(null);
const audioContext = ref(null);
const mediaStreamSource = ref(null);
const scriptProcessorNode = ref(null);
const audioBufferQueue = ref([]); // Stores Int16Array chunks
const TARGET_SAMPLE_RATE = 16000;
const CHUNK_DURATION_MS = 100; // Send audio in 100ms chunks
const SAMPLES_PER_CHUNK = TARGET_SAMPLE_RATE * (CHUNK_DURATION_MS / 1000);
const SCRIPT_PROCESSOR_BUFFER_SIZE = 4096; // Power of 2, for Web Audio API
let audioIntervalId = null;
let inputCounter = 0;


// --- Store conceptual properties (assuming they are added to global store elsewhere or managed via props) ---
// store.ttsShouldBeInterrupted = store.ttsShouldBeInterrupted || ref(false); // Managed by TextMsgPiece interaction
// store.backendTranscribing = store.backendTranscribing || ref(false); // For UI feedback

watch(() => store.scrollToBottom, scrollConversationDown)
watch(() => store.selectedPlConversationId, async (newId, oldId) => {
    await createConnection(); // Existing chat WebSocket
    if (store.speechRecognitionAlwaysOn) {
        if (oldId && audioSocket.value) {
            await stopAudioStreamingAndSocket();
        }
        if (newId) {
            await connectAudioWebSocketAndStream();
        }
    }
})
watch(() => store.feedbackSent, animateFeedbackSent)
watch(() => store.resendMsgId, resendMsg)
watch(() => store.messagesToBeSentSignal, sendMessagesToBeSent)

watch(() => store.speechRecognitionAlwaysOn, (isAlwaysOn) => {
    if (isAlwaysOn) {
        connectAudioWebSocketAndStream();
    } else {
        stopAudioStreamingAndSocket();
    }
});

onMounted(async () => {
    await initializeConversation(); // Includes createConnection for chat ws
    if (store.speechRecognitionAlwaysOn && store.selectedPlConversationId) {
       await connectAudioWebSocketAndStream();
    }
})

onBeforeUnmount(async () => {
    if (ws) {
        ws.onclose = null; // Prevent reconnection attempts
        ws.close();
    }
    await stopAudioStreamingAndSocket();
})

async function connectAudioWebSocketAndStream() {
    if (!store.selectedPlConversationId || store.previewMode) {
        console.log("[Chat.vue] connectAudioWebSocketAndStream: Aborting - no selectedPlConversationId or in previewMode. ID:", store.selectedPlConversationId, "Preview:", store.previewMode);
        return;
    }
    console.log("[Chat.vue] connectAudioWebSocketAndStream: Proceeding to connect audio WebSocket.");
    try {
        await connectAudioWebSocket(); // This now returns a promise that resolves on open
        // If the promise resolved, the WebSocket should be open.
        if (audioSocket.value && audioSocket.value.readyState === WebSocket.OPEN) {
            console.log("[Chat.vue] connectAudioWebSocketAndStream: Audio WebSocket successfully opened, calling initAudioStreaming.");
            await initAudioStreaming();
        } else {
             console.error("[Chat.vue] connectAudioWebSocketAndStream: Audio WebSocket not in OPEN state after connect promise resolved. State:", audioSocket.value ? audioSocket.value.readyState : 'null', "Socket:", audioSocket.value);
             // This case should ideally not be reached if the promise logic in connectAudioWebSocket is correct.
        }
    } catch (error) {
        console.error("[Chat.vue] connectAudioWebSocketAndStream: Failed to connect Audio WebSocket or init streaming due to error:", error);
        // Optionally, update UI or store to reflect this failure
        store.backendTranscribing = false; // Ensure this is reset
    }
}

async function stopAudioStreamingAndSocket() {
    await stopAudioStreaming();
    if (audioSocket.value) {
        audioSocket.value.onclose = null;
        audioSocket.value.close();
        audioSocket.value = null;
        store.backendTranscribing = false;
    }
}


// --- Audio Streaming and WebSocket Logic ---

function getAudioWebSocketUrl() {
    if (!store.chatfaqWS) return null;
    try {
        const url = new URL(store.chatfaqWS);
        // Assuming routing.py's ws/audio/transcribe/ is at the root of the domain for WS connections
        return `${url.protocol === 'wss:' ? 'wss:' : 'ws:'}//${url.host}/ws/audio/transcribe/`;
    } catch (e) {
        console.error("Error constructing audio WebSocket URL:", e);
        return null;
    }
}

async function connectAudioWebSocket() {
    return new Promise((resolve, reject) => {
        if (audioSocket.value && audioSocket.value.readyState === WebSocket.OPEN) {
            console.log("[Chat.vue] connectAudioWebSocket: Already open.");
            resolve();
            return;
        }
        if (store.previewMode) {
            console.log("[Chat.vue] connectAudioWebSocket: Aborting - previewMode.");
            reject(new Error("Preview mode"));
            return;
        }

        const audioWsUrl = getAudioWebSocketUrl();
        if (!audioWsUrl) {
            console.error("[Chat.vue] Audio WebSocket URL could not be determined.");
            store.backendTranscribing = false;
            reject(new Error("Audio WebSocket URL could not be determined."));
            return;
        }

        console.log("[Chat.vue] Attempting to connect to Audio WebSocket:", audioWsUrl);
        audioSocket.value = new WebSocket(audioWsUrl);

        audioSocket.value.onopen = () => {
            console.log("[Chat.vue] Audio WebSocket connection established (onopen).");
            store.backendTranscribing = true; // Indicate listening state
            resolve();
        };

        audioSocket.value.onmessage = (event) => {
            try {
                const message = JSON.parse(event.data);
                // console.log("Received from Audio WS:", message);

                switch (message.type) {
                    case "connection_established":
                        console.log("Audio WS: Connection confirmed by backend.");
                        break;
                    case "speech_started":
                        console.log("Audio WS: Speech started (VAD). Interrupting TTS.");
                        if (typeof store.interruptTTS === 'function') {
                            store.interruptTTS();
                        } else { // Fallback if store.interruptTTS is not defined yet
                            if (window.speechSynthesis) window.speechSynthesis.cancel();
                            store.ttsShouldBeInterrupted = true; // Ensure TextMsgPiece picks this up
                        }
                        break;
                    case "transcription_completed":
                        console.log("Audio WS: Transcription completed:", message.transcript);
                        store.backendTranscribing = false; // VAD detected end or final transcription
                        if (message.transcript && message.transcript.trim() !== "") {
                            const userMessage = {
                                sender: { type: 'human', platform: 'audio_input' },
                                stack: [{
                                    type: 'message',
                                    payload: { content: message.transcript.trim() }
                                }],
                                stack_id: Math.random().toString(36).substring(7),
                                stack_group_id: Math.random().toString(36).substring(7),
                                last: true,
                            };
                            if (store.speechRecognitionAutoSend) {
                                sendMessage(userMessage, true); // Send immediately
                            } else {
                                // If not auto-send, might populate prompt or handle differently
                               store.addMessage(userMessage);
                               store.scrollToBottom +=1;
                            }
                        }
                        break;
                    case "error":
                        console.error("Audio WS: Error from backend:", message.message);
                        // store.addMessage for error display
                        store.backendTranscribing = false;
                        break;
                    default:
                        console.warn("Audio WS: Unknown message type:", message.type);
                }
            } catch (e) {
                console.error("Error processing message from Audio WS:", e);
            }
        };

        audioSocket.value.onerror = (error) => {
            console.error("[Chat.vue] Audio WebSocket error:", error);
            store.backendTranscribing = false;
            // store.addMessage for error display
            reject(error); // Reject the promise on error
        };

        audioSocket.value.onclose = (event) => {
            console.log("[Chat.vue] Audio WebSocket connection closed:", event.code, event.reason);
            store.backendTranscribing = false;
            // Handle potential reconnection if desired, but not for now.
            // If the promise hasn't resolved (e.g. onopen never fired), we might want to reject it here too, 
            // but usually onerror will cover connection failures.
        };
    });
}

async function initAudioStreaming() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        console.error("[Chat.vue] initAudioStreaming: getUserMedia not supported on this browser!");
        // store.addMessage for error display
        return;
    }
    if (audioContext.value) {
        console.log("[Chat.vue] initAudioStreaming: AudioContext already initialized. Skipping.");
        return; // Already initialized
    }
    console.log("[Chat.vue] initAudioStreaming: Starting audio stream initialization...");

    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                sampleRate: { ideal: 24000 },
                channelCount: 1,
                echoCancellation: true,
                // autoGainControl: true,
                noiseSuppression: true
            }
        });
        console.log("[Chat.vue] initAudioStreaming: getUserMedia successful. Stream:", stream);
        audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
        console.log("[Chat.vue] initAudioStreaming: AudioContext created. Sample rate:", audioContext.value.sampleRate);
        
        mediaStreamSource.value = audioContext.value.createMediaStreamSource(stream);
        scriptProcessorNode.value = audioContext.value.createScriptProcessor(
            SCRIPT_PROCESSOR_BUFFER_SIZE, 1, 1 // bufferSize, inputChannels, outputChannels
        );

        scriptProcessorNode.value.onaudioprocess = (audioProcessingEvent) => {
            if (!audioSocket.value || audioSocket.value.readyState !== WebSocket.OPEN) {
                return;
            }

            const inputBuffer = audioProcessingEvent.inputBuffer;
            const rawPcmFloat32 = inputBuffer.getChannelData(0); // Assuming mono

            // Resample if necessary
            let resampledPcmFloat32;
            if (inputCounter++ % 100 === 0) { // Log approx every 100*SCRIPT_PROCESSOR_BUFFER_SIZE samples processed
                console.log("[Chat.vue] onaudioprocess: Processing audio frame. Input SR:", audioProcessingEvent.inputBuffer.sampleRate, "Queue length:", audioBufferQueue.value.length);
            }

            if (audioContext.value.sampleRate === TARGET_SAMPLE_RATE) {
                resampledPcmFloat32 = rawPcmFloat32;
            } else {
                resampledPcmFloat32 = resampleBuffer(rawPcmFloat32, audioContext.value.sampleRate, TARGET_SAMPLE_RATE);
                        }
            
            const pcm16Array = float32ToInt16(resampledPcmFloat32);
            audioBufferQueue.value.push(...Array.from(pcm16Array)); // Add individual samples
        };

        mediaStreamSource.value.connect(scriptProcessorNode.value);
        scriptProcessorNode.value.connect(audioContext.value.destination); // Necessary for processing

        // Start interval to send chunks from queue
        if (audioIntervalId) clearInterval(audioIntervalId);
        audioIntervalId = setInterval(sendAudioChunkFromQueue, CHUNK_DURATION_MS);

        console.log("Audio streaming initialized. Context SR:", audioContext.value.sampleRate, "Target SR:", TARGET_SAMPLE_RATE);

    } catch (err) {
        console.error("Error initializing audio streaming:", err);
        // store.addMessage for error display
        if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
            store.speechRecognitionAlwaysOn = false; // Disable if permission denied
             // Inform user via a message
            const permMessage = {
                sender: { type: "bot" }, stack: [{ type: "message", payload: { content: "Microphone permission denied. Real-time transcription disabled." } }],
                stack_id: Math.random().toString(36).substring(7), stack_group_id: Math.random().toString(36).substring(7), last: true,
            };
            store.addMessage(permMessage);
        }
    }
}

function sendAudioChunkFromQueue() {
    if (audioBufferQueue.value.length >= SAMPLES_PER_CHUNK) {
        const chunkToSend = new Int16Array(audioBufferQueue.value.splice(0, SAMPLES_PER_CHUNK));
        if (audioSocket.value && audioSocket.value.readyState === WebSocket.OPEN) {
            const audioBase64 = btoa(String.fromCharCode.apply(null, new Uint8Array(chunkToSend.buffer)));
            console.log("[Chat.vue] sendAudioChunkFromQueue: Sending audio chunk, bytes:", chunkToSend.byteLength);
            audioSocket.value.send(JSON.stringify({
                type: "audio_chunk",
                data: audioBase64
            }));
        }
    }
}

async function stopAudioStreaming() {
    if (audioIntervalId) clearInterval(audioIntervalId);
    audioIntervalId = null;

    if (scriptProcessorNode.value) {
        scriptProcessorNode.value.disconnect();
        scriptProcessorNode.value.onaudioprocess = null;
        scriptProcessorNode.value = null;
    }
    if (mediaStreamSource.value) {
        mediaStreamSource.value.disconnect();
        mediaStreamSource.value.mediaStream.getTracks().forEach(track => track.stop());
        mediaStreamSource.value = null;
    }
    if (audioContext.value && audioContext.value.state !== 'closed') {
        try {
            await audioContext.value.close();
        } catch (e) { console.error("Error closing audio context:", e); }
        audioContext.value = null;
    }
    audioBufferQueue.value = [];
    console.log("Audio streaming stopped.");
}

// --- Audio Processing Utilities ---
function resampleBuffer(inputBufferFloat32, inputSampleRate, outputSampleRate) {
    if (inputSampleRate === outputSampleRate) {
        return inputBufferFloat32;
    }
    const inputLength = inputBufferFloat32.length;
    const outputLength = Math.round(inputLength * outputSampleRate / inputSampleRate);
    const outputBuffer = new Float32Array(outputLength);
    const ratio = inputSampleRate / outputSampleRate;
    
    for (let i = 0; i < outputLength; i++) {
        // Simple linear interpolation
        const P = i * ratio;
        const K = Math.floor(P);
        const F = P - K;
        
        const V0 = inputBufferFloat32[K] || 0;
        const V1 = inputBufferFloat32[K + 1] || 0;
        
        outputBuffer[i] = V0 + F * (V1 - V0);
    }
    return outputBuffer;
}

function float32ToInt16(buffer) {
    let l = buffer.length;
    const buf = new Int16Array(l);
    while (l--) {
        buf[l] = Math.min(1, Math.max(-1, buffer[l])) * 0x7FFF; // Clamp and scale
    }
    return buf;
}


// --- Existing Chat Logic ---
function isLastOfType(index) {
    return index === store.messages.length - 1 || store.messages[index + 1].sender.type !== store.messages[index].sender.type
}
function getFirstLayerMergeToPrev(message) {
    return message?.stack[0]?.payload.merge_to_prev;
}
function scrollConversationDown() {
    nextTick(() => {
        conversationContent.value.scroll({top: conversationContent.value.scrollHeight, behavior: "smooth"})
    })
}

function isFullyScrolled() {
    return conversationContent.value.scrollHeight - conversationContent.value.scrollTop === conversationContent.value.clientHeight
}

function animateFeedbackSent() {
    feedbackSentDisabled.value = false
    setTimeout(() => {
        feedbackSentDisabled.value = true
    }, 1500)
}
function isRenderableStackType(message) {
    return !notRenderableStackTypes.includes(message.stack[0]?.type)
}

function createConnection() {
    if(store.previewMode)
        return

    if (ws)
        ws.close()

    let queryParams = ""
    if (store.initialConversationMetadata)
        queryParams = `?metadata=${JSON.stringify(store.initialConversationMetadata)}`

    if (store.authToken && store.authToken.length) {
        if (queryParams.length) queryParams += "&"
        else queryParams = "?"
        queryParams += `token=${encodeURIComponent(store.authToken)}`
    }


    if (store.stateOverride) {
        if (queryParams.length) {
            queryParams += "&"
        } else {
            queryParams = "?"
        }
        queryParams += `state_override=${encodeURIComponent(store.stateOverride)}`
    }

    ws = new WebSocket(
        store.chatfaqWS
        + "/back/ws/broker/"
        + store.selectedPlConversationId
        + "/"
        + store.fsmDef
        + "/"
        + (store.userId ? `${store.userId}/${queryParams}` : "")
    );
    ws.onmessage = async function (e) {
        const msg = JSON.parse(e.data);
        if (msg.status === 400) {
            console.error(`Error in message from WS: ${msg.payload}`)
            return
        }

        if (store.lastMsg && store.lastMsg.sender.type === 'human')  // Scroll down if brand a new message from bot just came
            store.scrollToBottom += 1;
        if (isFullyScrolled())  // Scroll down if user is at the bottom
            store.scrollToBottom += 1;
        if (msg.stack[0]?.type === 'close_conversation')
            store.conversationClosed = true

        sendToGTM(msg)
        store.addMessage(msg);
        if(store.messages.length === 1)
            await store.gatherConversations()
    };
    ws.onopen = async function () {
        store.disconnected = false;
        // await store.gatherConversations()
    };
    const plConversationId = store.selectedPlConversationId
    ws.onclose = function (e) {
        if (e.code === 4000 || e.code === 3000) {  // SDK not existent or RPC worker not connected || Authentication error
            let _msg = e.reason
            store.addMessage({
                "sender": {
                    "type": "bot",
                    "platform": "WS",
                },
                "stack": [{
                    "type": "message",
                    "payload": {
                        "content": _msg
                    },
                }],
                "stack_id": Math.random().toString(36).substring(7),
                "stack_group_id": Math.random().toString(36).substring(7),
                "last": true,
            });
            return;
        }
        if (plConversationId !== store.selectedPlConversationId)
            return;
        store.disconnected = true;
        setTimeout(function () {
            createConnection();
        }, 1000);
    };
}


async function initializeConversation() {
    if(store.previewMode)
        return

    if (store.initialSelectedPlConversationId) {
        await store.gatherConversations()
        if (store.conversation(store.initialSelectedPlConversationId)) {
            return await store.openConversation(store.initialSelectedPlConversationId);
        }
    }
    store.conversationClosed = false
    store.createNewConversation(store.initialSelectedPlConversationId);
}

function sendMessage(message) {
    if (!store.canSendMsg)
        return;

    if (!message)
        return

    store.messages.push(message);
    ws.send(JSON.stringify(message));
    store.scrollToBottom += 1;

    chatInput.value?.blur(); // Remove focus from the input
}

function sendMessagesToBeSent() {
    if(!store.canSendMsg) {
        setTimeout(() => sendMessagesToBeSent(), 1000)
        return
    }

    while (store.messagesToBeSent.length) {
        sendMessage(store.messagesToBeSent.pop());
    }
}

function resendMsg(msgId) {
    if (msgId === undefined)
        return;
    if (store.disconnected)
        return;
    store.deleteMsgsAfter(msgId)
    ws.send(JSON.stringify({reset: msgId}));
}

function sendToGTM(msg) {
    if (msg?.stack?.length && msg.stack[0].type === "gtm_tag") {
        if (window?.dataLayer)
            window.dataLayer.push(msg.stack[0].payload);
        else
            console.warn("GTM tag received but no dataLayer found")
    }
}

</script>
<style scoped lang="scss">
.chat-wrapper {
    font: $chatfaq-font-body-s;
    font-style: normal;
    position: absolute;
    height: 100%;
    width: 100%;
    display: flex;
    flex-direction: column;

    background-color: $chatfaq-color-chat-background-light;

    &.dark-mode {
        background-color: $chatfaq-color-chat-background-dark;
    }
    &.fit-to-parent {
        border: unset !important;
        border-radius: inherit !important;
        overflow: hidden;
    }

    &.stick-input-prompt {
        overflow: initial !important;
    }
}

.alert-message {
    margin-bottom: -16px;
    text-align: center;
    color: $chatfaq-color-alertMessage-text-light;

    &.dark-mode {
        color: $chatfaq-color-alertMessage-text-dark;
    }
}

.pulsating {
    animation-duration: 2s;
    animation-name: pulsating;
    animation-iteration-count: infinite;
    animation-direction: alternate;
}

@keyframes pulsating {
    0% {
        opacity: 100%;
    }
    50% {
        opacity: 30%;
    }
    100% {
        opacity: 100%;
    }
}

.fade-out {
    animation: shake 0.82s cubic-bezier(0.36, 0.07, 0.19, 0.97) both;
    transform: translate3d(0, 0, 0);
    visibility: hidden;
    opacity: 0;
    transition: visibility 0s 2s, opacity 2s linear;
}

.conversation-content {
    height: 100%;
    width: 100%;
    overflow-x: hidden;

    @include scroll-style();

    &.dark-mode {
        @include scroll-style($chatfaq-color-scrollBar-dark);
    }
    &.fit-to-parent-conversation-content {
        min-height: inherit;
    }
}

.merge-to-prev {
    display: none;
}
</style>
