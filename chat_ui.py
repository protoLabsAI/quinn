"""
Reusable Gradio chat UI for protoResearcher.

Provides a clean chat interface with a research-themed design.
Includes settings sidebar with model/tools panels.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import gradio as gr

CLEAN_CSS = """
    footer { display: none !important; }
    .prose { overflow: hidden !important; max-height: 3em !important; }
    .built-with { display: none !important; }
    button.copy-btn, button.like, button.dislike,
    .message-buttons-left, .message-buttons-right,
    .bot .message-buttons, .user .message-buttons,
    .copy-button, .action-button,
    [data-testid="copy-button"], [data-testid="like"], [data-testid="dislike"],
    .message-wrap .icon-button, .message-wrap .icon-buttons,
    .chatbot .icon-button, .chatbot .icon-buttons,
    .chatbot .action-buttons,
    .chatbot button[aria-label="Copy"], .chatbot button[aria-label="Like"],
    .chatbot button[aria-label="Dislike"], .chatbot button[aria-label="Retry"],
    .badge-wrap, .chatbot .badge-wrap,
    span.chatbot-badge, .chatbot-badge,
    .built-with-gradio, a[href*="gradio.app"],
    .show-api, button.show-api, #show-api-btn,
    [class*="show-api"], .api-docs-btn {
        display: none !important;
    }
"""

# protoResearcher dark theme — emerald/teal accents, research-dark backgrounds
RESEARCHER_DARK_CSS = """
    html { color-scheme: dark !important; }

    body, .gradio-container, .main, .wrap, .gap, #component-0 {
        background: #0a0f14 !important;
    }

    .block, .form, .panel, .tabitem, .sidebar, .sidebar-content {
        background: #0f1620 !important;
        border-color: rgba(20, 184, 166, 0.2) !important;
    }

    input, textarea, .gr-input, .gr-textarea,
    [class*="input-"], [class*="textbox"] {
        background: #162030 !important;
        color: #e2e8f0 !important;
        border-color: rgba(20, 184, 166, 0.35) !important;
    }
    input:focus, textarea:focus {
        border-color: #14b8a6 !important;
        box-shadow: 0 0 0 2px rgba(20, 184, 166, 0.25) !important;
    }

    button.primary, .btn-primary, [class*="primary"][class*="btn"] {
        background: #14b8a6 !important;
        border-color: #14b8a6 !important;
        color: #fff !important;
    }
    button.primary:hover, .btn-primary:hover {
        background: #0d9488 !important;
        border-color: #0d9488 !important;
    }

    button.secondary, .btn-secondary {
        background: #162030 !important;
        border-color: rgba(20, 184, 166, 0.35) !important;
        color: #5eead4 !important;
    }
    button.secondary:hover { background: #1a3040 !important; }

    .message.bot, .message.assistant,
    [data-testid="bot"], [class*="bot-message"] {
        background: #0f1f2e !important;
        border-left: 3px solid #14b8a6 !important;
        color: #e2e8f0 !important;
    }

    .message.user, [data-testid="user"], [class*="user-message"] {
        background: #1a2a3a !important;
        color: #e2e8f0 !important;
    }

    .markdown, .prose, .gr-markdown, p, span, label, li {
        color: #e2e8f0 !important;
    }

    h1, h2, h3, .markdown h1, .markdown h2, .markdown h3 {
        color: #5eead4 !important;
    }

    .accordion-header, [class*="accordion"] button {
        background: #0f1620 !important;
        color: #5eead4 !important;
        border-color: rgba(20, 184, 166, 0.2) !important;
    }

    .dropdown, select {
        background: #162030 !important;
        color: #e2e8f0 !important;
        border-color: rgba(20, 184, 166, 0.35) !important;
    }

    code, pre, .gr-code, [class*="code-"] {
        background: #060a10 !important;
        border-color: rgba(20, 184, 166, 0.2) !important;
        color: #99f6e4 !important;
    }

    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #0a0f14; }
    ::-webkit-scrollbar-thumb {
        background: rgba(20, 184, 166, 0.4);
        border-radius: 3px;
    }
    ::-webkit-scrollbar-thumb:hover { background: #14b8a6; }

    .tab-nav button.selected, [class*="tab"][aria-selected="true"] {
        border-bottom-color: #14b8a6 !important;
        color: #5eead4 !important;
    }

    .sidebar-toggle, [class*="sidebar-toggle"] {
        background: #14b8a6 !important;
        color: #fff !important;
    }
"""

RESEARCHER_PWA_HEAD = """
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="alternate icon" href="/static/favicon.svg">
<link rel="apple-touch-icon" href="/static/icons/icon-192.svg">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#14b8a6">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="protoResearcher">
<script>
if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
        navigator.serviceWorker
            .register('/sw.js', { scope: '/' })
            .then(function (reg) {
                console.log('[protoResearcher] SW registered:', reg.scope);
            })
            .catch(function (err) {
                console.warn('[protoResearcher] SW registration failed:', err);
            });
    });
}
</script>
"""

ChatFn = Callable[[str, str], Awaitable[list[dict]]]
StreamingChatFn = Callable[..., Any]  # generator function
SettingsCallbacks = dict[str, Any]


def create_chat_app(
    chat_fn: ChatFn,
    title: str = "Chat",
    subtitle: str = "",
    placeholder: str = "Type a message...",
    chat_height: str = "80vh",
    footer_html: str = '<div style="text-align:center; padding:8px 0; opacity:0.5; font-size:12px;">built with <a href="https://protolabs.studio" target="_blank" rel="noopener" style="color:inherit;">protolabs.studio</a></div>',
    extra_css: str = "",
    settings: SettingsCallbacks | None = None,
    pwa: bool = True,
    streaming_chat_fn: StreamingChatFn | None = None,
) -> gr.Blocks:
    _theme = gr.themes.Soft(primary_hue="teal", neutral_hue="slate")
    _css = CLEAN_CSS + RESEARCHER_DARK_CSS + extra_css
    _head = RESEARCHER_PWA_HEAD if pwa else ""

    def _build() -> gr.Blocks:
        with gr.Blocks(
            title=title.replace("*", "").strip(),
            theme=_theme,
            css=_css,
            head=_head,
            analytics_enabled=False,
        ) as app:
            session_id = gr.State("default")

            header_text = f"**{title}**"
            if subtitle:
                header_text += f" &nbsp; {subtitle}"

            header_md = gr.Markdown(header_text)

            chatbot = gr.Chatbot(height=chat_height, show_label=False)

            with gr.Row():
                txt = gr.Textbox(
                    placeholder=placeholder, show_label=False,
                    scale=9, container=False,
                )
                send_btn = gr.Button("Send", variant="primary", scale=1, min_width=80)

            with gr.Row():
                clear_btn = gr.Button("Clear", size="sm", variant="secondary")
                new_btn = gr.Button("New Session", size="sm", variant="secondary")

            if footer_html:
                gr.HTML(footer_html)

            # --- Settings sidebar ---
            if settings:
                with gr.Sidebar(label="Settings", open=False, position="right"):
                    with gr.Accordion("Tools", open=False):
                        tools_display = gr.Markdown("Loading...")
                        refresh_tools_btn = gr.Button("Refresh", size="sm")

                    with gr.Accordion("Model", open=False):
                        model_display = gr.Markdown("Loading...")
                        provider_dropdown = gr.Dropdown(
                            label="Provider", choices=[], interactive=True,
                        )
                        switch_status = gr.Markdown("")
                        refresh_model_btn = gr.Button("Refresh", size="sm")

                    if "get_knowledge_stats" in settings:
                        with gr.Accordion("Knowledge Base", open=False):
                            kb_display = gr.Markdown("Loading...")
                            refresh_kb_btn = gr.Button("Refresh", size="sm")

                    # --- Callbacks ---

                    def load_tools():
                        return settings["get_tools_list"]()

                    def load_model():
                        return settings["get_model_info"]()

                    def load_provider_choices():
                        choices = settings["get_provider_choices"]()
                        current = settings["get_current_provider"]()
                        return gr.update(choices=choices, value=current)

                    def switch_provider(choice):
                        return settings["switch_provider"](choice)

                    def load_subtitle():
                        return settings["get_subtitle"]()

                    app.load(fn=load_tools, outputs=[tools_display])
                    app.load(fn=load_model, outputs=[model_display])
                    app.load(fn=load_provider_choices, outputs=[provider_dropdown])

                    refresh_tools_btn.click(fn=load_tools, outputs=[tools_display])
                    refresh_model_btn.click(
                        fn=load_model, outputs=[model_display]
                    ).then(fn=load_provider_choices, outputs=[provider_dropdown])

                    provider_dropdown.change(
                        fn=switch_provider, inputs=[provider_dropdown], outputs=[switch_status],
                    ).then(fn=load_model, outputs=[model_display]).then(
                        fn=load_subtitle, outputs=[header_md],
                    )

                    if "get_knowledge_stats" in settings:
                        def load_kb_stats():
                            return settings["get_knowledge_stats"]()

                        app.load(fn=load_kb_stats, outputs=[kb_display])
                        refresh_kb_btn.click(fn=load_kb_stats, outputs=[kb_display])

            # --- Chat callbacks ---

            def add_user_message(message: str, history: list[dict]):
                if not message.strip():
                    return "", history, message
                history.append({"role": "user", "content": message})
                return "", history, message

            def get_response(history: list[dict], original_msg: str, sid: str):
                if not original_msg.strip():
                    return history, sid
                result = asyncio.run(chat_fn(original_msg, sid))
                for msg in result:
                    meta = msg.get("metadata", {})
                    if meta.get("_clear"):
                        return [], sid
                    if meta.get("_new"):
                        return [], secrets.token_hex(4)
                history.extend(result)
                return history, sid

            pending_msg = gr.State("")

            for trigger in [txt.submit, send_btn.click]:
                trigger(
                    fn=add_user_message,
                    inputs=[txt, chatbot],
                    outputs=[txt, chatbot, pending_msg],
                ).then(
                    fn=get_response,
                    inputs=[chatbot, pending_msg, session_id],
                    outputs=[chatbot, session_id],
                )

            clear_btn.click(fn=lambda: ([], "default"), outputs=[chatbot, session_id])
            new_btn.click(fn=lambda: ([], secrets.token_hex(4)), outputs=[chatbot, session_id])

        return app

    app = _build()

    _original_launch = app.launch

    def _launch(**kwargs):
        kwargs.setdefault("server_name", "0.0.0.0")
        if kwargs.pop("pwa", None) is not None:
            try:
                return _original_launch(**kwargs, pwa=True)
            except TypeError:
                pass
        return _original_launch(**kwargs)

    app.launch = _launch
    return app
