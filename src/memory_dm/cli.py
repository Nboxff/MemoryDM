"""Conversation CLI with shared terminal and slash-command definitions."""

import argparse
import json
import os
import shlex
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from difflib import get_close_matches
from pathlib import Path
from threading import Event, Thread

from memory_dm.config import Settings
from memory_dm.experience.service import ExperienceService
from memory_dm.forgetting import ForgetReport, ForgettingError, ForgettingService
from memory_dm.llm.clients import ClientError, EmbeddingClient, LLMClient
from memory_dm.memory.agent import AgentReply, MemoryAgent
from memory_dm.memory.reconciliation import ReconciliationError
from memory_dm.models import AtomicMemory, Session, Turn
from memory_dm.profile.service import ProfileService
from memory_dm.retrieval import HybridRetriever
from memory_dm.retrieval.advanced import AdvancedRetriever
from memory_dm.retrieval.dense import RetrievalError
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.retrieval.models import GrepQuery, RetrievalLimits
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.experience import ExperienceStore
from memory_dm.storage.memory import MemoryStore
from memory_dm.storage.profile import ProfileStore
from memory_dm.storage.provenance import ProvenanceGraph
from memory_dm.storage.raw_fts import RawFTSStore
from memory_dm.storage.sqlite import SQLiteStore

CLI_ERRORS = (
    ClientError,
    ValueError,
    OSError,
    sqlite3.Error,
    RetrievalError,
    ForgettingError,
    ReconciliationError,
)
SEARCH_DEFAULTS = {
    "search_mode": "hybrid",
    "top_k": 10,
    "dense_top_k": 20,
    "sparse_top_k": 20,
    "time_start": None,
    "time_end": None,
    "time_field": None,
    "force_reembed": False,
    "advanced": False,
}


class Console:
    """Keep terminal effects out of redirected output and business logic."""

    def __init__(self, *, plain: bool = False):
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self.effects = (
            self.interactive
            and not plain
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )
        self._activity: list[str] | None = None
        self._stop_busy: Callable[[], None] | None = None

    def accent(self, text: str) -> str:
        return f"\033[1;36m{text}\033[0m" if self.effects else text

    def session(self, context: "CLIContext") -> None:
        session = context.require_session()
        mode = "Chat" if context.client else "Record only"
        print(self.accent("Memory DM") + " · Your personal assistant")
        print(f"Session: {session.session_id}")
        if context.admin:
            print(f"{mode} · User: {context.user_id} · Model: {context.settings.llm.model}")
        print("/help commands · /paste multi-line · // literal slash · /exit quit")

    def activity(self, label: str) -> None:
        """Update the active spinner; progress callbacks stay silent outside a busy scope."""
        if self._activity is not None:
            self._activity[0] = label

    def stop_busy(self) -> None:
        if self._stop_busy is not None:
            self._stop_busy()

    @contextmanager
    def busy(self, label: str) -> Iterator[None]:
        if not self.effects or not sys.stderr.isatty():
            yield
            return
        stopped = Event()
        activity = [label]
        previous, self._activity = self._activity, activity
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        try:
            frames.encode(sys.stderr.encoding or "utf-8")
        except (UnicodeEncodeError, LookupError):
            frames = "|/-\\"

        def animate() -> None:
            index = 0
            while not stopped.is_set():
                print(
                    f"\r\033[2K{frames[index % len(frames)]} {activity[0]}",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
                index += 1
                stopped.wait(0.1)

        worker = Thread(target=animate, daemon=True)
        worker.start()
        previous_stop = self._stop_busy

        def finish() -> None:
            if stopped.is_set():
                return
            stopped.set()
            worker.join()
            self._activity = previous
            self._stop_busy = previous_stop
            print("\r\033[2K", end="", file=sys.stderr, flush=True)

        self._stop_busy = finish
        try:
            yield
        finally:
            finish()


class ReplyOutput:
    """Render deltas once and always end a partial reply on its own line."""

    def __init__(self, console: Console):
        self.console = console
        self.started = False
        self.finished = False

    def write(self, text: str) -> None:
        if not text:
            return
        if not self.started:
            self.console.stop_busy()
            self.started = True
            print(self.console.accent("[assistant]") + " ", end="", flush=True)
        print(text, end="", flush=True)

    def __enter__(self):
        return self

    def finish(self) -> None:
        if self.started and not self.finished:
            print(flush=True)
            self.finished = True

    def __exit__(self, *args):
        self.finish()


@dataclass
class CLIContext:
    store: SQLiteStore
    settings: Settings
    user_id: str
    console: Console
    session: Session | None = None
    client: LLMClient | None = None
    agent: MemoryAgent | None = None
    running: bool = True
    admin: bool = False

    def require_session(self) -> Session:
        if self.session is None:
            raise ValueError("This command requires --session")
        return self.session


@dataclass(frozen=True)
class Command:
    """Add a handler and one entry to extend both CLI and interactive commands."""

    name: str
    description: str
    handler: Callable[[CLIContext, argparse.Namespace], None]
    configure: Callable[[argparse.ArgumentParser], None] | None = None
    aliases: tuple[str, ...] = ()
    interactive_only: bool = False
    requires_session: bool = False
    raw_text: bool = False


class CommandParseError(ValueError):
    pass


class CommandHelp(Exception):
    """Stop parsing after help without exiting the interactive shell."""


class InteractiveParser(argparse.ArgumentParser):
    """A typo in a slash command must never terminate the conversation."""

    def error(self, message: str) -> None:
        raise CommandParseError(f"{message}\n{self.format_usage().strip()}")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise CommandParseError(message or "Invalid command")
        raise CommandHelp


def _show_history(store: SQLiteStore, session: Session) -> None:
    turns = store.get_turns(session.session_id, session.user_id)
    if not turns:
        print("No conversation history.")
    for turn in turns:
        print(f"[{turn.role}] {turn.content}")


def _show_memories(memories: list[AtomicMemory]) -> None:
    if not memories:
        print("No atomic memories.")
    for memory in memories:
        print(f"{memory.id} [{memory.type}] {memory.content}")
        if memory.valid_time:
            print(f"  Valid time: {memory.valid_time.model_dump_json()}")
        for evidence in memory.evidence:
            span = (
                f"[{evidence.span_start}, {evidence.span_end})"
                if evidence.span_start is not None
                else "whole turn"
            )
            print(f"  Source: {evidence.session_id}/{evidence.turn_id} ({span})")


def _extract_session(store: SQLiteStore, session: Session, settings: Settings) -> None:
    turns = store.get_turns(session.session_id, session.user_id)
    if not any(turn.role == "user" for turn in turns):
        print("No user messages to extract.")
        return
    with ExitStack() as stack:
        client = stack.enter_context(LLMClient(settings.llm))
        embedding = None
        if settings.embedding.api_key.strip() and settings.embedding.model.strip():
            embedding = stack.enter_context(EmbeddingClient(settings.embedding))
        report = MemoryAgent(store, settings, client, embedding).maintain(session)
    saved = [AtomicMemoryStore(store).get(key, session.user_id) for key in report.memory_ids]
    print(f"Memory update: {report.counts}; dense index: {report.index_state}.")
    if report.experience is not None:
        print("Experience update: " + json.dumps(asdict(report.experience)))
    if report.profile is not None:
        print("Profile update: " + json.dumps(report.profile))
    _show_memories(saved)


def _search_memories(
    store: SQLiteStore,
    settings: Settings,
    user_id: str,
    query: str,
    *,
    mode: str = "hybrid",
    top_k: int = 10,
    dense_top_k: int = 20,
    sparse_top_k: int = 20,
    session_id: str | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    time_field: str = "EVENT_TIME",
    advanced: bool = False,
    history: list[Turn] | None = None,
) -> None:
    with ExitStack() as stack:
        if advanced:
            llm = stack.enter_context(LLMClient(settings.llm))
            embedding = None
            if (
                mode != "sparse"
                and settings.embedding.api_key.strip()
                and settings.embedding.model.strip()
            ):
                embedding = stack.enter_context(EmbeddingClient(settings.embedding))
            if history is None and session_id is not None:
                history = store.get_turns(session_id, user_id)
            result = AdvancedRetriever(
                store,
                replace(settings, memory_top_k=top_k),
                llm,
                embedding,
                mode=mode if embedding or mode == "dense" else "sparse",
            ).retrieve(
                query,
                user_id,
                scope=SearchFilters(user_id, session_id, time_start, time_end, time_field),
                history=history,
                limits=RetrievalLimits(
                    top_k=top_k, dense_top_k=dense_top_k, sparse_top_k=sparse_top_k
                ),
            )
            print(
                json.dumps(
                    {"results": result.context.records, "metrics": result.metrics()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        embedding = (
            stack.enter_context(EmbeddingClient(settings.embedding)) if mode != "sparse" else None
        )
        results = HybridRetriever(store, embedding).search(
            query,
            user_id,
            mode=mode,
            top_k=top_k,
            dense_top_k=dense_top_k,
            sparse_top_k=sparse_top_k,
            session_id=session_id,
            time_start=time_start,
            time_end=time_end,
            time_field=time_field,
        )
    if not results:
        print("No matching atomic memories.")
    for result in results:
        print(
            f"Score: {result.score:.6f} (dense rank: {result.dense_rank}, "
            f"sparse rank: {result.sparse_rank})"
        )
        _show_memories([result.memory])


def _rebuild_index(
    store: SQLiteStore,
    settings: Settings,
    user_id: str,
    *,
    mode: str = "hybrid",
    force_reembed: bool = False,
) -> None:
    with ExitStack() as stack:
        embedding = (
            stack.enter_context(EmbeddingClient(settings.embedding)) if mode != "sparse" else None
        )
        counts = HybridRetriever(store, embedding).rebuild(user_id, force_reembed=force_reembed)
        raw_count = RawFTSStore(store).rebuild(user_id)
    print(
        f"Index rebuilt: {counts['dense']} dense memories, {counts['sparse']} sparse memories, "
        f"{raw_count} raw turns."
    )


def _show_forget_report(report: ForgetReport) -> None:
    label = (
        "Forgetting completed"
        if report.verified
        else ("Forgetting incomplete; available changes saved")
    )
    print(f"{label}: {report.model_dump_json()}")


def _forget_memories(
    store: SQLiteStore,
    settings: Settings,
    user_id: str,
    request: str,
    *,
    session_id: str | None = None,
) -> None:
    with ExitStack() as stack:
        llm = stack.enter_context(LLMClient(settings.llm))
        embedding = None
        if settings.embedding.api_key.strip() and settings.embedding.model.strip():
            embedding = stack.enter_context(EmbeddingClient(settings.embedding))
        report = ForgettingService(
            store,
            llm,
            embedding,
            batch_size=settings.forget_batch_size,
            batch_chars=settings.forget_batch_chars,
            max_passes=settings.forget_max_passes,
            context_turns=settings.memory_context_turns,
            probe_count=settings.forget_probe_count,
            options=settings.forgetting,
            probe_answer=lambda question: (
                MemoryAgent(store, settings, llm, embedding).query(user_id, question).content
            ),
        ).forget(request, user_id, session_id=session_id)
    # Neither the original request nor removed text is printed or persisted in an audit log.
    _show_forget_report(report)


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _search_options(parser: argparse.ArgumentParser) -> None:
    default = argparse.SUPPRESS
    parser.add_argument(
        "--mode",
        "--search-mode",
        dest="search_mode",
        choices=["hybrid", "dense", "sparse"],
        default=default,
    )
    for option in ("top-k", "dense-top-k", "sparse-top-k"):
        parser.add_argument(f"--{option}", type=_positive_int, default=default)
    parser.add_argument("--time-start", default=default)
    parser.add_argument("--time-end", default=default)
    parser.add_argument(
        "--time-field",
        choices=["EVENT_TIME", "RECORDED_TIME"],
        default=default,
    )
    parser.add_argument("--force-reembed", action="store_true", default=default)
    parser.add_argument(
        "--advanced",
        action="store_true",
        default=default,
        help="Generate an LLM retrieval plan and execute bounded searches",
    )


def _send_message(context: CLIContext, content: str) -> bool:
    session = context.require_session()
    output = ReplyOutput(context.console)
    try:
        with output:
            options = {} if context.admin else {"on_text": output.write}
            if context.agent:
                with context.console.busy("Thinking..."):
                    result = context.agent.respond(session, content, **options)
                output.finish()
                # The completed text is already on screen; retain receipts and warnings.
                _show_agent_reply(
                    replace(result, content="") if output.started else result,
                    show_metrics=context.admin and context.settings.show_metrics,
                    diagnostics=context.admin,
                )
                return True
            context.store.add_turn(
                Turn(session_id=session.session_id, role="user", content=content), context.user_id
            )
            if context.client is None:
                print("Saved.")
                return True
            history = context.store.get_turns(session.session_id, context.user_id)
            with context.console.busy("Writing reply..."):
                reply = context.client.chat(history, **options)
            context.store.add_turn(
                Turn(session_id=session.session_id, role="assistant", content=reply),
                context.user_id,
                expected_history=history,
            )
            if not output.started:
                print(context.console.accent("[assistant]") + f" {reply}")
            return True
    except CLI_ERRORS as error:
        if output.started:
            print("Reply interrupted; the partial reply was not saved.", file=sys.stderr)
        print(f"Request failed: {error}; use /history to inspect saved messages.", file=sys.stderr)
        return False
    except KeyboardInterrupt:
        if output.started:
            print("Reply interrupted; the partial reply was not saved.", file=sys.stderr)
        raise


@contextmanager
def _line_editor(commands=None) -> Iterator[None]:
    """Use optional stdlib readline; keep input history in this process only."""
    if not sys.stdin.isatty():
        yield
        return
    try:
        import readline
    except ImportError:
        yield
        return
    previous_completer = readline.get_completer()
    previous_delimiters = readline.get_completer_delims()
    previous_history = [
        readline.get_history_item(index)
        for index in range(1, readline.get_current_history_length() + 1)
    ]
    names = sorted(CHAT_COMMAND_LOOKUP if commands is None else commands)

    def complete(text: str, state: int) -> str | None:
        if not readline.get_line_buffer().startswith("/") or readline.get_begidx() != 0:
            return None
        candidates = [f"/{name} " for name in names if f"/{name}".startswith(text)]
        return candidates[state] if state < len(candidates) else None

    readline.clear_history()
    readline.set_completer_delims(" \t\n")
    readline.set_completer(complete)
    binding = "bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete"
    readline.parse_and_bind(binding)
    try:
        yield
    finally:
        readline.clear_history()
        for item in previous_history:
            readline.add_history(item)
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delimiters)


def _command_parser(command: Command, *, interactive: bool = False) -> argparse.ArgumentParser:
    parser_type = InteractiveParser if interactive else argparse.ArgumentParser
    parser = parser_type(prog=f"/{command.name}", description=command.description)
    parser.set_defaults(**SEARCH_DEFAULTS)
    if command.configure:
        command.configure(parser)
    return parser


def _dispatch(context: CLIContext, line: str) -> None:
    name, *remainder = line[1:].split(maxsplit=1)
    arguments = remainder[0] if remainder else ""
    commands = COMMAND_LOOKUP if context.admin else CHAT_COMMAND_LOOKUP
    command = commands.get(name)
    if command is None:
        matches = get_close_matches(name, commands, n=1)
        hint = f" Did you mean /{matches[0]}?" if matches else " Use /help to list commands."
        raise CommandParseError(f"Unknown command: /{name}.{hint}")
    parser = _command_parser(command, interactive=True)
    tokens = (
        [arguments]
        if command.raw_text and arguments and arguments not in ("-h", "--help")
        else shlex.split(arguments)
    )
    try:
        args = parser.parse_args(tokens)
    except CommandHelp:
        return
    if command.requires_session:
        context.require_session()
    command.handler(context, args)


def _interact(context: CLIContext) -> None:
    if context.console.interactive:
        context.console.session(context)
    with _line_editor(COMMAND_LOOKUP if context.admin else CHAT_COMMAND_LOOKUP):
        while context.running:
            try:
                prompt = "you › " if context.console.interactive else ""
                if context.console.effects:
                    # Readline must not count ANSI sequences as visible prompt columns.
                    prompt = "\001\033[1;36m\002you › \001\033[0m\002"
                content = input(prompt)
            except (EOFError, KeyboardInterrupt):
                if context.console.interactive:
                    print()
                break
            command = content.strip()
            if not command:
                continue
            if command.startswith("/") and not command.startswith("//"):
                try:
                    _dispatch(context, command)
                except CLI_ERRORS as error:
                    print(f"Command failed: {error}", file=sys.stderr)
                except KeyboardInterrupt:
                    print("Command interrupted. Use /exit to leave.", file=sys.stderr)
                continue
            if command.startswith("//"):
                # Escape only the first slash, preserving message whitespace.
                offset = content.index("//")
                content = content[:offset] + content[offset + 1 :]
            try:
                _send_message(context, content)
            except KeyboardInterrupt:
                print(
                    "Request interrupted. Use /history to inspect saved messages.", file=sys.stderr
                )
    _finish_conversation(context)


def _show_agent_reply(
    result: AgentReply, *, show_metrics: bool = False, diagnostics: bool = False
) -> None:
    if not diagnostics:
        if result.forget:
            receipt = (
                "I've forgotten that information."
                if result.forget.verified
                else "I couldn't finish forgetting everything. Please try again."
            )
            print(f"[assistant] {receipt}")
        elif result.content:
            print(f"[assistant] {result.content}")
        write = result.write
        pending = result.maintenance_error or (
            write
            and (
                write.maintenance_failures
                or write.index_state == "pending"
                or write.pending_recovery_sessions
                or (write.experience and write.experience.status in {"failed", "retry_exhausted"})
                or (write.profile and write.profile["status"] in {"failed", "retry_exhausted"})
            )
        )
        if pending:
            print("Your conversation is saved. I'll retry remembering it later.", file=sys.stderr)
        return
    if result.forget:
        _show_forget_report(result.forget)
    elif result.content:
        print(f"[assistant] {result.content}")
    if result.write and result.write.counts:
        print("Memory updated.")
        if result.write.index_state == "pending":
            print("Memory saved; dense index refresh is pending.", file=sys.stderr)
    if result.write and result.write.maintenance_failures:
        print(
            "Maintenance failures: " + json.dumps(result.write.maintenance_failures),
            file=sys.stderr,
        )
    if result.maintenance_error:
        print(f"Memory update pending: {result.maintenance_error}", file=sys.stderr)
    if result.write and result.write.pending_recovery_sessions:
        print("Some earlier messages are still awaiting memory processing.", file=sys.stderr)
    if result.write and result.write.experience is not None:
        experience = result.write.experience
        if experience.status in {"failed", "retry_exhausted"}:
            print(
                "Experience consolidation is pending; atomic memories remain saved."
                f" Status: {experience.error_code or experience.status}",
                file=sys.stderr,
            )
    if (
        result.write
        and result.write.profile
        and result.write.profile["status"] in {"failed", "retry_exhausted"}
    ):
        print(
            "Profile consolidation is pending; atomic memories remain saved. "
            + result.write.profile.get("error_code", result.write.profile["status"]),
            file=sys.stderr,
        )
    if show_metrics and result.metrics is not None:
        print("Metrics: " + json.dumps(result.metrics))


def _flush_memory(agent: MemoryAgent | None, session: Session) -> AgentReply | None:
    if agent is None:
        return
    try:
        report = agent.end_session(session)
        return AgentReply("", write=report)
    except (ClientError, ValueError, sqlite3.Error, ReconciliationError, RetrievalError) as error:
        return AgentReply("", maintenance_error=str(error))


def _finish_conversation(context: CLIContext) -> None:
    with context.console.busy("Remembering..."):
        result = _flush_memory(context.agent, context.require_session())
    if result is not None:
        _show_agent_reply(result, diagnostics=context.admin)


def _configure_search(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", nargs="+", help="Query text (quote text containing options)")
    _search_options(parser)


def _configure_retrieve(parser: argparse.ArgumentParser) -> None:
    _configure_search(parser)
    parser.set_defaults(advanced=True)


def _configure_rebuild(parser: argparse.ArgumentParser) -> None:
    _search_options(parser)


def _configure_forget(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("request", nargs="+", help="Describe the information to forget")


def _configure_grep(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pattern", nargs="?")
    parser.add_argument(
        "--mode",
        dest="grep_mode",
        type=str.upper,
        choices=["ALL", "LITERAL", "REGEX", "FTS"],
        default="LITERAL",
    )
    parser.add_argument(
        "--layer", choices=["RAW", "ATOMIC", "EXPERIENCE", "PROFILE"], default="RAW"
    )
    parser.add_argument("--role", choices=["user", "assistant"])
    for name, default in (("before", 0), ("after", 0), ("limit", 20), ("offset", 0)):
        parser.add_argument(f"--{name}", type=int, default=default)
    parser.add_argument("--time-start", default=argparse.SUPPRESS)
    parser.add_argument("--time-end", default=argparse.SUPPRESS)
    parser.add_argument(
        "--time-field",
        choices=["EVENT_TIME", "RECORDED_TIME"],
        default=argparse.SUPPRESS,
    )


def _search_command(context: CLIContext, args: argparse.Namespace) -> None:
    _search_memories(
        context.store,
        context.settings,
        context.user_id,
        " ".join(args.query),
        mode=args.search_mode,
        top_k=args.top_k,
        dense_top_k=args.dense_top_k,
        sparse_top_k=args.sparse_top_k,
        session_id=getattr(args, "session", None),
        time_start=args.time_start,
        time_end=args.time_end,
        time_field=args.time_field or "EVENT_TIME",
        advanced=args.advanced,
        history=(
            context.store.get_turns(context.session.session_id, context.user_id)
            if context.session and args.advanced
            else None
        ),
    )


def _rebuild_command(context: CLIContext, args: argparse.Namespace) -> None:
    _rebuild_index(
        context.store,
        context.settings,
        context.user_id,
        mode=getattr(args, "search_mode", "hybrid"),
        force_reembed=getattr(args, "force_reembed", False),
    )


def _forget_command(context: CLIContext, args: argparse.Namespace) -> None:
    if getattr(args, "session", None):
        raise ValueError("Forgetting is user-wide; omit --session and describe the target")
    if getattr(args, "time_start", None) or getattr(args, "time_end", None):
        raise ValueError("Forgetting time limits must be stated in the request")
    request = " ".join(args.request)
    if not request.strip():
        raise ValueError("A forgetting request is required")
    if context.agent:
        with context.console.busy("Forgetting…"):
            result = context.agent.forget(context.require_session(), request)
        _show_agent_reply(
            result,
            show_metrics=context.admin and context.settings.show_metrics,
            diagnostics=context.admin,
        )
    else:
        _forget_memories(
            context.store,
            context.settings,
            context.user_id,
            request,
            session_id=context.session.session_id if context.session else None,
        )


def _grep_command(context: CLIContext, args: argparse.Namespace) -> None:
    matches = MemoryStore(context.store).grep(
        context.user_id,
        GrepQuery(
            layers=[args.layer],
            pattern=args.pattern,
            mode=args.grep_mode,
            time_start=getattr(args, "time_start", None),
            time_end=getattr(args, "time_end", None),
            time_field=getattr(args, "time_field", None) or "RECORDED_TIME",
            session_id=getattr(args, "session", None),
            role=args.role,
            before=args.before,
            after=args.after,
            limit=args.limit,
            offset=args.offset,
        ),
    )
    print(
        json.dumps(
            [match.model_dump(mode="json") for match in matches], ensure_ascii=False, indent=2
        )
    )


def _history_command(context: CLIContext, args: argparse.Namespace) -> None:
    _show_history(context.store, context.require_session())


def _memories_command(context: CLIContext, args: argparse.Namespace) -> None:
    session_id = context.session.session_id if context.session else None
    _show_memories(AtomicMemoryStore(context.store).list(context.user_id, session_id))


def _extract_command(context: CLIContext, args: argparse.Namespace) -> None:
    try:
        _extract_session(context.store, context.require_session(), context.settings)
    except CLI_ERRORS as error:
        raise ValueError(f"Extraction failed: {error}; retry to process pending turns.") from error


def _configure_experiences(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--id", help="Inspect a specific experience")
    selection.add_argument("--source", help="Find experiences derived from an atomic memory")
    parser.add_argument("--sources", action="store_true", help="Show full sources with --id")


def _configure_profile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh", action="store_true", help="Refresh the user's complete 1000-token profile"
    )
    parser.add_argument("--json", action="store_true", help="Inspect the current profile document")
    parser.add_argument("--export", type=Path, help="Export current Markdown to a new file")


def _profile_command(context: CLIContext, args: argparse.Namespace) -> None:
    if args.refresh:
        if not context.settings.profile_enabled:
            raise ValueError("Set PROFILE_ENABLED=true before generating a profile")
        with LLMClient(context.settings.llm) as client:
            report = ProfileService(context.store, client).maintain(context.user_id)
        print(json.dumps(report, ensure_ascii=False))
        if report["status"] in {"failed", "retry_exhausted"}:
            raise ValueError("Profile consolidation is pending: " + report["error_code"])
    repository = ProfileStore(context.store)
    if not repository.publish(context.user_id):
        print(
            "Profile Markdown synchronization is pending: PROFILE_FILE_SYNC_ERROR", file=sys.stderr
        )
    if args.export:
        # An explicit export is a user-managed snapshot; never silently overwrite it.
        with args.export.open("x", encoding="utf-8") as output:
            output.write(repository.markdown(context.user_id))
    if args.json:
        print(
            json.dumps(
                [e.model_dump(mode="json") for e in repository.read(context.user_id).values()],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(repository.markdown(context.user_id))


def _configure_provenance(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id")
    parser.add_argument(
        "--layer", choices=["RAW", "ATOMIC", "EXPERIENCE", "PROFILE"], required=True
    )


def _provenance_command(context: CLIContext, args: argparse.Namespace) -> None:
    graph = ProvenanceGraph(context.store, context.user_id)
    key = (args.layer, args.id)
    if key not in graph.nodes:
        raise ValueError("Source not found for this user")
    print(
        json.dumps(
            {
                "node": graph.record(key),
                "ancestors": graph.ancestors(key),
                "descendants": graph.descendants({key}),
                "repair_order": graph.repair_order({key}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _experiences_command(context: CLIContext, args: argparse.Namespace) -> None:
    repository = ExperienceStore(context.store)
    if args.sources:
        if not args.id:
            raise ValueError("--sources requires --id")
        print(
            json.dumps(repository.evidence(args.id, context.user_id), ensure_ascii=False, indent=2)
        )
        return
    if args.id:
        found = repository.get(args.id, context.user_id)
        if found is None:
            raise ValueError("Experience not found for this user")
        values = [found]
    elif args.source:
        values = repository.descendants(args.source, context.user_id)
    else:
        values = repository.list(context.user_id)
    if not values:
        print("No experience memories.")
    for experience in values:
        print(experience.model_dump_json(indent=2))


def _configure_consolidate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rescan", action="store_true", help="Recheck all current atomic facts")


def _consolidate_command(context: CLIContext, args: argparse.Namespace) -> None:
    if args.rescan:
        repository = ExperienceStore(context.store)
        repository.schedule(
            context.user_id,
            [
                m.id
                for m in AtomicMemoryStore(context.store).list(context.user_id)
                if m.status == "ACTIVE"
            ],
        )
    with ExitStack() as stack:
        client = context.client or stack.enter_context(LLMClient(context.settings.llm))
        embedding = context.agent.embedding if context.agent else None
        if (
            embedding is None
            and context.settings.embedding.api_key.strip()
            and context.settings.embedding.model.strip()
        ):
            embedding = stack.enter_context(EmbeddingClient(context.settings.embedding))
        report = ExperienceService(
            context.store,
            client,
            batch_size=context.settings.experience_batch_size,
            embedding=embedding,
        ).maintain(context.user_id, force=True)
    print("Experience update: " + json.dumps(asdict(report)))
    if report.status == "failed":
        raise ValueError(f"Experience consolidation is pending: {report.error_code}")


def _sessions_command(context: CLIContext, args: argparse.Namespace) -> None:
    sessions = context.store.list_sessions(context.user_id)
    if not sessions:
        print("No sessions. Start a conversation to create one.")
    for session in sessions:
        current = " *" if context.session == session else ""
        print(f"{session.session_id}  {session.created_at.isoformat()}{current}")


def _status_command(context: CLIContext, args: argparse.Namespace) -> None:
    session = context.require_session()
    print(f"Session: {session.session_id}\nUser: {context.user_id}")
    if context.admin:
        print(f"Mode: {'Chat' if context.client else 'Record only'}")
        print(f"Model: {context.settings.llm.model if context.client else 'disabled'}")
        print(f"Automatic memory: {'on' if context.agent else 'off'}")
    print(f"Turns: {len(context.store.get_turns(session.session_id, context.user_id))}")


def _stats_command(context: CLIContext, args: argparse.Namespace) -> None:
    metrics = context.agent.last_metrics if context.agent else None
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


def _help_command(context: CLIContext, args: argparse.Namespace) -> None:
    print(context.console.accent("Commands"))
    for command in COMMANDS if context.admin else CHAT_COMMANDS:
        print(f"  /{command.name:<15} {command.description}")
    print("Use /command --help for options. Tab completes commands; ↑/↓ recalls input.")
    print("/paste ends with /end; Ctrl+C cancels the draft. // sends a literal slash.")


def _new_command(context: CLIContext, args: argparse.Namespace) -> None:
    _finish_conversation(context)
    context.session = context.store.create_session(context.user_id)
    if context.agent:
        context.agent.last_metrics = None
    context.console.session(context)


def _configure_switch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_id", help="Full session ID belonging to the current user")


def _switch_command(context: CLIContext, args: argparse.Namespace) -> None:
    target = context.store.get_session(args.session_id, context.user_id)
    if target is None:
        raise ValueError("Session not found for this user; use /sessions")
    if target != context.session:
        _finish_conversation(context)
        context.session = target
        if context.agent:
            context.agent.last_metrics = None
    context.console.session(context)


def _paste_command(context: CLIContext, args: argparse.Namespace) -> None:
    print("Multi-line message: /end sends, Ctrl+C cancels; //end inserts literal /end.")
    lines = []
    try:
        while True:
            line = input("… " if context.console.interactive else "")
            if line == "/end":
                break
            lines.append(line[1:] if line == "//end" else line)
    except (EOFError, KeyboardInterrupt):
        print("Draft cancelled; nothing saved.")
        return
    content = "\n".join(lines)
    if content.strip():
        _send_message(context, content)


def _exit_command(context: CLIContext, args: argparse.Namespace) -> None:
    context.running = False


# This registry drives subcommands, slash dispatch, help, aliases and completion.
# Handlers receive explicit dependencies; adding a command needs no dispatcher edits.
COMMANDS = (
    Command("history", "Show current conversation", _history_command, requires_session=True),
    Command("memories", "List atomic memories", _memories_command),
    Command(
        "profile",
        "Inspect or export the supported user profile",
        _profile_command,
        _configure_profile,
    ),
    Command(
        "provenance",
        "Inspect cross-layer sources and dependent records",
        _provenance_command,
        _configure_provenance,
    ),
    Command(
        "experiences",
        "Inspect habits, preferences and their sources",
        _experiences_command,
        _configure_experiences,
    ),
    Command(
        "consolidate",
        "Consolidate pending facts into experience memories",
        _consolidate_command,
        _configure_consolidate,
    ),
    Command("sessions", "List your sessions (* marks current)", _sessions_command),
    Command(
        "extract",
        "Process pending conversation into memories",
        _extract_command,
        requires_session=True,
    ),
    Command("search", "Search atomic memories", _search_command, _configure_search),
    Command("retrieve", "Inspect advanced retrieval", _search_command, _configure_retrieve),
    Command("grep", "Search raw conversations or atomic facts", _grep_command, _configure_grep),
    Command("rebuild-index", "Rebuild retrieval indexes", _rebuild_command, _configure_rebuild),
    Command(
        "forget",
        "Forget information across this user's sessions",
        _forget_command,
        _configure_forget,
        raw_text=True,
    ),
    Command(
        "help", "Show commands and usage", _help_command, aliases=("?",), interactive_only=True
    ),
    Command("status", "Show conversation details", _status_command, interactive_only=True),
    Command("stats", "Show metrics for the last request", _stats_command, interactive_only=True),
    Command("new", "Start a new session", _new_command, interactive_only=True),
    Command(
        "switch",
        "Resume one of your sessions",
        _switch_command,
        _configure_switch,
        interactive_only=True,
    ),
    Command("paste", "Compose a multi-line message", _paste_command, interactive_only=True),
    Command(
        "exit",
        "Save conversation and quit",
        _exit_command,
        aliases=("quit", "q"),
        interactive_only=True,
    ),
)
COMMAND_LOOKUP = {
    name: command for command in COMMANDS for name in (command.name, *command.aliases)
}
CHAT_COMMANDS = tuple(
    command
    for command in COMMANDS
    if command.name in {"help", "history", "sessions", "status", "new", "switch", "paste", "exit"}
)
CHAT_COMMAND_LOOKUP = {
    name: command for command in CHAT_COMMANDS for name in (command.name, *command.aliases)
}


def _build_parser(*, admin: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-dm-admin" if admin else "memory-dm",
        description="Memory DM · memory tools" if admin else "Memory DM · your personal assistant",
        epilog="Examples:\n"
        "  memory-dm                              Start chatting\n"
        '  memory-dm -m "Hello"                    Send one message and exit\n'
        "\nIn a conversation, type /help to see available commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="Conversation storage path")
    parser.set_defaults(**SEARCH_DEFAULTS)
    parser.add_argument("--user", help="User ID")
    parser.add_argument("--session", help="Resume an existing session")
    parser.add_argument(
        "--plain", action="store_true", help="Disable colors and progress animation"
    )
    chat_mode = parser.add_mutually_exclusive_group()
    chat_mode.add_argument("--llm", action="store_true", help=argparse.SUPPRESS)
    chat_mode.add_argument("--record-only", action="store_true", help=argparse.SUPPRESS)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("-m", "--message", help="Send one message and exit (supports newlines)")
    for name in (
        ("sessions", "history", "extract", "memories", "rebuild-index")
        if admin
        else ("sessions", "history")
    ):
        actions.add_argument(
            f"--{name}",
            dest="action",
            action="store_const",
            const=name,
            help=COMMAND_LOOKUP[name].description,
        )
    parser.set_defaults(search=None, forget=None)
    if admin:
        actions.add_argument("--search", metavar="QUERY", help="Search stored atomic memories")
        actions.add_argument("--forget", metavar="REQUEST", help="Forget target information")
        _search_options(parser)
    commands = parser.add_subparsers(dest="command")
    for command in COMMANDS if admin else CHAT_COMMANDS:
        if command.interactive_only:
            continue
        subparser = commands.add_parser(
            command.name,
            aliases=list(command.aliases),
            help=command.description,
            description=command.description,
        )
        if command.configure:
            command.configure(subparser)
        for name in ("db", "user", "session"):
            subparser.add_argument(f"--{name}", default=argparse.SUPPRESS)
        subparser.add_argument("--plain", action="store_true", default=argparse.SUPPRESS)
        subparser.set_defaults(handler=command)
    return parser


def main(argv: list[str] | None = None) -> int:
    return _main(argv)


def admin_main(argv: list[str] | None = None) -> int:
    return _main(argv, admin=True)


def _main(argv: list[str] | None = None, *, admin: bool = False) -> int:
    parser = _build_parser(admin=admin)
    args = parser.parse_args(argv)
    action = args.action
    if args.search is not None:
        action, args.query = "search", [args.search]
    if args.forget is not None:
        action, args.request = "forget", [args.forget]
    if args.command and (action or args.message is not None):
        parser.error("Commands cannot be combined with another action")
    command = getattr(args, "handler", None) or (COMMAND_LOOKUP[action] if action else None)
    if command and (args.llm or args.record_only):
        parser.error("Commands cannot be combined with --llm or --record-only")
    if command and command.requires_session and not args.session:
        parser.error(f"--{command.name} requires --session")
    if args.message is not None and not args.message.strip():
        parser.error("--message must not be blank")
    try:
        settings = Settings.from_env()
        user_id = args.user if args.user is not None else settings.user_id
        with ExitStack() as stack:
            store = stack.enter_context(SQLiteStore(args.db or settings.db_path))
            context = CLIContext(store, settings, user_id, Console(plain=args.plain), admin=admin)
            if command:
                if args.session:
                    context.session = store.get_session(args.session, user_id)
                    if context.session is None:
                        raise ValueError("Session not found for this user")
                command.handler(context, args)
                return 0
            configured = bool(settings.llm.api_key.strip() and settings.llm.model.strip())
            use_llm = args.llm or (configured and not args.record_only)
            context.client = stack.enter_context(LLMClient(settings.llm)) if use_llm else None
            if args.session:
                context.session = store.get_session(args.session, user_id)
                if context.session is None:
                    raise ValueError("Session not found for this user")
            else:
                context.session = store.create_session(user_id)
            if context.client and settings.auto_memory:
                embedding = None
                if settings.embedding.api_key.strip() and settings.embedding.model.strip():
                    embedding = stack.enter_context(EmbeddingClient(settings.embedding))
                context.agent = MemoryAgent(
                    store, settings, context.client, embedding, progress=context.console.activity
                )
            if args.message is not None:
                print(f"Session: {context.session.session_id}")
                success = _send_message(context, args.message)
                _finish_conversation(context)
                return 0 if success else 1
            _interact(context)
    except KeyboardInterrupt:
        print("\nExited. Saved conversations are preserved.", file=sys.stderr)
        return 130
    except CLI_ERRORS as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0
