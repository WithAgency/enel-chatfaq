<template>
    <div v-if="!store.speechRecognitionTranscribing"
         :class="{'dark-mode': store.darkMode}"
         class="prompt-right-button"
         @click="() => {if(activeSend) {emit('send')}}"
    >
        <Send class="chat-prompt-button send" :class="{'dark-mode': store.darkMode, 'active': activeSend}"/>
    </div>
    <div v-else-if="store.speechRecognition" :class="{'dark-mode': store.darkMode}" class="prompt-right-button"
         @click="() => {
             if (activeMicro)
                 if (sttPhrase && sttPhrase.started){
                     speechRecognitionPhraseActivated = true
                     sttPhrase.stop()
                 }
                 stt.start()
         }">
        <div v-if="store.speechRecognitionTranscribing" class="micro-anim-elm has-scale-animation"></div>
        <div v-if="store.speechRecognitionTranscribing" class="micro-anim-elm has-scale-animation has-delay-short"></div>
        <Microphone :class="{'active': activeMicro, 'dark-mode': store.darkMode}" class="chat-prompt-button micro" />
    </div>
</template>

<script setup>

import {ref, watch, onMounted, computed} from "vue";
import Microphone from "~/components/icons/Microphone.vue";
import Send from "~/components/icons/Send.vue";
import {useGlobalStore} from "~/store";
import beepAudio from '~/assets/audio/beep.mp3';
import beepOutAudio from '~/assets/audio/beepOut.mp3';

const store = useGlobalStore();

const stt = ref(null)
const sttPhrase = ref(null)
const speechRecognitionPhraseActivated = ref(false)

const beepAudioPlayer = new Audio(beepAudio);
const beepOutAudioPlayer = new Audio(beepOutAudio);

// ---------------------------------------- WATCHS & COMPUTES ----------------------------------------

watch(() => store.selectedPlConversationId, () => {
    store.conversationClosed = false
})

const activeSend = computed(() => {
    return !store.speechRecognitionTranscribing && !store.waitingForResponse && !store.disconnected
})

const activeMicro = computed(() => {
    return store.speechRecognition && !store.speechRecognitionTranscribing
})

const emit = defineEmits(['send', 'text'])

// ---------------------------------------- INITIALIZATIONS ----------------------------------------

onMounted(async () => {
    if (!store.speechRecognition)
        return

    initSTT();
    if (store.speechRecognitionPhraseActivation)
        initSTTPhrase();
    else if (store.speechRecognitionAlwaysOn)
        stt.value.start();

})

function _initSTT(continuous = false) {
    const sr = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
    sr.lang = store.speechRecognitionLang;
    sr.continuous = continuous;
    sr.interimResults = true;
    sr.maxAlternatives = 1;

    sr.addEventListener("audioend", sr.stop)
    sr.addEventListener("soundend", sr.stop)
    sr.addEventListener("speechend", sr.stop)

    sr.onerror = (event) => {
        console.log('Error occurred in the speech recognition:', event)
        // Don't restart if we got a not-allowed error, usually because the user has not granted permission
        if (event.error === 'not-allowed') {
            store.speechRecognition = false;  // Disable speech recognition entirely
            store.speechRecognitionAlwaysOn = false;  // Ensure we don't retry
            return;
        }
    }
    sr.gatherText = (event) => {
        let final = "";
        let interim = "";

        for (let i = 0; i < event.results.length; ++i) {
            if (event.results[i].final) {
                final += event.results[i][0].transcript;
            } else {
                interim += event.results[i][0].transcript;
            }
        }
        if (final.length)
            return final
        return interim
    }

    return sr
}

function initSTT() {
    stt.value = _initSTT();
    stt.value.addEventListener("start", () => {
        store.speechRecognitionTranscribing = true;

        if (store.speechRecognitionBeep)
            beepAudioPlayer.play();
    })

    stt.value.onresult = (event) => {
        const text = stt.value.gatherText(event)
        emit('text', text)
        if (window.speechDebug)
            console.log(`%c ${text} -> %c ${trimFromActivationPhraseForwards(text)} `, "color: #007700", "color: #00FF00");
    }
    stt.value.onend = () => {
        store.speechRecognitionTranscribing = false;

        if (store.speechRecognitionBeep)
            beepOutAudioPlayer.play();

        if (store.speechRecognitionAutoSend)
            emit('send')

        if (store.speechRecognitionPhraseActivation)
            sttPhrase.value.start();
    }
}

function initSTTPhrase() {
    sttPhrase.value = _initSTT();
    sttPhrase.value.started = false
    sttPhrase.value.addEventListener("start", () => sttPhrase.value.started = true)

    sttPhrase.value.onresult = (event) => {
        const text = sttPhrase.value.gatherText(event)

        if (!speechRecognitionPhraseActivated.value && !store._speechRecognitionTranscribing && store.speechRecognitionPhraseActivation) {
            if (window.speechDebug)
                console.log(`%c ${text} `, 'color: #66ccff');

            if (matchActivationPhrase(text)) {
                speechRecognitionPhraseActivated.value = true
                sttPhrase.value.stop()
            }
        }
    }
    sttPhrase.value.onend = () => {

        if(speechRecognitionPhraseActivated.value) { // that means we programmatically ended the SR because we detected the activation phrase
            speechRecognitionPhraseActivated.value = false
            stt.value.start();
        } else {
            sttPhrase.value.start();
        }
    }
    sttPhrase.value.start();
}

// ---------------------------------------- UTILS ----------------------------------------
function matchActivationPhrase(text) {
    return text.toLowerCase().includes(store.speechRecognitionPhraseActivation.toLowerCase())
}

function trimFromActivationPhraseForwards(text) {
    if (!store.speechRecognitionPhraseActivation)
        return text
    if (text.toLowerCase().indexOf(store.speechRecognitionPhraseActivation.toLowerCase()) > -1)
        return text.substring(text.toLowerCase().indexOf(store.speechRecognitionPhraseActivation.toLowerCase()) + store.speechRecognitionPhraseActivation.length)
    return text
}

</script>

<style scoped lang="scss">

.prompt-right-button {
    position: relative;
    flex: 0 0 40px;
    height: 40px;
    margin-left: 8px;
    border-radius: 4px;
    display: flex;
    cursor: pointer;
    background-color: $chatfaq-prompt-button-background-color-light;
    &.dark-mode {
        background-color: $chatfaq-prompt-button-background-color-dark;
    }

    &.disabled {
        cursor: not-allowed;
        opacity: 0.6;
        pointer-events: none;
    }
}

.chat-prompt-button, .chat-prompt-button:focus, .chat-prompt-button:hover {
    cursor: pointer;
    height: 16px;
    align-self: end;
    opacity: 0.6;
    margin: auto;
    z-index: 1;

    &.send {
        color: $chatfaq-send-icon-color-light;
        &.dark-mode {
            color: $chatfaq-send-icon-color-dark;
        }
    }
    &.micro {
        color: $chatfaq-microphone-icon-color-light;
        &.dark-mode {
            color: $chatfaq-microphone-icon-color-dark;
        }
    }

    &.active {
        opacity: 1;
    }
}

.micro-anim-elm {
    position: absolute;
    background: $chatfaq-prompt-button-background-color-light;

    &.dark-mode {
        background: $chatfaq-prompt-button-background-color-dark;
    }

    border-radius: 4px;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    height: 100%;
    width: 100%;
}


.has-scale-animation {
    animation: smallScale 1.7s infinite
}

.has-delay-short {
    animation-delay: 0.5s
}

@keyframes fadeIn {
    0% {
        opacity: 0;
    }
    100% {
        opacity: 1;
    }
}

@keyframes smallScale {
    0% {
        transform: scale(1);
        opacity: 0.7;
    }
    100% {
        transform: scale(1.5);
        opacity: 0;
    }
}


</style>
