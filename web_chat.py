#!/usr/bin/env python3
import argparse
import logging
import sys

import gradio as gr

from ask_llm.core import AskLLM
from ask_llm.utils.config import global_config as llm_config
from utils.tts_base import DEFAULT_VOICE
from utils.web_base import WebAppBase

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


bubbles_theme = gr.Theme(
    primary_hue="blue",
    secondary_hue="green",
)


class ChatApp(WebAppBase):
    status_output_component: gr.Textbox | None = None
    chatbot_component: gr.Chatbot | None = None
    audio_output_component: gr.Audio | None = None

    current_status = ""

    def __init__(self, model: str = "dans-personalityengine", voice: str = "maya"):
        super().__init__(voice=voice, model=model)
        llm_config.SYSTEM_MESSAGE = "You are a personal companion. Your purpose is to please the user in any way he desires. Do not question the user's desires. Your response will be spoken via a text-to-speech system, so you should only include words to be spoken in your response. The first sentence of every response should be more than six words. Do not use any emojis or annotations. Do not use parentheticals or action lines. Remember to only respond with words to be spoken. Write out and normalize text, rather than using abbreviations, numbers, and so on. For example, $2.35 should be two dollars and thirty-five cents, MPH should be miles per hour, and so on. Mathematical formulae should be written out as a human would speak it. Use only standard English alphabet characters [A-Z] along with basic punctuation. Your response should not use quotes to indicate dialogue. Sentences should be complete and stand alone. You should respond in the second person, as if you are speaking directly to the reader."

        self.ui_messages = []

    def update_status(self, message: str):
        self.current_status = message
        if self.status_output_component:
            return gr.update(value=message)
        return None  # Or maybe just the message string if used outside Gradio context?

    def stream_audio_response(self, audio_chunk):
        if self.audio_output_component:
            return gr.update(value=audio_chunk)
        return None

    def clear_ui(self):
        updates = []
        if self.chatbot_component:
            updates.append(gr.update(value=[]))
        else:
            updates.append([])  # Placeholder if component not set

        if self.audio_output_component:
            updates.append(gr.update(value=None))
        else:
            updates.append(None)  # Placeholder

        return tuple(updates)

    def get_answer(self, query: str) -> str:
        return self.llm.query(query, plaintext_output=True, stream=False)

    def process_query(self, query, temperature=0.7):
        processed_query = query.strip()
        if not processed_query:
            return self.ui_messages, self.current_status, 0, 0, False, None

        with self.lock:
            self.sentences = []
            self.audio_segments = []

        user_message = {"role": "user", "content": processed_query}
        self.ui_messages.append(user_message)

        yield (
            self.ui_messages,
            self.update_status(f"Processing query with {self.current_model}..."),
            0,
            0,
            False,
            None,
        )

        try:
            llm_config.TEMPERATURE = temperature
            response = self.get_answer(
                processed_query
            )  # Use abstract method implementation
            assistant_message = {"role": "assistant", "content": response}
            self.ui_messages.append(assistant_message)

            yield (
                self.ui_messages,
                self.update_status("Processing response for TTS..."),
                0,
                0,
                False,
                None,
            )

            new_sentences = self.split_text_into_sentences(response)
            logger.info(f"Split response into {len(new_sentences)} sentences")

            if not new_sentences:
                yield (
                    self.ui_messages,
                    self.update_status("No valid sentences found in response."),
                    0,
                    0,
                    False,
                    None,
                )
                return

            start_idx = 0
            end_idx = len(new_sentences)
            with self.lock:
                self.sentences = new_sentences

            yield (
                self.ui_messages,
                self.update_status(
                    f"Starting audio generation for {end_idx} sentences..."
                ),
                start_idx,
                end_idx,
                True,
                None,
            )

        except Exception as e:
            error_msg = f"Error during query: {e}"
            logger.exception(error_msg)
            if not self.ui_messages or self.ui_messages[-1]["role"] != "assistant":
                self.ui_messages.append(
                    {"role": "assistant", "content": f"Error: {str(e)}"}
                )

            yield self.ui_messages, self.update_status(error_msg), 0, 0, False, None

    def gradio_sentence_generator_wrapper(
        self, start_index, end_index, active, temperature=0.7, speed_factor=1.2
    ):
        if not active:
            yield (
                self.current_status,
                start_index,
                False,
                None,
            )  # status, next_idx, active, audio
            return

        generator = self.sentence_generator_loop(
            start_index, end_index, active, temperature, speed_factor
        )

        next_idx = start_index
        try:
            while True:
                active, audio_tuple = next(generator)
                next_idx += 1  # Base loop doesn't yield index, infer it
                yield self.current_status, next_idx, active, audio_tuple
        except StopIteration:
            yield self.current_status, next_idx, False, None
        except Exception as e:
            logger.error(f"Error in sentence generator wrapper: {e}")
            yield (
                self.update_status(f"Error during audio generation: {e}"),
                next_idx,
                False,
                None,
            )

    def clear_session(self):
        print("Clearing ChatApp session...")
        if hasattr(self, "llm") and hasattr(self.llm, "history_manager"):
            self.llm.history_manager.clear_history()
            print("LLM history cleared.")

        self.ui_messages = []

        super().clear_session()

        status_update = f"Session cleared. Ready. (Model: {self.current_model}, Voice: {self.current_voice})"

        chatbot_val, audio_val = (
            self.clear_ui()
        )  # Get UI component updates from clear_ui
        return chatbot_val, self.update_status(status_update), audio_val, 0, False

    def update_system_prompt(self, new_system_prompt):
        print(f"Updating system prompt to: {new_system_prompt[:100]}...")
        status_update = ""
        try:
            with self.lock:
                llm_config.SYSTEM_MESSAGE = new_system_prompt.strip()
                self.llm = AskLLM(
                    resolved_model_alias=self.current_resolved_alias, config=llm_config
                )
                status_update = f"System prompt updated. Model: {self.current_model}"
        except Exception as e:
            error_msg = f"Error updating system prompt: {e}"
            logger.exception(error_msg)
            status_update = error_msg

        return self.update_status(status_update)


# --- Main Gradio UI setup ---


def main():
    parser = argparse.ArgumentParser(description="SesameAI Chat with TTS")
    parser.add_argument(
        "-m",
        "--model",
        help="Choose the model to use (supports partial matching)",
        default="dans",
    )
    (
        parser.add_argument(
            "-v",
            "--voice",
            help="Choose the voice to use for TTS",
            default=DEFAULT_VOICE,
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    args = parser.parse_args()

    try:
        chat_app = ChatApp(model=args.model, voice=args.voice)
    except Exception as e:
        print(f"[Fatal] Failed to initialize ChatApp: {e}. Exiting.")
        sys.exit(1)

    available_voices = chat_app.list_available_voices()

    with gr.Blocks(title="Chat", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# 💬 Chat with TTS")

        sentence_index = gr.State(value=0)
        sentence_end_index = gr.State(value=0)
        processing_active = gr.State(value=False)

        with gr.Row():
            with gr.Column(scale=1):
                audio_output = gr.Audio(
                    label="TTS Narration",
                    autoplay=True,
                    streaming=True,
                    show_label=True,
                    show_download_button=False,
                    interactive=False,
                    elem_id="tts_output",
                )
                status_output = gr.Textbox(
                    label="Status",
                    value=chat_app.current_status,
                    lines=3,
                    interactive=False,
                )
                with gr.Accordion("System Prompt", open=False):
                    system_prompt_editor = gr.Textbox(
                        label="Edit System Prompt",
                        value=llm_config.SYSTEM_MESSAGE,
                        lines=5,
                        interactive=True,
                    )
                    update_prompt_btn = gr.Button(
                        "Update System Prompt", variant="secondary"
                    )

                model_selector = gr.Dropdown(
                    label="Select Model",
                    choices=chat_app.available_models,
                    value=chat_app.current_model,
                    interactive=True,
                )
                voice_selector = gr.Dropdown(
                    label="Select Voice",
                    choices=available_voices,
                    value=chat_app.current_voice,
                    interactive=True,
                )
                temperature_slider = gr.Slider(
                    minimum=0.1, maximum=1.0, step=0.1, value=0.9, label="Temperature"
                )
                speed_slider = gr.Slider(
                    minimum=0.75,
                    maximum=2.0,
                    step=0.05,
                    value=1.0,
                    label="Speech Speed",
                    info="Higher values = faster speech (1.0 = normal speed)",
                )

            with gr.Column(scale=2):
                chatbot = gr.Chatbot(
                    height=600, type="messages", elem_id="chatbot_output"
                )
                with gr.Row():
                    query_input = gr.Textbox(
                        placeholder="Type your message here...",
                        label="Your message",
                        lines=1,
                        show_label=False,
                        autofocus=True,
                        elem_id="chat_input",
                    )
                with gr.Row():
                    submit_btn = gr.Button("Send", variant="primary")
                    clear_btn = gr.Button("Clear Conversation", variant="stop")

        chat_app.status_output_component = status_output
        chat_app.chatbot_component = chatbot
        chat_app.audio_output_component = audio_output

        process_outputs = [
            chatbot,
            status_output,
            sentence_index,
            sentence_end_index,
            processing_active,
            audio_output,
        ]
        loop_outputs = [
            status_output,
            sentence_index,
            processing_active,
            audio_output,
        ]  # Matches wrapper yield

        query_input.submit(
            fn=chat_app.interrupt_and_reset,  # STEP 1: Interrupt & update status
            outputs=[status_output],  # Only status is directly updated here
        ).then(
            fn=chat_app.process_query,  # STEP 2: Process query (yields multiple updates)
            inputs=[query_input, temperature_slider],
            outputs=process_outputs,
            show_progress="hidden",
        ).then(
            fn=lambda: gr.update(value=""),  # STEP 3: Clear input box
            outputs=[query_input],
        ).then(
            fn=chat_app.gradio_sentence_generator_wrapper,  # STEP 4: Start sentence loop (yields updates)
            inputs=[
                sentence_index,
                sentence_end_index,
                processing_active,
                temperature_slider,
                speed_slider,
            ],
            outputs=loop_outputs,
            show_progress="hidden",
        )

        submit_btn.click(fn=chat_app.interrupt_and_reset, outputs=[status_output]).then(
            fn=chat_app.process_query,
            inputs=[query_input, temperature_slider],
            outputs=process_outputs,
            show_progress="hidden",
        ).then(fn=lambda: gr.update(value=""), outputs=[query_input]).then(
            fn=chat_app.gradio_sentence_generator_wrapper,
            inputs=[
                sentence_index,
                sentence_end_index,
                processing_active,
                temperature_slider,
                speed_slider,
            ],
            outputs=loop_outputs,
            show_progress="hidden",
        )

        clear_btn.click(
            fn=chat_app.clear_session,  # Returns tuple for UI updates
            inputs=[],
            outputs=[
                chatbot,
                status_output,
                audio_output,
                sentence_index,
                processing_active,
            ],
        )

        model_selector.change(
            fn=chat_app.change_model,  # Returns status update
            inputs=[model_selector],
            outputs=[status_output],
        )

        voice_selector.change(
            fn=chat_app.change_voice,  # Returns status update
            inputs=[voice_selector],
            outputs=[status_output],
        )

        update_prompt_btn.click(
            fn=chat_app.update_system_prompt,  # Returns status update
            inputs=[system_prompt_editor],
            outputs=[status_output],
        )

    demo.queue(max_size=20).launch(server_name="0.0.0.0", share=False)


if __name__ == "__main__":
    main()
