from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import warnings
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import kaos
from kaos.path import KaosPath
from pydantic import SecretStr

from kimi_cli.agentspec import DEFAULT_AGENT_FILE
from kimi_cli.auth.oauth import OAuthManager
from kimi_cli.cli import InputFormat, OutputFormat
from kimi_cli.config import Config, LLMModel, LLMProvider, load_config
from kimi_cli.llm import augment_provider_with_env_vars, create_llm, model_display_name
from kimi_cli.session import Session
from kimi_cli.share import get_share_dir
from kimi_cli.soul import run_soul
from kimi_cli.soul.agent import Runtime, load_agent
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.utils.aioqueue import QueueShutDown
from kimi_cli.utils.logging import logger, redirect_stderr_to_logger
from kimi_cli.utils.path import shorten_home
from kimi_cli.wire import Wire, WireUISide
from kimi_cli.wire.types import ContentPart, WireMessage

if TYPE_CHECKING:
    from fastmcp.mcp_config import MCPConfig


class ConcurrentLogHandlerSink:
    """
    将 concurrent_log_handler 包装为 loguru 的 sink，解决 Windows 多进程文件锁定问题。
    """

    def __init__(self, filepath: Path, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 10):
        try:
            from concurrent_log_handler import ConcurrentRotatingFileHandler
        except ImportError:
            # 如果未安装 concurrent_log_handler，回退到标准 FileHandler
            # 但会失去多进程安全保护
            self._handler = logging.FileHandler(filepath, encoding="utf-8")
        else:
            self._handler = ConcurrentRotatingFileHandler(
                filename=str(filepath),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        # 使用与 loguru 默认格式兼容的格式
        formatter = logging.Formatter(
            "{levelname} | {asctime} | {name}:{lineno} - {message}",
            style="{",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self._handler.setFormatter(formatter)

    def write(self, message: str) -> None:
        # loguru 的消息包含换行符，需要去除
        message = message.rstrip("\n\r")
        if message:
            # 从消息中提取级别 - loguru 格式: "LEVEL | timestamp | ..."
            level = "INFO"
            if message.startswith("TRACE"):
                level = "TRACE"
            elif message.startswith("DEBUG"):
                level = "DEBUG"
            elif message.startswith("INFO"):
                level = "INFO"
            elif message.startswith("SUCCESS"):
                level = "INFO"
            elif message.startswith("WARNING"):
                level = "WARNING"
            elif message.startswith("ERROR"):
                level = "ERROR"
            elif message.startswith("CRITICAL"):
                level = "CRITICAL"

            record = logging.LogRecord(
                name="kimi_cli",
                level=getattr(logging, level, logging.INFO),
                pathname="",
                lineno=0,
                msg=message,
                args=(),
                exc_info=None,
            )
            self._handler.emit(record)

    def flush(self) -> None:
        self._handler.flush()

    def close(self) -> None:
        self._handler.close()


def enable_logging(debug: bool = False, *, redirect_stderr: bool = True) -> None:
    # NOTE: stderr redirection is implemented by swapping the process-level fd=2 (dup2).
    # That can hide Click/Typer error output during CLI startup, so some entrypoints delay
    # installing it until after critical initialization succeeds.
    logger.remove()  # Remove default stderr handler
    logger.enable("kimi_cli")
    if debug:
        logger.enable("kosong")

    # 使用 concurrent_log_handler 替代 loguru 的内置文件处理器
    # 解决 Windows 上多进程环境下的文件锁定问题
    log_file = get_share_dir() / "logs" / "kimi.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # 创建自定义 sink
    concurrent_sink = ConcurrentLogHandlerSink(
        filepath=log_file,
        max_bytes=10 * 1024 * 1024,  # 10MB
        backup_count=10,
    )

    logger.add(
        concurrent_sink,
        level="TRACE" if debug else "INFO",
        format="{level.name} | {time:YYYY-MM-DD HH:mm:ss} | {name}:{line} - {message}",
        colorize=False,
        enqueue=False,  # 不使用 loguru 的内部队列，因为 ConcurrentRotatingFileHandler 已经是线程安全的
    )

    if redirect_stderr:
        redirect_stderr_to_logger()


class KimiCLI:
    @staticmethod
    async def create(
        session: Session,
        *,
        # Basic configuration
        config: Config | Path | None = None,
        model_name: str | None = None,
        thinking: bool | None = None,
        # Run mode
        yolo: bool = False,
        # Extensions
        agent_file: Path | None = None,
        mcp_configs: list[MCPConfig] | list[dict[str, Any]] | None = None,
        skills_dir: KaosPath | None = None,
        # Loop control
        max_steps_per_turn: int | None = None,
        max_retries_per_step: int | None = None,
        max_ralph_iterations: int | None = None,
        startup_progress: Callable[[str], None] | None = None,
        defer_mcp_loading: bool = False,
    ) -> KimiCLI:
        """
        Create a KimiCLI instance.

        Args:
            session (Session): A session created by `Session.create` or `Session.continue_`.
            config (Config | Path | None, optional): Configuration to use, or path to config file.
                Defaults to None.
            model_name (str | None, optional): Name of the model to use. Defaults to None.
            thinking (bool | None, optional): Whether to enable thinking mode. Defaults to None.
            yolo (bool, optional): Approve all actions without confirmation. Defaults to False.
            agent_file (Path | None, optional): Path to the agent file. Defaults to None.
            mcp_configs (list[MCPConfig | dict[str, Any]] | None, optional): MCP configs to load
                MCP tools from. Defaults to None.
            skills_dir (KaosPath | None, optional): Override skills directory discovery. Defaults
                to None.
            max_steps_per_turn (int | None, optional): Maximum number of steps in one turn.
                Defaults to None.
            max_retries_per_step (int | None, optional): Maximum number of retries in one step.
                Defaults to None.
            max_ralph_iterations (int | None, optional): Extra iterations after the first turn in
                Ralph mode. Defaults to None.
            startup_progress (Callable[[str], None] | None, optional): Progress callback used by
                interactive startup UI. Defaults to None.
            defer_mcp_loading (bool, optional): Defer MCP startup until the interactive shell is
                ready. Defaults to False.

        Raises:
            FileNotFoundError: When the agent file is not found.
            ConfigError(KimiCLIException, ValueError): When the configuration is invalid.
            AgentSpecError(KimiCLIException, ValueError): When the agent specification is invalid.
            SystemPromptTemplateError(KimiCLIException, ValueError): When the system prompt
                template is invalid.
            InvalidToolError(KimiCLIException, ValueError): When any tool cannot be loaded.
            MCPConfigError(KimiCLIException, ValueError): When any MCP configuration is invalid.
            MCPRuntimeError(KimiCLIException, RuntimeError): When any MCP server cannot be
                connected.
        """
        if startup_progress is not None:
            startup_progress("Loading configuration...")

        config = config if isinstance(config, Config) else load_config(config)
        if max_steps_per_turn is not None:
            config.loop_control.max_steps_per_turn = max_steps_per_turn
        if max_retries_per_step is not None:
            config.loop_control.max_retries_per_step = max_retries_per_step
        if max_ralph_iterations is not None:
            config.loop_control.max_ralph_iterations = max_ralph_iterations
        logger.info("Loaded config: {config}", config=config)

        oauth = OAuthManager(config)

        model: LLMModel | None = None
        provider: LLMProvider | None = None

        # try to use config file
        if not model_name and config.default_model:
            # no --model specified && default model is set in config
            model = config.models[config.default_model]
            provider = config.providers[model.provider]
        if model_name and model_name in config.models:
            # --model specified && model is set in config
            model = config.models[model_name]
            provider = config.providers[model.provider]

        if not model:
            model = LLMModel(provider="", model="", max_context_size=100_000)
            provider = LLMProvider(type="kimi", base_url="", api_key=SecretStr(""))

        # try overwrite with environment variables
        assert provider is not None
        assert model is not None
        env_overrides = augment_provider_with_env_vars(provider, model)

        # determine thinking mode
        thinking = config.default_thinking if thinking is None else thinking

        # determine yolo mode
        yolo = yolo if yolo else config.default_yolo

        llm = create_llm(
            provider,
            model,
            thinking=thinking,
            session_id=session.id,
            oauth=oauth,
        )
        if llm is not None:
            logger.info("Using LLM provider: {provider}", provider=provider)
            logger.info("Using LLM model: {model}", model=model)
            logger.info("Thinking mode: {thinking}", thinking=thinking)

        if startup_progress is not None:
            startup_progress("Scanning workspace...")

        runtime = await Runtime.create(config, oauth, llm, session, yolo, skills_dir)
        runtime.notifications.recover()
        runtime.background_tasks.reconcile()

        # Refresh plugin configs with fresh credentials (e.g. OAuth tokens)
        try:
            from kimi_cli.plugin.manager import (
                collect_host_values,
                get_plugins_dir,
                refresh_plugin_configs,
            )

            host_values = collect_host_values(config, oauth)
            if host_values.get("api_key"):
                refresh_plugin_configs(get_plugins_dir(), host_values)
        except Exception:
            logger.debug("Failed to refresh plugin configs, skipping")

        if agent_file is None:
            agent_file = DEFAULT_AGENT_FILE
        if startup_progress is not None:
            startup_progress("Loading agent...")

        agent = await load_agent(
            agent_file,
            runtime,
            mcp_configs=mcp_configs or [],
            start_mcp_loading=not defer_mcp_loading,
        )

        if startup_progress is not None:
            startup_progress("Restoring conversation...")
        context = Context(session.context_file)
        await context.restore()

        if context.system_prompt is not None:
            agent = dataclasses.replace(agent, system_prompt=context.system_prompt)
        else:
            await context.write_system_prompt(agent.system_prompt)

        soul = KimiSoul(agent, context=context)
        return KimiCLI(soul, runtime, env_overrides)

    def __init__(
        self,
        _soul: KimiSoul,
        _runtime: Runtime,
        _env_overrides: dict[str, str],
    ) -> None:
        self._soul = _soul
        self._runtime = _runtime
        self._env_overrides = _env_overrides

    @property
    def soul(self) -> KimiSoul:
        """Get the KimiSoul instance."""
        return self._soul

    @property
    def session(self) -> Session:
        """Get the Session instance."""
        return self._runtime.session

    def shutdown_background_tasks(self) -> None:
        """Kill active background tasks on exit, unless keep_alive_on_exit is configured."""
        if self._runtime.config.background.keep_alive_on_exit:
            return
        killed = self._runtime.background_tasks.kill_all_active(reason="CLI session ended")
        if killed:
            logger.info("Stopped {n} background task(s) on exit: {ids}", n=len(killed), ids=killed)

    @contextlib.asynccontextmanager
    async def _env(self) -> AsyncGenerator[None]:
        original_cwd = KaosPath.cwd()
        await kaos.chdir(self._runtime.session.work_dir)
        try:
            # to ignore possible warnings from dateparser
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            async with self._runtime.oauth.refreshing(self._runtime):
                yield
        finally:
            await kaos.chdir(original_cwd)

    async def run(
        self,
        user_input: str | list[ContentPart],
        cancel_event: asyncio.Event,
        merge_wire_messages: bool = False,
    ) -> AsyncGenerator[WireMessage]:
        """
        Run the Kimi Code CLI instance without any UI and yield Wire messages directly.

        Args:
            user_input (str | list[ContentPart]): The user input to the agent.
            cancel_event (asyncio.Event): An event to cancel the run.
            merge_wire_messages (bool): Whether to merge Wire messages as much as possible.

        Yields:
            WireMessage: The Wire messages from the `KimiSoul`.

        Raises:
            LLMNotSet: When the LLM is not set.
            LLMNotSupported: When the LLM does not have required capabilities.
            ChatProviderError: When the LLM provider returns an error.
            MaxStepsReached: When the maximum number of steps is reached.
            RunCancelled: When the run is cancelled by the cancel event.
        """
        async with self._env():
            wire_future = asyncio.Future[WireUISide]()
            stop_ui_loop = asyncio.Event()

            async def _ui_loop_fn(wire: Wire) -> None:
                wire_future.set_result(wire.ui_side(merge=merge_wire_messages))
                await stop_ui_loop.wait()

            soul_task = asyncio.create_task(
                run_soul(
                    self.soul,
                    user_input,
                    _ui_loop_fn,
                    cancel_event,
                    runtime=self._runtime,
                )
            )

            try:
                wire_ui = await wire_future
                while True:
                    msg = await wire_ui.receive()
                    yield msg
            except QueueShutDown:
                pass
            finally:
                # stop consuming Wire messages
                stop_ui_loop.set()
                # wait for the soul task to finish, or raise
                await soul_task

    async def run_shell(self, command: str | None = None) -> bool:
        """Run the Kimi Code CLI instance with shell UI."""
        from kimi_cli.ui.shell import Shell, WelcomeInfoItem

        welcome_info = [
            WelcomeInfoItem(
                name="Directory", value=str(shorten_home(self._runtime.session.work_dir))
            ),
            WelcomeInfoItem(name="Session", value=self._runtime.session.id),
        ]
        if base_url := self._env_overrides.get("KIMI_BASE_URL"):
            welcome_info.append(
                WelcomeInfoItem(
                    name="API URL",
                    value=f"{base_url} (from KIMI_BASE_URL)",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        if self._env_overrides.get("KIMI_API_KEY"):
            welcome_info.append(
                WelcomeInfoItem(
                    name="API Key",
                    value="****** (from KIMI_API_KEY)",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        if not self._runtime.llm:
            welcome_info.append(
                WelcomeInfoItem(
                    name="Model",
                    value="not set, send /login to login",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        elif "KIMI_MODEL_NAME" in self._env_overrides:
            welcome_info.append(
                WelcomeInfoItem(
                    name="Model",
                    value=f"{self._soul.model_name} (from KIMI_MODEL_NAME)",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        else:
            welcome_info.append(
                WelcomeInfoItem(
                    name="Model",
                    value=model_display_name(self._soul.model_name),
                    level=WelcomeInfoItem.Level.INFO,
                )
            )
            if self._soul.model_name not in (
                "kimi-for-coding",
                "kimi-code",
                "kimi-k2.5",
                "kimi-k2-5",
            ):
                welcome_info.append(
                    WelcomeInfoItem(
                        name="Tip",
                        value="send /login to use our latest kimi-k2.5 model",
                        level=WelcomeInfoItem.Level.WARN,
                    )
                )
        welcome_info.append(
            WelcomeInfoItem(
                name="\nTip",
                value=(
                    "Kimi Code Web UI, a GUI version of Kimi Code, is now in technical preview."
                    "\n"
                    "     Type /web to switch, or next time run `kimi web` directly."
                ),
                level=WelcomeInfoItem.Level.INFO,
            )
        )
        async with self._env():
            shell = Shell(self._soul, welcome_info=welcome_info)
            return await shell.run(command)

    async def run_print(
        self,
        input_format: InputFormat,
        output_format: OutputFormat,
        command: str | None = None,
        *,
        final_only: bool = False,
    ) -> bool:
        """Run the Kimi Code CLI instance with print UI."""
        from kimi_cli.ui.print import Print

        async with self._env():
            print_ = Print(
                self._soul,
                input_format,
                output_format,
                self._runtime.session.context_file,
                final_only=final_only,
            )
            return await print_.run(command)

    async def run_acp(self) -> None:
        """Run the Kimi Code CLI instance as ACP server."""
        from kimi_cli.ui.acp import ACP

        async with self._env():
            acp = ACP(self._soul)
            await acp.run()

    async def run_wire_stdio(self) -> None:
        """Run the Kimi Code CLI instance as Wire server over stdio."""
        from kimi_cli.wire.server import WireServer

        async with self._env():
            server = WireServer(self._soul)
            await server.serve()
