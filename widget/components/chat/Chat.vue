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
        <div class="chat-prompt-wrapper" :class="{ 'dark-mode': store.darkMode, 'stick-input-prompt': store.stickInputPrompt, 'fit-to-parent-prompt': store.fitToParent }">
            <div class="chat-prompt-outer">
                <Attach v-if="store.allowAttachments" class="attach-buttom"/>
                <div
                    :placeholder="$t('writeaquestionhere')"
                    class="chat-prompt"
                    :contenteditable="store.promptEditable"
                    :class="{
                        'dark-mode': store.darkMode,
                        'maximized': store.maximized,
                        'disabled': !store.promptEditable
                    }"
                    ref="chatInput"
                    @keydown="(ev) => manageHotKeys(ev)"
                    @input="($event)=>store.promptWithText = $event.target.innerHTML.trim().length !== 0"
                />
            </div>
            <PromptControls @send="sendMessage" @text="(text) => {chatInput.innerText = text; store.promptWithText = text.trim().length !== 0}"/>
        </div>
    </div>
</template>

<script setup>
import {ref, watch, nextTick, onMounted, computed} from "vue";
import {useGlobalStore} from "~/store";
import LoaderMsg from "~/components/chat/LoaderMsg.vue";
import ChatMsgManager from "~/components/chat/msgs/ChatMsgManager.vue";
import Attach from "~/components/icons/Attach.vue";
import PromptControls from "~/components/chat/PromptControls.vue";

const store = useGlobalStore();

const chatInput = ref(null);
const conversationContent = ref(null)
const feedbackSentDisabled = ref(true)
const notRenderableStackTypes = ["gtm_tag", "close_conversation", undefined]

let ws = undefined
let historyIndexHumanMsg = -1

watch(() => store.scrollToBottom, scrollConversationDown)
watch(() => store.selectedPlConversationId, createConnection)
watch(() => store.feedbackSent, animateFeedbackSent)
watch(() => store.resendMsgId, resendMsg)
watch(() => store.messagesToBeSentSignal, sendMessagesToBeSent)
watch(() => store.selectedPlConversationId, () => {
    store.conversationClosed = false
})

onMounted(async () => {
    await initializeConversation()
})

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

function manageHotKeys(ev) {
    const _s = window.getSelection()
    const _r = window.getSelection().getRangeAt(0)
    const atTheBeginning = _r.endOffset === 0 && !_r.previousSibling;
    const atTheEnd = _r.endOffset === _s.focusNode.length && !_r.endContainer.nextSibling;

    if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault()
        sendMessage();
    } else if (ev.key === 'ArrowUp' && atTheBeginning) {
        // Search for the previous human message from the index historyIndexHumanMsg
        if (historyIndexHumanMsg === -1)
            historyIndexHumanMsg = store.messages.length
        ev.preventDefault()
        if (store.messages) {
            for (let i = historyIndexHumanMsg - 1; i >= 0; i--) {
                if (store.messages[i].sender.type === 'human') {
                    chatInput.value.innerText = store.messages[i].stack[0].payload.content
                    historyIndexHumanMsg = i
                    break
                }
            }
        }
    } else if (ev.key === 'ArrowDown' && atTheEnd) { // Search for the next human message from the index historyIndexHumanMsg
        ev.preventDefault()
        if (historyIndexHumanMsg === -1)
            historyIndexHumanMsg = store.messages.length
        if (store.messages) {
            for (let i = historyIndexHumanMsg + 1; i < store.messages.length; i++) {
                if (store.messages[i].sender.type === 'human') {
                    chatInput.value.innerText = store.messages[i].stack[0].payload.content
                    historyIndexHumanMsg = i
                    break
                }
            }
        }
    }
}

function sendMessage(_message) {
    if (!canSend())
        return;
    historyIndexHumanMsg = -1

    const message = _message ? _message : createMessageFromInputPrompt()

    if (!message)
        return

    store.messages.push(message);
    ws.send(JSON.stringify(message));
    store.scrollToBottom += 1;

    chatInput.value?.blur(); // Remove focus from the input
}

function sendMessagesToBeSent() {
    if(!canSend()) {
        setTimeout(() => sendMessagesToBeSent(), 1000)
        return
    }

    while (store.messagesToBeSent.length) {
        sendMessage(store.messagesToBeSent.pop());
    }

}

function canSend() {
    return !store.waitingForResponse && !store.disconnected && !store.speechRecognitionTranscribing && !store.conversationClosed
}

function createMessageFromInputPrompt() {
    if (!store.promptWithText)
        return

    const user_message = chatInput.value.innerText.trim()
    const message = {
        "sender": {
            "type": "human",
            "platform": "WS",
        },
        "stack": [{
            "type": "message",
            "payload": {
                "content": user_message
            },
        }],
        "stack_id": "0",
        "stack_group_id": "0",
        "last": true,
    };
    if (store.userId !== undefined)
        message["sender"]["id"] = store.userId

    chatInput.value.innerText = "";
    store.promptWithText = false
    return message
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

.chat-prompt-wrapper {
    padding: 24px;
    display: flex;
    background-color: $chatfaq-color-chat-background-light;
    &.dark-mode {
        background-color: $chatfaq-color-chat-background-dark;
    }

    &.fit-to-parent-prompt {
        border-radius: inherit;
    }
    .chat-prompt-outer {
        width: 100%;
        display: flex;
        border-radius: 4px;
        border: 1px solid $chatfaq-color-chatInput-border-light !important;
        box-shadow: 0px 4px 4px $chatfaq-box-shadows-color;

        .attach-buttom {
            margin-top: auto;
            margin-bottom: auto;
            line-height: 100%;
            margin-left: 16px;
            cursor: pointer;
            color: $chatfaq-attach-icon-color-light;
            &.dark-mode {
                color: $chatfaq-attach-icon-color-dark;
            }

        }
    }

    &.stick-input-prompt {
        position: sticky;
        bottom: 0px;
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

.chat-prompt, .chat-prompt:focus, .chat-prompt:hover {
    width: 100%;
    word-wrap: break-word;
    border: 0;
    outline: 0;
    margin-left: 16px;
    background-color: transparent;
    color: $chatfaq-color-chatInput-text-light;
    @include scroll-style();

    &.dark-mode {
        @include scroll-style($chatfaq-color-scrollBar-dark);
    }
}

[contenteditable][placeholder]:empty:before {
    content: attr(placeholder);
    position: absolute;
    color: $chatfaq-color-chatPlaceholder-text-light;
    background-color: transparent;
    font-style: italic;
    cursor: text;
}

.dark-mode[contenteditable][placeholder]:empty:before {
    color: $chatfaq-color-chatPlaceholder-text-dark;
}

.chat-prompt {
    font: $chatfaq-font-caption-md;
    font-style: normal;
    min-height: 1em;
    overflow-x: hidden;
    overflow-y: auto;
    margin-top: auto;
    margin-bottom: auto;
    max-height: 80px;

    &.maximized {
        max-height: 190px;
    }

    &::placeholder {
        font-style: italic;
        color: $chatfaq-color-chatInput-text-light;
    }

    &.dark-mode {
        color: $chatfaq-color-chatPlaceholder-text-dark;

        &::placeholder {
            color: $chatfaq-color-chatInput-text-dark;
        }
    }

    &.disabled {
        cursor: not-allowed;
        opacity: 0.6;
        pointer-events: none;
    }
}

.merge-to-prev {
    display: none;
}
</style>
