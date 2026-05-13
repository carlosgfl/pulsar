"""
Pulsar — analysis GUI.
Launch with run_analysis.bat, or: streamlit run analyze_ui.py
"""

import configparser
import os
from datetime import date
from pathlib import Path

import anthropic
import streamlit as st

from analyze import (
    append_knowledge,
    build_initial_content,
    load_knowledge,
    load_screenshots,
    select_keyframes,
    split_response,
)

BASE_DIR = Path(__file__).parent


def get_client(cfg: configparser.ConfigParser) -> anthropic.Anthropic:
    api_key = (
        cfg.get('api', 'anthropic_api_key', fallback='').strip()
        or os.environ.get('ANTHROPIC_API_KEY', '')
    )
    if not api_key:
        st.error('Set anthropic_api_key in config.ini or the ANTHROPIC_API_KEY environment variable.')
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


def stream_into_chat(client: anthropic.Anthropic, messages: list[dict]) -> str:
    """Stream Claude's response inside the current st.chat_message context."""
    result = ''
    slot = st.empty()
    with client.messages.stream(
        model='claude-opus-4-7',
        max_tokens=4096,
        messages=messages,
    ) as stream:
        for chunk in stream.text_stream:
            result += chunk
            slot.markdown(result + '▌')
    slot.markdown(result)
    return result


def save_analysis(target_date: str, analysis: str):
    output_dir = BASE_DIR / 'logs' / target_date
    (output_dir / 'analysis.md').write_text(analysis, encoding='utf-8')
    if '```mermaid' in analysis:
        start = analysis.index('```mermaid') + 10
        end   = analysis.index('```', start)
        (output_dir / 'workflow.mmd').write_text(analysis[start:end].strip(), encoding='utf-8')


def reset_state():
    for k in ('phase', 'chat', 'api_messages', 'analysis', 'questions_section', 'target_date'):
        st.session_state.pop(k, None)


def init_state():
    defaults = {
        'phase':             'idle',    # idle | running | waiting | done
        'chat':              [],        # [{role, content}] — display history
        'api_messages':      [],        # full API conversation for follow-ups
        'analysis':          '',
        'questions_section': '',
        'target_date':       '',
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def main():
    st.set_page_config(page_title='Pulsar Analysis', layout='wide')
    st.title('Pulsar — Daily Analysis')
    init_state()

    cfg = configparser.ConfigParser()
    cfg.read(BASE_DIR / 'config.ini')

    # ── Sidebar ────────────────────────────────────────────────────────────
    with st.sidebar:
        selected_date = st.date_input('Date', value=date.today())
        target_date   = selected_date.strftime('%Y-%m-%d')

        analyze_btn = st.button(
            'Analyze',
            type='primary',
            use_container_width=True,
            disabled=st.session_state.phase in ('running', 'waiting', 'done'),
        )

        if st.session_state.phase == 'done':
            if st.button('New analysis', use_container_width=True):
                reset_state()
                st.rerun()

        knowledge = load_knowledge()
        if knowledge:
            st.divider()
            with st.expander('Knowledge base'):
                st.markdown(knowledge)

    # ── Main columns: gallery | chat ───────────────────────────────────────
    # Use analyzed date for the gallery once analysis is running/done
    gallery_date = st.session_state.target_date or target_date
    screenshots_dir = BASE_DIR / 'logs' / gallery_date / 'screenshots'
    shots = sorted(screenshots_dir.glob('*.jpg')) if screenshots_dir.exists() else []

    col_gallery, col_chat = st.columns([1, 2], gap='medium')

    with col_gallery:
        st.subheader(f'Screenshots — {gallery_date}')
        if shots:
            for shot in shots:
                parts     = shot.stem.split('__')
                time_lbl  = parts[0].replace('-', ':')
                app_lbl   = parts[1] if len(parts) > 1 else ''
                title_lbl = (parts[2][:35] + '…' if len(parts[2]) > 35 else parts[2]) if len(parts) > 2 else ''
                st.image(str(shot), caption=f'{time_lbl}  {app_lbl}  {title_lbl}', use_container_width=True)
        else:
            st.info('No screenshots for this date.')

    with col_chat:
        st.subheader('Chat')

        # Replay stored history
        for msg in st.session_state.chat:
            with st.chat_message(msg['role']):
                st.markdown(msg['content'])

        # ── Trigger: Analyze button ─────────────────────────────────────
        if analyze_btn and st.session_state.phase == 'idle':
            if not shots:
                st.error(f'No screenshots found for {target_date}.')
            else:
                keyframe_count = cfg.getint('analysis', 'keyframes_per_day', fallback=30)
                screenshots    = load_screenshots(screenshots_dir)
                keyframes      = select_keyframes(screenshots, keyframe_count)

                st.session_state.target_date = target_date
                st.session_state.phase       = 'running'

                with st.chat_message('assistant'):
                    client   = get_client(cfg)
                    messages = [{'role': 'user', 'content': build_initial_content(keyframes, target_date, knowledge)}]
                    response = stream_into_chat(client, messages)

                analysis, questions = split_response(response)
                st.session_state.api_messages      = messages + [{'role': 'assistant', 'content': response}]
                st.session_state.analysis          = analysis
                st.session_state.questions_section = questions

                # Store for history replay — show questions inline so user sees them
                display = analysis + ('\n\n---\n\n' + questions if questions else '')
                st.session_state.chat.append({'role': 'assistant', 'content': display})

                if questions:
                    st.session_state.phase = 'waiting'
                else:
                    save_analysis(target_date, analysis)
                    st.session_state.phase = 'done'
                st.rerun()

        # ── Phase: waiting for clarification ────────────────────────────
        if st.session_state.phase == 'waiting':
            answer = st.chat_input("Answer Claude's questions above…")
            if answer:
                st.session_state.chat.append({'role': 'user', 'content': answer})

                api_msgs = st.session_state.api_messages + [{
                    'role': 'user',
                    'content': (
                        f'Clarifications:\n{answer}\n\n'
                        'Now produce the final complete analysis incorporating these answers. '
                        'Do not include a Questions section.'
                    ),
                }]

                with st.chat_message('assistant'):
                    final = stream_into_chat(get_client(cfg), api_msgs)

                st.session_state.chat.append({'role': 'assistant', 'content': final})
                append_knowledge(st.session_state.target_date, st.session_state.questions_section, answer)
                save_analysis(st.session_state.target_date, final)
                st.session_state.analysis = final
                st.session_state.phase    = 'done'
                st.rerun()

        elif st.session_state.phase == 'done':
            st.chat_input('Analysis complete.', disabled=True)
        elif st.session_state.phase == 'idle':
            st.chat_input('Click Analyze to start…', disabled=True)


if __name__ == '__main__':
    main()
