import asyncio
import atexit
from asyncio import wait_for
from datetime import datetime
from logging import DEBUG, Formatter, INFO, StreamHandler, basicConfig, getLogger
from os import environ
from pathlib import Path
from platform import system
from re import search as re_search
from threading import Lock
from time import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

import zendriver
from setup import LOG_DIR, SETTINGS


CHAT_URL = "https://chat.deepseek.com/"
SESSION_URL_TEMPLATE = "https://chat.deepseek.com/a/chat/s/{session_id}"
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
TEST_UI_FILE = BASE_DIR / "test.html"
TOKENIZER_DIR = BASE_DIR / "tokenizer" / "deepseek_v3_tokenizer"
CONNECTIVITY_PROMPT = "你是连通性测试器，请只回复：测试通畅✅"


def _load_dotenv() -> None:
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in environ:
            environ[key] = value


_load_dotenv()

SERVER_TOKEN = environ.get("DEEPSEEK_USER_TOKEN", "").strip()
SERVER_PORT = int(environ.get("PORT", "8000"))


def _parse_token_pool() -> list[str]:
    raw_values = []
    if environ.get("DEEPSEEK_USER_TOKENS"):
        raw_values.append(environ["DEEPSEEK_USER_TOKENS"])
    if SERVER_TOKEN:
        raw_values.append(SERVER_TOKEN)

    tokens: list[str] = []
    for raw in raw_values:
        normalized = raw.replace("\r", "\n").replace(",", "\n")
        for part in normalized.split("\n"):
            token = part.strip()
            if token and token not in tokens:
                tokens.append(token)
    return tokens


TOKEN_POOL = _parse_token_pool()


class CharacterStats:
    def __init__(self, enabled: bool, log_dir: Path) -> None:
        self.enabled = enabled
        self.log_dir = log_dir
        self._lock = Lock()
        self._exported = False
        self.total_requests = 0
        self.total_input_chars = 0
        self.total_output_chars = 0
        self.total_thinking_chars = 0

    def record(self, request_text: str, response_text: Optional[str], thinking_text: Optional[str]) -> None:
        if not self.enabled:
            return

        with self._lock:
            self.total_requests += 1
            self.total_input_chars += len(request_text or "")
            self.total_output_chars += len(response_text or "")
            self.total_thinking_chars += len(thinking_text or "")

    def export(self) -> Optional[Path]:
        if not self.enabled:
            return None

        with self._lock:
            if self._exported:
                return None

            self.log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            export_path = self.log_dir / f"char-count-{timestamp}.txt"
            total_model_output = self.total_output_chars + self.total_thinking_chars
            export_path.write_text(
                "\n".join(
                    [
                        "DeepSeek Proxy Character Statistics",
                        f"generated_at={datetime.now().isoformat(timespec='seconds')}",
                        f"requests={self.total_requests}",
                        f"total_input_chars={self.total_input_chars}",
                        f"total_output_chars={self.total_output_chars}",
                        f"total_thinking_chars={self.total_thinking_chars}",
                        f"total_model_output_chars={total_model_output}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            self._exported = True
            return export_path


CHARACTER_STATS = CharacterStats(
    enabled=SETTINGS.char_count_enabled,
    log_dir=LOG_DIR,
)


class TokenUsageCounter:
    def __init__(self, tokenizer_dir: Path) -> None:
        self.tokenizer_dir = tokenizer_dir
        self._tokenizer = None
        self._lock = Lock()
        self._load_error: Optional[str] = None

    def _load_tokenizer(self) -> Any:
        with self._lock:
            if self._tokenizer is not None:
                return self._tokenizer
            if self._load_error is not None:
                raise RuntimeError(self._load_error)

            try:
                from transformers import AutoTokenizer
            except Exception as exc:
                self._load_error = f"transformers import failed: {exc}"
                raise RuntimeError(self._load_error) from exc

            try:
                self._tokenizer = AutoTokenizer.from_pretrained(
                    str(self.tokenizer_dir),
                    trust_remote_code=True,
                )
            except Exception as exc:
                self._load_error = f"tokenizer load failed: {exc}"
                raise RuntimeError(self._load_error) from exc

            return self._tokenizer

    def count_text(self, text: Optional[str]) -> int:
        normalized = text or ""
        if not normalized:
            return 0

        tokenizer = self._load_tokenizer()
        return len(tokenizer.encode(normalized, add_special_tokens=False))

    def build_usage(
        self,
        request_text: Optional[str],
        thinking_text: Optional[str],
        response_text: Optional[str],
    ) -> dict[str, Any]:
        input_tokens = self.count_text(request_text)
        thinking_tokens = self.count_text(thinking_text)
        text_tokens = self.count_text(response_text)
        output_total = thinking_tokens + text_tokens
        return {
            "tokenizer": "deepseek_v3_tokenizer",
            "input": input_tokens,
            "thinking": thinking_tokens,
            "text": text_tokens,
            "output_total": output_total,
            "all_total": input_tokens + output_total,
        }


TOKEN_USAGE_COUNTER = TokenUsageCounter(TOKENIZER_DIR)


def _extract_request_text(payload: dict[str, Any]) -> str:
    request_text = payload.get("request")
    if isinstance(request_text, str) and request_text.strip():
        return request_text.strip()

    raise HTTPException(status_code=400, detail="Missing `request` field.")


def _extract_session_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None

    matched = re_search(r"/a/chat/s/([0-9a-fA-F-]+)", url)
    if not matched:
        return None
    return matched.group(1)


def _build_token_usage(
    request_text: Optional[str],
    thinking_text: Optional[str],
    response_text: Optional[str],
) -> Optional[dict[str, Any]]:
    try:
        return TOKEN_USAGE_COUNTER.build_usage(
            request_text=request_text,
            thinking_text=thinking_text,
            response_text=response_text,
        )
    except Exception as exc:
        getLogger("DeepSeekProxy").warning("Token usage calculation failed: %s", exc)
        return None


class DeepSeekProxyClient:
    def __init__(
        self,
        token: str,
        deepthink: bool = False,
        search: bool = False,
        expert_mode: bool = False,
        headless: bool = SETTINGS.effective_headless,
        verbose: bool = SETTINGS.effective_verbose,
    ) -> None:
        self.token = token
        self.headless = headless
        self.verbose = verbose
        self.default_deepthink = deepthink
        self.default_search = search
        self.default_expert_mode = expert_mode
        self._initialized = False
        self._busy = False

    async def initialize(self) -> None:
        self.logger = getLogger("DeepSeekProxy")
        self.logger.setLevel(DEBUG if SETTINGS.debug_mode_enabled else INFO)
        if not self.logger.handlers and self.verbose:
            handler = StreamHandler()
            handler.setFormatter(Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
            self.logger.addHandler(handler)
        self.logger.propagate = False

        if self._initialized:
            return

        if system() == "Linux" and "DISPLAY" not in environ:
            from pyvirtualdisplay.display import Display

            self.display = Display()
            self.display.start()

        self.browser = await zendriver.start(headless=self.headless)
        self._initialized = True
        self.logger.debug("Persistent browser initialized successfully.")

    async def prepare_request(
        self,
        session_id: Optional[str] = None,
        deepthink: Optional[bool] = None,
        search: Optional[bool] = None,
        expert_mode: Optional[bool] = None,
    ) -> None:
        if not self._initialized:
            await self.initialize()

        target_url = CHAT_URL if not session_id else SESSION_URL_TEMPLATE.format(session_id=session_id)
        current_deepthink = self.default_deepthink if deepthink is None else deepthink
        current_search = self.default_search if search is None else search
        current_expert_mode = self.default_expert_mode if expert_mode is None else expert_mode

        self.logger.debug(f"Navigating to: {target_url}")
        await self.browser.main_tab.get(target_url)

        if SETTINGS.cloudflare_wait_enabled:
            try:
                self.logger.debug(
                    "Verifying Cloudflare for up to %s second(s)...",
                    SETTINGS.cloudflare_wait_seconds,
                )
                await wait_for(
                    self.browser.main_tab.verify_cf(),
                    timeout=SETTINGS.cloudflare_wait_seconds,
                )
            except Exception:
                self.logger.debug(
                    "Cloudflare verification timed out or was skipped.",
                    exc_info=SETTINGS.debug_mode_enabled,
                )

        await self.browser.main_tab.evaluate(
            f"""
            localStorage.setItem('userToken', JSON.stringify({{value: '{self.token}', __version: '0'}}));
            localStorage.setItem('thinkingEnabled', JSON.stringify({{value: {str(current_deepthink).lower()}, __version: '2'}}));
            localStorage.setItem('searchEnabled', JSON.stringify({{value: {str(current_search).lower()}, __version: '0'}}));
            """,
            await_promise=True,
            return_by_value=True,
        )

        await self.browser.main_tab.reload()
        await asyncio.sleep(1)
        await self.browser.main_tab.wait_for("textarea", timeout=5)
        if current_expert_mode:
            await self._ensure_expert_mode_enabled()
        self.logger.debug("Request context prepared.")

    async def _ensure_expert_mode_enabled(self) -> None:
        try:
            state = await self.browser.main_tab.evaluate(
                """
                (() => {{
                    const control = document.querySelector('[data-model-type="expert"]');
                    if (!control) {{
                        return {{ found: false, active: false }};
                    }}

                    const nodes = [];
                    let current = control;
                    for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {{
                        nodes.push(current);
                    }}

                    const explicitState = nodes
                        .map((node) => {{
                            const values = [
                                node.getAttribute('aria-pressed'),
                                node.getAttribute('aria-selected'),
                                node.getAttribute('aria-checked'),
                                node.getAttribute('data-state'),
                                node.getAttribute('data-selected'),
                                node.getAttribute('data-active')
                            ];
                            return values.find((value) => value !== null);
                        }})
                        .find((value) => value !== undefined);

                    let active = false;
                    if (explicitState !== undefined) {{
                        active = ['true', 'checked', 'selected', 'active', 'on', 'open'].includes(String(explicitState).toLowerCase());
                    }} else {{
                        active = nodes.some((node) => /(^|\\s)(active|selected|checked|current)(\\s|$)/i.test(node.className || ''));
                    }}

                    if (!active) {{
                        control.click();
                        return {{ found: true, clicked: true, active_before: active }};
                    }}

                    return {{ found: true, clicked: false, active_before: active }};
                }})()
                """,
                await_promise=True,
                return_by_value=True,
            )
        except Exception:
            self.logger.debug("Failed to enable expert mode.", exc_info=SETTINGS.debug_mode_enabled)
            return

        if not state.get("found"):
            self.logger.debug("Expert mode control was not found on the page.")
            return

        if state.get("clicked"):
            await asyncio.sleep(0.5)
            self.logger.debug("Expert mode enabled.")
        else:
            self.logger.debug("Expert mode already enabled.")

    async def ask(self, request_text: str, timeout: int = 60) -> dict[str, Optional[str]]:
        if not self._initialized:
            raise RuntimeError("Client is not initialized.")

        before_state = await self._capture_response_state()

        textbox = await self.browser.main_tab.select("textarea")
        await textbox.send_keys(request_text)

        if not await self._submit_message():
            raise RuntimeError("Could not submit the message.")

        submission_confirmed = await self._wait_for_message_submission(request_text, before_state, timeout=15)
        if not submission_confirmed and self.verbose:
            self.logger.debug("Message submission could not be confirmed. Continuing to response wait anyway.")

        response_payload = await self._get_response(
            timeout=timeout,
            previous_response_count=before_state["count"],
            previous_response_text=before_state["last_text"],
        )
        if not response_payload or not response_payload.get("text"):
            raise RuntimeError("No response was captured.")

        current_url = await self.browser.main_tab.evaluate(
            "window.location.href",
            await_promise=True,
            return_by_value=True,
        )
        response_payload["session_id"] = _extract_session_id_from_url(current_url)
        return response_payload

    async def _submit_message(self) -> bool:
        try:
            send_buttons = await self.browser.main_tab.select_all(
                "div.ec4f5d61 div[role='button'][aria-disabled='false']"
            )
            for send_button in reversed(send_buttons):
                try:
                    await send_button.click()
                    return True
                except Exception:
                    continue
        except Exception:
            pass

        try:
            return bool(
                await self.browser.main_tab.evaluate(
                    """
                    (() => {
                        const composer = document.querySelector('div.ec4f5d61');
                        if (!composer) return false;
                        const buttons = Array.from(
                            composer.querySelectorAll("div[role='button'][aria-disabled='false'], button:not([disabled])")
                        );
                        const submitButton = buttons[buttons.length - 1];
                        if (!submitButton) return false;
                        submitButton.click();
                        return true;
                    })()
                    """,
                    await_promise=True,
                    return_by_value=True,
                )
            )
        except Exception:
            return False

    async def _capture_response_state(self) -> dict[str, Any]:
        try:
            return await self.browser.main_tab.evaluate(
                """
                (() => {
                    const texts = Array.from(document.querySelectorAll('.ds-markdown'))
                        .map((node) => (node.innerText || '').trim())
                        .filter(Boolean);
                    return {
                        count: texts.length,
                        last_text: texts.length ? texts[texts.length - 1] : ''
                    };
                })()
                """,
                await_promise=True,
                return_by_value=True,
            )
        except Exception:
            return {"count": 0, "last_text": ""}

    async def _wait_for_message_submission(self, request_text: str, previous_state: dict[str, Any], timeout: int) -> bool:
        end_time = time() + timeout
        normalized_request = request_text.strip()

        while time() < end_time:
            try:
                state = await self.browser.main_tab.evaluate(
                    """
                    (() => {
                        const textarea = document.querySelector('textarea');
                        const texts = Array.from(document.querySelectorAll('.ds-markdown'))
                            .map((node) => (node.innerText || '').trim())
                            .filter(Boolean);
                        const generating = Boolean(
                            document.querySelector('.ds-loading') ||
                            document.querySelector('[class*="loading"]') ||
                            document.querySelector('[class*="typing"]')
                        );
                        const submitButtons = Array.from(
                            document.querySelectorAll("div.ec4f5d61 div[role='button'], div.ec4f5d61 button")
                        );
                        const activeSubmitButtons = submitButtons.filter((node) => {
                            const disabled = node.getAttribute('aria-disabled');
                            return disabled !== 'true' && !node.hasAttribute('disabled');
                        });

                        return {
                            textarea_value: textarea ? textarea.value : null,
                            response_count: texts.length,
                            last_response_text: texts.length ? texts[texts.length - 1] : '',
                            generating,
                            active_submit_button_count: activeSubmitButtons.length
                        };
                    })()
                    """,
                    await_promise=True,
                    return_by_value=True,
                )
            except Exception:
                await asyncio.sleep(0.5)
                continue

            if state["textarea_value"] == "":
                return True
            if state["generating"]:
                return True
            if state["active_submit_button_count"] == 0:
                return True
            if state["response_count"] > previous_state["count"]:
                return True
            if state["last_response_text"] != previous_state["last_text"]:
                return True
            if normalized_request and state["textarea_value"] and state["textarea_value"].strip() != normalized_request:
                return True

            await asyncio.sleep(0.5)

        return False

    async def _get_response(
        self,
        timeout: int,
        previous_response_count: int,
        previous_response_text: str,
    ) -> Optional[dict[str, Optional[str]]]:
        end_time = time() + timeout
        stable_polls = 0
        last_seen_signature = None

        while time() < end_time:
            script = """
            (() => {
                function extractMarkdownText(element) {
                    let text = '';
                    Array.from(element.childNodes).forEach((node) => {
                        if (node.nodeType === Node.TEXT_NODE) {
                            const value = node.textContent.trim();
                            if (value) text += value + '\\n';
                            return;
                        }
                        if (node.nodeType !== Node.ELEMENT_NODE) return;
                        const tagName = node.tagName.toLowerCase();
                        if (tagName.startsWith('h')) {
                            const level = Number(tagName.slice(1)) || 1;
                            text += '#'.repeat(level) + ' ' + node.textContent.trim() + '\\n\\n';
                        } else if (tagName === 'pre') {
                            const codeText = node.textContent.trim().replace(/^\\s+/gm, '');
                            const language = node.querySelector('span.d813de27')?.textContent || 'text';
                            text += '```' + language + '\\n' + codeText + '\\n```\\n\\n';
                        } else if (tagName === 'li') {
                            text += '- ' + node.textContent.trim() + '\\n';
                        } else {
                            text += extractMarkdownText(node);
                        }
                    });
                    return text.trim();
                }

                function hasCompletionMarker(node) {
                    let current = node;
                    for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
                        const markers = Array.from(current.querySelectorAll('div.ds-flex'));
                        if (markers.some((marker) => /(^|\\s)_[a-z0-9]+(\\s|$)/i.test(marker.className || ''))) {
                            return true;
                        }
                    }
                    return false;
                }

                const previousCount = __PREVIOUS_COUNT__;
                const markdownRoots = Array.from(document.querySelectorAll('.ds-markdown'));
                const markdownTexts = markdownRoots.map((node) => extractMarkdownText(node)).filter(Boolean);
                const newTexts = markdownTexts.slice(previousCount);
                const fallbackTexts = markdownTexts.length ? [markdownTexts[markdownTexts.length - 1]] : [];
                const responseTexts = newTexts.length ? newTexts : fallbackTexts;
                const targetNode = markdownRoots[previousCount] || markdownRoots[markdownRoots.length - 1] || null;
                const generating = Boolean(document.querySelector('.ds-loading'));

                return {
                    count: markdownTexts.length,
                    response_texts: responseTexts,
                    thinking: responseTexts.length >= 2 ? responseTexts[0] : null,
                    text: responseTexts.length >= 2 ? responseTexts[1] : (responseTexts[0] || ''),
                    generating,
                    has_completion_marker: targetNode ? hasCompletionMarker(targetNode) : false
                };
            })()
            """.replace("__PREVIOUS_COUNT__", str(previous_response_count))

            state = await self.browser.main_tab.evaluate(
                script,
                await_promise=True,
                return_by_value=True,
            )

            has_new_response = (
                state["count"] > previous_response_count or
                (state["text"] and state["text"] != previous_response_text)
            )
            has_text = bool((state["text"] or "").strip())
            completed = state["has_completion_marker"] or not state["generating"]
            signature = "\n---\n".join(state.get("response_texts") or [])

            if has_new_response and has_text:
                if signature == last_seen_signature and completed:
                    stable_polls += 1
                else:
                    stable_polls = 0
                    last_seen_signature = signature

                if stable_polls >= 2 and completed:
                    return {
                        "text": state["text"],
                        "thinking": state["thinking"],
                    }

            await asyncio.sleep(1)

        return None

    async def close(self) -> None:
        browser = getattr(self, "browser", None)
        if browser is None:
            return

        for method_name in ("stop", "close", "aclose"):
            method = getattr(browser, method_name, None)
            if method is None:
                continue
            try:
                result = method()
                if asyncio.iscoroutine(result):
                    await result
                break
            except Exception:
                continue


app = FastAPI(title="DeepSeek Unofficial API Proxy")
app.state.client_pool = []
app.state.pool_lock = asyncio.Lock()
app.state.token_index = 0


def _resolve_timeout(payload: dict[str, Any]) -> int:
    if SETTINGS.fixed_timeout_enabled:
        return SETTINGS.fixed_timeout_seconds
    return int(payload.get("timeout", SETTINGS.fixed_timeout_seconds))


def _normalize_timeout(timeout: int) -> int:
    if SETTINGS.fixed_timeout_enabled:
        return SETTINGS.fixed_timeout_seconds
    return timeout


def _export_character_stats_at_exit() -> None:
    export_path = CHARACTER_STATS.export()
    if export_path is not None:
        print(f"Character statistics exported to: {export_path}")


def _configure_runtime_logging() -> None:
    if SETTINGS.debug_mode_enabled:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        debug_log_file = LOG_DIR / "debug.log"
        basicConfig(
            level=DEBUG,
            format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
            filename=debug_log_file,
            filemode="a",
            force=True,
        )
        stream_handler = StreamHandler()
        stream_handler.setFormatter(Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
        getLogger().addHandler(stream_handler)
        getLogger("zendriver").setLevel(DEBUG)
        getLogger("uvicorn").setLevel(DEBUG)
        getLogger("uvicorn.access").setLevel(DEBUG)
        getLogger("uvicorn.error").setLevel(DEBUG)
    else:
        basicConfig(level=INFO, format="[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S", force=True)


_configure_runtime_logging()
atexit.register(_export_character_stats_at_exit)


async def _acquire_client() -> DeepSeekProxyClient:
    async with app.state.pool_lock:
        for client in app.state.client_pool:
            if not client._busy:
                client._busy = True
                return client

        if not TOKEN_POOL:
            raise HTTPException(status_code=500, detail="Missing DEEPSEEK_USER_TOKEN or DEEPSEEK_USER_TOKENS in .env")

        token = TOKEN_POOL[app.state.token_index % len(TOKEN_POOL)]
        app.state.token_index += 1

        client = DeepSeekProxyClient(
            token=token,
            headless=SETTINGS.effective_headless,
            verbose=SETTINGS.effective_verbose,
        )
        client._busy = True
        await client.initialize()
        app.state.client_pool.append(client)
        return client


async def _release_client(client: DeepSeekProxyClient) -> None:
    async with app.state.pool_lock:
        client._busy = False


async def _handle_chat(request: Request) -> dict[str, Any]:
    if not TOKEN_POOL:
        raise HTTPException(status_code=500, detail="Missing DEEPSEEK_USER_TOKEN or DEEPSEEK_USER_TOKENS in .env")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    request_text = _extract_request_text(payload)
    timeout = _resolve_timeout(payload)
    deepthink = bool(payload.get("deepthink", False))
    search = bool(payload.get("search", False))
    expert_mode = bool(payload.get("expert_mode", False))
    multi_turn = bool(payload.get("multi_turn", False))
    session_id = payload.get("session_id")

    if session_id is not None and not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="`session_id` must be a string or null.")

    target_session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None

    client = await _acquire_client()
    try:
        await client.prepare_request(
            session_id=target_session_id if multi_turn else None,
            deepthink=deepthink,
            search=search,
            expert_mode=expert_mode,
        )
        result = await client.ask(request_text, timeout=timeout)
        usage = _build_token_usage(
            request_text=request_text,
            thinking_text=result.get("thinking"),
            response_text=result.get("text"),
        )
        CHARACTER_STATS.record(
            request_text=request_text,
            response_text=result.get("text"),
            thinking_text=result.get("thinking"),
        )
        return {
            "mode": "multi_turn" if multi_turn else "single_turn",
            "session_id": result.get("session_id") if multi_turn else None,
            "text": result.get("text"),
            "thinking": result.get("thinking"),
            "usage": {"tokens": usage} if usage is not None else None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await _release_client(client)


async def _execute_request(
    request_text: str,
    deepthink: bool = False,
    search: bool = False,
    expert_mode: bool = False,
    multi_turn: bool = False,
    session_id: Optional[str] = None,
    timeout: int = 60,
) -> dict[str, Any]:
    timeout = _normalize_timeout(timeout)
    client = await _acquire_client()
    try:
        await client.prepare_request(
            session_id=session_id if multi_turn else None,
            deepthink=deepthink,
            search=search,
            expert_mode=expert_mode,
        )
        result = await client.ask(request_text, timeout=timeout)
        usage = _build_token_usage(
            request_text=request_text,
            thinking_text=result.get("thinking"),
            response_text=result.get("text"),
        )
        CHARACTER_STATS.record(
            request_text=request_text,
            response_text=result.get("text"),
            thinking_text=result.get("thinking"),
        )
        return {
            "mode": "multi_turn" if multi_turn else "single_turn",
            "session_id": result.get("session_id") if multi_turn else None,
            "text": result.get("text"),
            "thinking": result.get("thinking"),
            "usage": {"tokens": usage} if usage is not None else None,
        }
    finally:
        await _release_client(client)


@app.post("/chat")
async def chat(request: Request) -> dict[str, Any]:
    return await _handle_chat(request)


@app.get("/", response_class=HTMLResponse)
async def root_ui() -> str:
    if TEST_UI_FILE.exists():
        return TEST_UI_FILE.read_text(encoding="utf-8")
    return "<h1>test.html not found</h1>"


@app.get("/test", response_class=HTMLResponse)
async def test_ui() -> str:
    return await root_ui()


@app.get("/health")
async def health_get() -> dict[str, Any]:
    result = await _execute_request(
        request_text=CONNECTIVITY_PROMPT,
        deepthink=False,
        search=False,
        expert_mode=False,
        multi_turn=False,
        session_id=None,
        timeout=45,
    )
    text = (result.get("text") or "").strip()
    return {
        "ok": text == "测试通畅✅",
        "expected": "测试通畅✅",
        "mode": result.get("mode"),
        "session_id": result.get("session_id"),
        "text": text,
        "thinking": result.get("thinking"),
        "usage": result.get("usage"),
    }


@app.post("/health")
async def health_post() -> dict[str, Any]:
    return await health_get()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    for client in app.state.client_pool:
        await client.close()
    app.state.client_pool = []
    export_path = CHARACTER_STATS.export()
    if export_path is not None:
        getLogger("DeepSeekProxy").info("Character statistics exported to %s", export_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT, reload=False)
