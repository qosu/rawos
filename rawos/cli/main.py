"""
rawos CLI — intent-native OS shell interface.

Commands:
  rawos status   — current inferred intent + confidence
  rawos show     — proactive artifacts rawos has created
  rawos goal     — submit an explicit goal, stream response live
  rawos ask      — one-shot question, stream response live
  rawos chat     — interactive multi-turn REPL
  rawos why      — explain why rawos created a file
  rawos watch    — live TUI (rich Live display)
  rawos login    — authenticate and save credentials

Config stored at ~/.rawos/credentials.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import click
import httpx

_CONFIG_DIR  = Path.home() / ".rawos"
_CREDS_FILE  = _CONFIG_DIR / "credentials.json"
_DEFAULT_URL = os.environ.get("RAWOS_URL", "http://127.0.0.1:8002")


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------

def _load_creds() -> dict:
    try:
        return json.loads(_CREDS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_creds(data: dict) -> None:
    _CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _CREDS_FILE.write_text(json.dumps(data, indent=2))
    _CREDS_FILE.chmod(0o600)


def _get_token() -> str:
    creds = _load_creds()
    token = creds.get("access_token", "")
    if not token:
        click.echo("Not authenticated. Run: rawos login", err=True)
        sys.exit(1)
    return token


def _api(method: str, path: str, **kwargs: Any) -> dict:
    token = _get_token()
    url = _DEFAULT_URL.rstrip("/") + path
    with httpx.Client(timeout=15.0) as client:
        resp = getattr(client, method.lower())(
            url,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
    if resp.status_code == 401:
        click.echo("Session expired. Run: rawos login", err=True)
        sys.exit(1)
    if resp.status_code >= 400:
        click.echo(f"API error {resp.status_code}: {resp.text[:200]}", err=True)
        sys.exit(1)
    return resp.json()


_MAX_RECONNECTS = 5


def _api_stream(path: str, payload: dict) -> Iterator[dict]:
    """Stream SSE events from a POST endpoint, resuming across drops.

    Uses unbounded read-timeout (read=None) because agent runs can be long.
    Yields one parsed dict per ``data: {...}`` SSE frame, excluding the
    `run_started` / `run_complete` control events (Stage F framing) — these
    are consumed here to track ``run_id`` and the last seen ``id:`` sequence.
    Blank keepalive lines and non-``data:``/``id:`` lines are skipped.
    Mirrors ``_api``'s 401 / ≥400 error handling: echoes the error and exits.

    On a clean `run_complete`, returns with no reconnect. On a premature
    stream end / transport error before `run_complete`, reconnects via
    ``GET {path}/{run_id}/stream`` with a `Last-Event-ID` header set to the
    last seen sequence number, up to `_MAX_RECONNECTS` attempts.
    """
    token = _get_token()
    url = _DEFAULT_URL.rstrip("/") + path
    run_id: str | None = None
    last_seq = 0
    method = "POST"
    request_url = url
    reconnects = 0

    with httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None)
    ) as client:
        while True:
            headers = {"Authorization": f"Bearer {token}"}
            stream_kwargs: dict[str, Any] = {"headers": headers}
            if method == "POST":
                stream_kwargs["json"] = payload
            else:
                headers["Last-Event-ID"] = str(last_seq)

            completed = False
            try:
                with client.stream(method, request_url, **stream_kwargs) as resp:
                    if resp.status_code == 401:
                        click.echo("Session expired. Run: rawos login", err=True)
                        sys.exit(1)
                    if resp.status_code >= 400:
                        click.echo(
                            f"API error {resp.status_code}",
                            err=True,
                        )
                        sys.exit(1)
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        if line.startswith("id: "):
                            try:
                                last_seq = int(line[len("id: "):].strip())
                            except ValueError:
                                pass
                            continue
                        if not line.startswith("data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        ev_type = event.get("type")
                        if ev_type == "run_started":
                            run_id = event.get("run_id")
                            continue
                        if ev_type == "run_complete":
                            completed = True
                            continue
                        yield event
            except httpx.TransportError:
                pass

            if completed:
                return
            if run_id is None or reconnects >= _MAX_RECONNECTS:
                return

            reconnects += 1
            time.sleep(min(2 ** reconnects, 10))
            method = "GET"
            request_url = f"{url}/{run_id}/stream"


def _resolve_project_id() -> str:
    """Return the current project ID, falling back to the first project.

    Exits with an error message if no project exists at all.
    Mirrors the project-resolution logic previously inlined in ``goal``.
    """
    status_data = _api("get", "/context/status")
    project_id = status_data.get("current_project_id")
    if not project_id:
        items = _api("get", "/projects")
        if not items:
            click.echo("No project found. Create one first.", err=True)
            sys.exit(1)
        project_id = items[0]["id"]
    return project_id


def _render_event(event: dict, console: Any) -> None:
    """Dispatch one SSE event dict to terminal output.

    Unknown event types are silently ignored for forward-compatibility
    with future server-side event types.
    """
    ev_type = event.get("type")
    if ev_type == "orchestrator_plan":
        for i, task in enumerate(event.get("plan") or [], start=1):
            console.print(f"[dim]{i}. {task.get('goal', '')}[/dim]")
    elif ev_type == "agent_spawn":
        console.print(
            f"[dim]↳ spawned {event.get('agent_type', '')}: "
            f"{event.get('goal', '')}[/dim]"
        )
    elif ev_type == "agent_status":
        status = event.get("status", "")
        color = "red" if status == "failed" else "dim"
        console.print(f"[{color}]{status}[/{color}]", end=" ")
    elif ev_type == "agent_output":
        text = event.get("content") or ""
        if text:
            console.print(text, end="")
    elif ev_type == "chunk":
        text = event.get("text") or ""
        if text:
            console.print(text, end="")
    elif ev_type == "tool_call":
        tool = event.get("tool", "")
        console.print(f"[dim]→ {tool}[/dim]")
    elif ev_type == "error":
        console.print(f"[red]{event.get('message', '')}[/red]")
    # unknown type → silently ignored (forward-compatible)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """rawos — intent-native operating system"""


@cli.command()
@click.option("--email", prompt="Email")
@click.option("--password", prompt="Password", hide_input=True)
def login(email: str, password: str) -> None:
    """Authenticate and store credentials."""
    url = _DEFAULT_URL.rstrip("/") + "/auth/login"
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(url, json={"email": email, "password": password})
    if resp.status_code != 200:
        click.echo(f"Login failed: {resp.status_code} {resp.text[:200]}", err=True)
        sys.exit(1)
    data = resp.json()
    _save_creds({
        "email": email,
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
    })
    click.echo("Authenticated.")


@cli.command()
def status() -> None:
    """Show current inferred intent and context."""
    from rich.console import Console
    from rich.table import Table
    from rich import box

    data = _api("get", "/context/status")
    intent = data.get("intent", {})
    console = Console()

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("key",   style="dim", no_wrap=True)
    table.add_column("value", style="white")

    goal = intent.get("goal") or "(no goal inferred yet)"
    conf = intent.get("confidence", 0.0)
    conf_color = "green" if conf >= 0.7 else "yellow" if conf >= 0.4 else "red"

    table.add_row("intent", f"[bold]{goal}[/bold]")
    table.add_row("confidence", f"[{conf_color}]{conf:.0%}[/{conf_color}]  ({intent.get('source','?')})")
    table.add_row("domain", intent.get("domain", "?"))

    stack = data.get("inferred_stack", [])
    if stack:
        table.add_row("stack", " · ".join(stack))

    domains = data.get("active_domains", [])
    if domains:
        table.add_row("activity", " · ".join(domains))

    project = data.get("current_project_id") or "none"
    table.add_row("project", project)

    actions = intent.get("suggested_actions", [])
    if actions:
        table.add_row("suggested", "\n".join(f"  {a}" for a in actions))

    console.print(table)


@cli.command()
@click.option("--limit", "-n", default=10, show_default=True, help="Max results")
def show(limit: int) -> None:
    """List proactive artifacts rawos has created."""
    from rich.console import Console
    from rich.table import Table
    from rich import box

    data = _api("get", f"/push/artifacts?limit={limit}")
    artifacts = data.get("artifacts", [])
    console = Console()

    if not artifacts:
        console.print("[dim]No proactive artifacts yet.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    table.add_column("file",       style="cyan", no_wrap=True, max_width=50)
    table.add_column("goal",       style="white", max_width=45)
    table.add_column("confidence", style="yellow", justify="right")
    table.add_column("age",        style="dim", justify="right")

    now = int(time.time())
    for art in artifacts:
        fname = Path(art.get("file_path", "?")).name
        goal  = (art.get("goal") or "")[:44]
        conf  = f"{art.get('confidence', 0):.0%}"
        age_s = now - (art.get("created_at") or now)
        if age_s < 60:
            age = f"{age_s}s ago"
        elif age_s < 3600:
            age = f"{age_s//60}m ago"
        else:
            age = f"{age_s//3600}h ago"
        table.add_row(fname, goal, conf, age)

    console.print(table)


@cli.command()
@click.argument("goal_text")
def goal(goal_text: str) -> None:
    """Submit an explicit goal to rawos and stream the response live."""
    from rich.console import Console
    console = Console()
    project_id = _resolve_project_id()
    for event in _api_stream("/intent", {"project_id": project_id, "message": goal_text}):
        _render_event(event, console)
    console.print()


@cli.command()
@click.argument("file_path")
def why(file_path: str) -> None:
    """Explain why rawos created a file."""
    from rich.console import Console
    from rich import box
    from rich.table import Table

    abs_path = str(Path(file_path).expanduser().resolve())
    data = _api("get", f"/context/why?path={abs_path}")
    console = Console()

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("key",   style="dim")
    table.add_column("value", style="white")
    table.add_row("file",      abs_path)
    table.add_row("goal",      data.get("goal", "?"))
    table.add_row("domain",    data.get("domain", "?"))
    ts = data.get("generated_at")
    if ts:
        import datetime
        table.add_row("created", datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"))
    console.print(table)


@cli.command()
@click.option("--interval", "-i", default=5.0, show_default=True,
              help="Refresh interval in seconds")
def watch(interval: float) -> None:
    """Live status view (Ctrl-C to exit)."""
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich import box
    import time as _time

    console = Console()

    def _make_table() -> Table:
        try:
            data = _api("get", "/context/status")
        except SystemExit:
            raise
        except Exception as e:
            t = Table(box=box.SIMPLE, show_header=False)
            t.add_column("", style="red")
            t.add_row(f"error: {e}")
            return t

        intent = data.get("intent", {})
        goal = intent.get("goal") or "(observing...)"
        conf = intent.get("confidence", 0.0)
        conf_color = "green" if conf >= 0.7 else "yellow" if conf >= 0.4 else "red"

        t = Table(
            title="[bold cyan]rawos[/bold cyan] — live",
            box=box.SIMPLE, show_header=False, padding=(0, 2),
        )
        t.add_column("key", style="dim", no_wrap=True)
        t.add_column("value", style="white")
        t.add_row("intent",     f"[bold]{goal}[/bold]")
        t.add_row("confidence", f"[{conf_color}]{conf:.0%}[/{conf_color}]")
        t.add_row("domain",     intent.get("domain", "?"))
        stack = data.get("inferred_stack", [])
        if stack:
            t.add_row("stack", " · ".join(stack))
        t.add_row("", "")
        t.add_row("[dim]updated[/dim]", f"[dim]{_time.strftime('%H:%M:%S')}[/dim]")
        return t

    with Live(_make_table(), console=console, refresh_per_second=1) as live:
        while True:
            _time.sleep(interval)
            live.update(_make_table())



@cli.command()
@click.argument('file_path')
@click.argument('rating', type=click.IntRange(1, 5))
@click.option('--comment', '-c', default=None, help='Optional comment')
def rate(file_path: str, rating: int, comment: str | None) -> None:
    """Rate a proactive artifact for research evaluation (1=useless, 5=excellent)."""
    from rich.console import Console
    console = Console()
    result = _api('post', '/evaluation/rate', json={
        'file_path': file_path,
        'rating': rating,
        'comment': comment,
    })
    stars = '★' * rating + '☆' * (5 - rating)
    console.print(f'[green]Rated[/green] {stars}  {file_path}')



@cli.command('eval')
def evaluation() -> None:
    """Show evaluation report: precision, relevance, and research metrics."""
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box

    data = _api('get', '/evaluation/report')
    console = Console()

    precision   = data.get('precision')
    rel_mean    = data.get('relevance_mean')
    total_inf   = data.get('total_inferences', 0)
    total_rated = data.get('total_rated', 0)
    status      = data.get('status', 'insufficient_data')
    threshold   = data.get('threshold_target', 0.65)
    rel_target  = data.get('relevance_target', 3.0)

    status_color = {'on_target': 'green', 'below_target': 'yellow', 'insufficient_data': 'dim'}.get(status, 'dim')
    status_label = status.replace('_', ' ').upper()
    prec_str  = f'{precision:.1%}' if precision is not None else '—'
    rel_str   = f'{rel_mean:.2f}/5' if rel_mean is not None else '—'

    summary_lines = [
        f'[{status_color}]■[/{status_color}] {status_label}',
        '',
        f'precision     {prec_str:>10}   target {threshold:.0%}',
        f'relevance     {rel_str:>10}   target {rel_target}/5',
        f'inferences    {total_inf:>10}',
        f'rated         {total_rated:>10}',
    ]
    console.print(Panel('\n'.join(summary_lines), title='[bold cyan]rawos eval[/bold cyan]', border_style='dim'))

    domain_bd = data.get('domain_breakdown', {})
    if domain_bd:
        t = Table(title='by domain', box=box.SIMPLE, show_header=True, padding=(0, 1))
        t.add_column('domain',     style='cyan')
        t.add_column('inferences', justify='right', style='dim')
        t.add_column('precision',  justify='right')
        t.add_column('relevance',  justify='right')
        for dom, vals in sorted(domain_bd.items()):
            prec = f"{vals['precision']:.1%}" if vals['precision'] is not None else '—'
            rel  = f"{vals['mean_rating']:.2f}" if vals['mean_rating'] is not None else '—'
            t.add_row(dom, str(vals['inferences']), prec, rel)
        console.print(t)

    conf_bins = data.get('confidence_bins', {})
    if conf_bins:
        t = Table(title='precision by confidence bucket', box=box.SIMPLE, show_header=True, padding=(0, 1))
        t.add_column('confidence', style='cyan')
        t.add_column('total',      justify='right', style='dim')
        t.add_column('precision',  justify='right')
        for bucket in ['0-50%', '50-65%', '65-80%', '80-100%']:
            v = conf_bins.get(bucket)
            if v:
                prec = f"{v['precision']:.1%}" if v['precision'] is not None else '—'
                t.add_row(bucket, str(v['total']), prec)
        console.print(t)



@cli.group()
def dataset() -> None:
    """Ground truth dataset for research evaluation (Phase 8)."""


@dataset.command('stats')
def dataset_stats() -> None:
    """Show dataset statistics: total examples, by source and domain."""
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.panel import Panel
    console = Console()
    import rawos.db as db
    from rawos.config import settings
    db.init(settings.db_path)
    from rawos.dataset.manager import stats
    data = stats()

    console.print()
    console.print(Panel(
        f"[bold]Total examples:[/bold] {data['total']}\n"
        f"[bold]Domain coverage:[/bold] {data['domain_coverage']}\n"
        f"[bold]Avg confidence:[/bold] {data['avg_confidence']:.3f}\n"
        f"[bold]Avg quality:[/bold] {data['avg_quality']:.2f}",
        title="[bold cyan]rawos dataset stats[/bold cyan]",
        border_style="dim",
    ))

    # By source
    src_table = Table(title="By Source", box=box.SIMPLE)
    src_table.add_column("Source", style="cyan")
    src_table.add_column("Count", justify="right")
    for src, cnt in sorted(data["by_source"].items()):
        src_table.add_row(src, str(cnt))
    console.print(src_table)

    # By domain
    dom_table = Table(title="By Domain", box=box.SIMPLE)
    dom_table.add_column("Domain", style="yellow")
    dom_table.add_column("Count", justify="right")
    for dom, cnt in sorted(data["by_domain"].items(), key=lambda x: -x[1]):
        dom_table.add_row(dom, str(cnt))
    if data["domains_missing"]:
        dom_table.add_row(
            "[red]missing: " + ", ".join(data["domains_missing"]) + "[/red]",
            "[red]0[/red]",
        )
    console.print(dom_table)


@dataset.command('build')
@click.option('--no-extract', is_flag=True, help='Skip tg-claude extraction')
@click.option('--synthetic', '-s', default=8, show_default=True,
              help='Synthetic examples per domain (0 to skip)')
def dataset_build(no_extract: bool, synthetic: int) -> None:
    """Build ground truth dataset: extract from tg-claude + generate synthetic."""
    from rich.console import Console
    from rich.panel import Panel
    console = Console()
    import asyncio
    import rawos.db as db
    from rawos.config import settings
    db.init(settings.db_path)
    from rawos.dataset.manager import build

    console.print("[dim]Starting dataset build...[/dim]")
    if not no_extract:
        console.print("  [cyan]→[/cyan] Extracting from tg-claude sessions.db")
    if synthetic > 0:
        console.print(f"  [cyan]→[/cyan] Generating {synthetic} synthetic examples × 12 domains")

    result = asyncio.run(build(extract=not no_extract, synthetic_per_domain=synthetic))

    console.print()
    console.print(Panel(
        f"[green]extracted:[/green]  {result['extracted']}\n"
        f"[green]synthetic:[/green]  {result['synthetic']}\n"
        f"[bold green]total:[/bold green]      {result['total']}\n"
        f"[red]errors:[/red]     {len(result['errors'])}",
        title="[bold cyan]Build Complete[/bold cyan]",
        border_style="green" if result['total'] >= 100 else "yellow",
    ))
    if result["errors"]:
        console.print("[red]Errors:[/red]")
        for err in result["errors"][:10]:
            console.print(f"  [red]•[/red] {err}")


@cli.group()
def classifier() -> None:
    """ML classifier for intent inference (Phase 9)."""


@classifier.command('train')
def classifier_train() -> None:
    """Train intent classifier on labeled dataset, save model to disk."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    console = Console()

    import rawos.db as db
    from rawos.config import settings
    db.init(settings.db_path)

    console.print("[dim]Training intent classifier...[/dim]")
    from rawos.inference.classifier import train
    clf = train(save=True)

    console.print()
    console.print(Panel(
        f"[bold]Best model:[/bold]    {clf.model_type.upper()}\n"
        f"[bold]CV macro F1:[/bold]   {clf.cv_f1_mean:.4f} ± {clf.cv_f1_std:.4f}\n"
        f"[bold]Training size:[/bold] {clf.training_size} examples",
        title="[bold cyan]Classifier Trained[/bold cyan]",
        border_style="green",
    ))

    cv_table = Table(title="Cross-Validation Results", box=box.SIMPLE)
    cv_table.add_column("Model", style="cyan")
    cv_table.add_column("Macro F1 mean", justify="right")
    cv_table.add_column("Macro F1 std", justify="right")
    for name, res in clf.cv_results.items():
        if name.startswith("_"):
            continue
        best = " [green]★[/green]" if name == clf.model_type else ""
        cv_table.add_row(name.upper() + best, f"{res['cv_f1_mean']:.4f}", f"{res['cv_f1_std']:.4f}")
    console.print(cv_table)


@classifier.command('benchmark')
@click.option('--llm', 'llm_sample', default=0, show_default=True,
              help='Evaluate LLM on N examples (0=skip, expensive)')
def classifier_benchmark(llm_sample: int) -> None:
    """Benchmark rule vs classifier vs LLM on the labeled dataset."""
    import asyncio
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    console = Console()

    import rawos.db as db
    from rawos.config import settings
    db.init(settings.db_path)

    from rawos.inference.classifier import IntentClassifier
    clf = IntentClassifier.load()
    if clf is None:
        console.print("[red]No trained classifier found. Run `rawos classifier train` first.[/red]")
        return

    # Load classifier into engine for benchmark
    import rawos.inference.intent_engine as engine
    engine._CLASSIFIER = clf

    console.print("[dim]Running benchmark...[/dim]")
    if llm_sample > 0:
        console.print(f"[dim]  (LLM sample: {llm_sample} examples — this will take ~{llm_sample * 3}s)[/dim]")

    from rawos.inference.benchmark import run_full_benchmark
    results = asyncio.run(run_full_benchmark(llm_sample=llm_sample))

    if "error" in results:
        console.print(f"[red]Error: {results['error']}[/red]")
        return

    console.print()
    t = Table(title="Benchmark Results", box=box.SIMPLE_HEAVY)
    t.add_column("Strategy",  style="cyan")
    t.add_column("Macro P",   justify="right")
    t.add_column("Macro R",   justify="right")
    t.add_column("Macro F1",  justify="right", style="bold")
    t.add_column("Accuracy",  justify="right")
    t.add_column("N",         justify="right")

    best_strat = results.get("best_strategy", "")
    for strat_name, s in results["strategies"].items():
        if "error" in s:
            t.add_row(strat_name, "[red]error[/red]", "", "", "", "")
            continue
        marker = " [green]★[/green]" if strat_name == best_strat else ""
        t.add_row(
            strat_name + marker,
            f"{s.get('macro_precision', 0):.4f}",
            f"{s.get('macro_recall', 0):.4f}",
            f"{s.get('macro_f1', 0):.4f}",
            f"{s.get('accuracy', 0):.4f}",
            str(s.get("n_examples", s.get("sample_size", "?"))),
        )
    console.print(t)
    console.print(f"[dim]Results saved to /root/rawos/data/benchmark_results.json[/dim]")


@cli.group()
def timing() -> None:
    """Timing model for proactive scheduling (Phase 10)."""


@timing.command('status')
def timing_status() -> None:
    """Show current timing signals and timeliness score for your account."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    console = Console()

    import rawos.db as db
    from rawos.config import settings
    db.init(settings.db_path)

    creds = _load_creds()
    if not creds:
        console.print("[red]Not logged in. Run `rawos login` first.[/red]")
        return

    resp = _api("GET", "/timing/score")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    score = resp.get("timeliness_score", 0)
    threshold = resp.get("threshold", 0.35)
    would_fire = resp.get("would_fire", False)
    explanation = resp.get("explanation", "")
    fallback = resp.get("fallback_mode", False)
    comps = resp.get("components", {})

    status_color = "green" if would_fire else "yellow"
    status_text = "[green]WOULD FIRE[/green]" if would_fire else "[yellow]BELOW THRESHOLD[/yellow]"

    console.print()
    console.print(Panel(
        f"[bold]Timeliness score:[/bold] {score:.4f} / 1.0\n"
        f"[bold]Threshold:[/bold]        {threshold}\n"
        f"[bold]Status:[/bold]           {status_text}\n"
        f"[bold]Explanation:[/bold]      {explanation}\n"
        f"[bold]Fallback mode:[/bold]    {fallback}",
        title="[bold cyan]rawos timing status[/bold cyan]",
        border_style=status_color,
    ))

    if comps:
        t = Table(title="Score Components", box=box.SIMPLE)
        t.add_column("Signal", style="cyan")
        t.add_column("Score", justify="right")
        for name, val in comps.items():
            bar = "█" * int(val * 20) if val > 0 else "·"
            t.add_row(name, f"{val:.4f}  {bar}")
        console.print(t)


@cli.group()
def study() -> None:
    """30-day research study management (Phase 11)."""


@study.command('setup')
@click.argument('watch_paths', nargs=-1, metavar='PATH...')
@click.option('--label', '-l', multiple=True, help='Labels for each path (optional)')
def study_setup(watch_paths: tuple, label: tuple) -> None:
    """Register workspace paths for context monitoring.

    Example: rawos study setup /root/rawos /root/sovereign --label rawos --label sovereign
    """
    from rich.console import Console
    console = Console()

    if not watch_paths:
        console.print("[red]Provide at least one path to watch.[/red]")
        console.print("Example: rawos study setup /root/rawos /root/sovereign")
        return

    resp = _api("POST", "/study/setup", json={
        "paths": list(watch_paths),
        "labels": list(label) if label else None,
    })
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    console.print()
    if resp.get("registered"):
        console.print("[green]Registered paths:[/green]")
        for p in resp["registered"]:
            console.print(f"  [cyan]→[/cyan] {p}")
    if resp.get("skipped"):
        console.print("[yellow]Skipped:[/yellow]")
        for p in resp["skipped"]:
            console.print(f"  [yellow]![/yellow] {p}")
    console.print(f"\n[dim]{resp.get('message', '')}[/dim]")


@study.command('status')
def study_status_cmd() -> None:
    """Current study state: day, data counts, watched paths."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    console = Console()

    resp = _api("GET", "/study/status")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    day = resp.get("study_day", 0)
    total_days = 30
    bar_filled = int(day / total_days * 30)
    progress_bar = "[green]" + "█" * bar_filled + "[/green]" + "·" * (30 - bar_filled)

    data_color = "green" if resp.get("data_flowing") else "red"
    data_status = "[green]FLOWING[/green]" if resp.get("data_flowing") else "[red]NO DATA — run setup[/red]"

    console.print()
    console.print(Panel(
        f"[bold]Study day:[/bold]      {day} / {total_days}\n"
        f"[bold]Progress:[/bold]       {progress_bar}\n"
        f"[bold]Start date:[/bold]     {resp.get('study_start', '?')}\n"
        f"[bold]Data status:[/bold]    {data_status}\n\n"
        f"[bold]Context events:[/bold] {resp.get('context_events', 0)}\n"
        f"[bold]Inferences:[/bold]     {resp.get('inferences', 0)}\n"
        f"[bold]Artifacts:[/bold]      {resp.get('artifacts', 0)}\n"
        f"[bold]Rated:[/bold]          {resp.get('rated', 0)}",
        title="[bold cyan]rawos study status[/bold cyan]",
        border_style="cyan",
    ))

    watched = resp.get("watched_paths", [])
    if watched:
        t = Table(title="Watched Paths", box=box.SIMPLE)
        t.add_column("Path", style="cyan")
        t.add_column("Label")
        for w in watched:
            t.add_row(w["path"], w["label"])
        console.print(t)
    else:
        console.print("[yellow]No watched paths. Run: rawos study setup <path>[/yellow]")


@study.command('report')
def study_report_cmd() -> None:
    """Full research report: hypotheses, metrics, trends."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    console = Console()

    resp = _api("GET", "/study/report")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    day = resp.get("study_day", 0)
    stats = resp.get("stats", {})
    hyps = resp.get("hypotheses", {})

    # Summary panel
    p3 = stats.get("precision_at_3")
    p3_str = f"{p3:.1%}" if p3 is not None else "N/A"
    avg_r = stats.get("avg_rating")
    avg_r_str = f"{avg_r:.2f}" if avg_r is not None else "N/A"

    console.print()
    console.print(Panel(
        f"[bold]Day {day}/30  ({resp.get('progress_pct', 0):.0f}%)[/bold]\n\n"
        f"[bold]Precision @3:[/bold]   {p3_str}  (target: ≥65%)\n"
        f"[bold]Avg rating:[/bold]     {avg_r_str} / 5\n"
        f"[bold]Total rated:[/bold]    {stats.get('total_rated', 0)}\n"
        f"[bold]Total artifacts:[/bold]{stats.get('total_artifacts', 0)}\n"
        f"[bold]Total inferences:[/bold]{stats.get('total_inferences', 0)}",
        title="[bold cyan]rawos study report[/bold cyan]",
        border_style="cyan",
    ))

    # Hypothesis table
    h_table = Table(title="Hypothesis Tracking", box=box.SIMPLE_HEAVY)
    h_table.add_column("ID",      style="cyan")
    h_table.add_column("Status",  justify="center")
    h_table.add_column("Value",   justify="right")
    h_table.add_column("Target",  justify="right")
    h_table.add_column("N",       justify="right")

    status_colors = {
        "CONFIRMED":  "[green]CONFIRMED[/green]",
        "REFUTED":    "[red]REFUTED[/red]",
        "COLLECTING": "[yellow]COLLECTING[/yellow]",
        "NO_DATA":    "[dim]NO DATA[/dim]",
    }

    for hid, h in hyps.items():
        val = h.get("current_value")
        val_str = f"{val:.3f}" if isinstance(val, float) else str(val) if val is not None else "—"
        tgt = h.get("target")
        tgt_str = f"{tgt:.2f}" if isinstance(tgt, float) else str(tgt)
        status_str = status_colors.get(h.get("status", ""), h.get("status", ""))
        h_table.add_row(hid, status_str, val_str, tgt_str, str(h.get("samples", 0)))
    console.print(h_table)

    # Source breakdown
    src = stats.get("source_breakdown", {})
    if src:
        s_table = Table(title="Inference Sources", box=box.SIMPLE)
        s_table.add_column("Source", style="cyan")
        s_table.add_column("Count", justify="right")
        for source, n in sorted(src.items()):
            s_table.add_row(source, str(n))
        console.print(s_table)


@cli.command("apply")
@click.argument("fix_file")
def apply_cmd(fix_file: str) -> None:
    """Apply a rawos code fix: show diff, confirm, patch the target file."""
    import difflib
    import shutil
    from rich.console import Console
    from rich.syntax import Syntax
    console = Console()

    fix_path = Path(fix_file).expanduser().resolve()
    if not fix_path.exists():
        console.print(f"[red]Fix file not found: {fix_file}[/red]")
        raise SystemExit(1)

    fix_text = fix_path.read_text(encoding="utf-8", errors="replace")

    # Parse rawos: metadata header — all lines starting with "# rawos:"
    target_file: str | None = None
    fix_description: str = ""
    content_lines: list[str] = []
    past_header = False
    for line in fix_text.splitlines():
        if not past_header and line.startswith("# rawos:"):
            if line.startswith("# rawos:target="):
                target_file = line.split("=", 1)[1].strip()
            elif line.startswith("# rawos:description="):
                fix_description = line.split("=", 1)[1].strip()
        elif not past_header and line.strip() == "":
            past_header = True  # blank line ends the header
        else:
            past_header = True
            content_lines.append(line)

    corrected_content = "\n".join(content_lines)
    if not corrected_content.strip():
        # Fallback: strip all rawos: lines
        corrected_content = "\n".join(
            l for l in fix_text.splitlines()
            if not l.startswith("# rawos:")
        ).lstrip("\n")

    if not target_file:
        console.print("[red]No rawos:target= metadata found in fix file header.[/red]")
        raise SystemExit(1)

    target_path = Path(target_file)
    if not target_path.exists():
        console.print(f"[red]Target file not found: {target_file}[/red]")
        raise SystemExit(1)

    original_content = target_path.read_text(encoding="utf-8", errors="replace")

    diff_lines = list(difflib.unified_diff(
        original_content.splitlines(),
        corrected_content.splitlines(),
        fromfile=f"a/{target_path.name}",
        tofile=f"b/{target_path.name} (rawos fix)",
        lineterm="",
    ))

    if not diff_lines:
        console.print("[yellow]No changes — fix file is identical to target.[/yellow]")
        return

    console.print()
    console.print(f"[bold cyan]rawos apply → {target_path.name}[/bold cyan]")
    if fix_description:
        console.print(f"  Fix: {fix_description}")
    console.print(f"  Source: {fix_path.name}")
    console.print()
    console.print(Syntax("\n".join(diff_lines), "diff", theme="monokai", line_numbers=False))
    console.print()

    if not click.confirm("Apply this fix?"):
        console.print("[dim]Aborted.[/dim]")
        return

    backup_suffix = f".rawos_backup_{int(time.time())}"
    backup_path = target_path.with_name(target_path.name + backup_suffix)
    shutil.copy2(str(target_path), str(backup_path))

    target_path.write_text(corrected_content, encoding="utf-8")
    console.print(f"\n[green]Applied.[/green] Backup saved: {backup_path.name}")



@cli.group()
def trust() -> None:
    """Earned autonomy management — track record, levels, grants."""


@trust.command("status")
def trust_status_cmd() -> None:
    """Current autonomy levels, track record, and upgrade eligibility."""
    from rich.console import Console
    from rich.table import Table
    from rich import box
    console = Console()

    resp = _api("GET", "/trust/status")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    grants = resp.get("grants", [])
    if not grants:
        console.print("[dim]No trust records found.[/dim]")
        return

    t = Table(title="rawos Autonomy Trust Status", box=box.SIMPLE_HEAVY)
    t.add_column("Action Type", style="cyan")
    t.add_column("Level", justify="center")
    t.add_column("Good", justify="right", style="green")
    t.add_column("Bad",  justify="right", style="red")
    t.add_column("Next at", justify="right")
    t.add_column("Eligible", justify="center")

    _LEVEL_LABELS = {
        0: "Insight (analysis only)",
        1: "Draft (code/doc suggestions)",
        2: "Organize (file ops, tests)",
        3: "Commit / Send",
        4: "Execute (scripts, deploy)",
        5: "Full Autonomy",
    }

    for g in grants:
        level    = g.get("level", 0)
        eligible = g.get("eligible_for_upgrade", False)
        threshold = g.get("next_threshold")
        at = g.get("action_type", "")
        eligible_str = f"[green]YES — rawos trust grant {at}[/green]" if eligible else "—"
        next_str = str(threshold) if threshold else "[dim]max[/dim]"
        t.add_row(at, str(level), str(g.get("good_count", 0)), str(g.get("bad_count", 0)), next_str, eligible_str)

    console.print()
    console.print(t)
    console.print()
    for g in grants:
        lv = g.get("level", 0)
        console.print(f"  [bold]{g.get('action_type')}[/bold] → Level {lv}: {_LEVEL_LABELS.get(lv, 'unknown')}")

    # Show which tools are active at current level
    tools_resp = _api("GET", "/trust/tools/status")
    tools = tools_resp.get("tools", [])
    if tools:
        console.print()
        t2 = Table(title="Active Tool Access", box=box.SIMPLE)
        t2.add_column("Tool", style="cyan")
        t2.add_column("Level", justify="center")
        t2.add_column("Status", justify="center")
        t2.add_column("Capability")
        for tool in tools:
            avail = tool.get("available", False)
            status_str = "[green]✓[/green]" if avail else "[dim]locked[/dim]"
            t2.add_row(
                tool["name"],
                str(tool["level_required"]),
                status_str,
                tool.get("description", ""),
            )
        console.print(t2)
        locked = [tool for tool in tools if not tool.get("available")]
        if locked:
            nl = locked[0]
            console.print(
                f"[dim]Next unlock: [cyan]{nl['name']}[/cyan] at Level {nl['level_required']}"
                f" — run 'rawos trust grant analysis'[/dim]"
            )
    console.print()


@trust.command("history")
@click.option("--limit", default=20, help="Number of artifacts to show.")
def trust_history_cmd(limit: int) -> None:
    """Recent rawos artifacts and their ratings."""
    import os
    from rich.console import Console
    from rich.table import Table
    from rich import box
    console = Console()

    resp = _api("GET", f"/trust/history?limit={limit}")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    history = resp.get("history", [])
    if not history:
        console.print("[dim]No artifacts found. rawos will generate them as you work.[/dim]")
        return

    t = Table(title="rawos Action History", box=box.SIMPLE)
    t.add_column("Type",    style="dim",  max_width=10)
    t.add_column("File",    style="cyan", max_width=36)
    t.add_column("Goal",    max_width=36)
    t.add_column("Rating",  justify="center")
    t.add_column("Outcome", justify="center")

    _OUTCOME = {
        "good":    "[green]good[/green]",
        "bad":     "[red]bad[/red]",
        "unrated": "[dim]unrated[/dim]",
    }
    _TYPE_STYLE = {
        "analysis": "[dim]analysis[/dim]",
        "draft":    "[cyan]draft[/cyan]",
    }

    for h in history:
        fname   = os.path.basename(h.get("file_path", ""))
        goal    = (h.get("goal") or "")[:36]
        rating  = str(h.get("rating")) if h.get("rating") else "—"
        outcome = _OUTCOME.get(h.get("outcome", "unrated"), "—")
        atype   = _TYPE_STYLE.get(h.get("action_type", "analysis"), h.get("action_type", ""))
        t.add_row(atype, fname, goal, rating, outcome)

    console.print()
    console.print(t)
    console.print("\n  [dim]Rate artifacts: rawos rate <file> 1-5[/dim]")


@trust.command("grant")
@click.argument("action_type")
def trust_grant_cmd(action_type: str) -> None:
    """Upgrade autonomy level for action_type (requires earned eligibility)."""
    from rich.console import Console
    console = Console()

    resp = _api("POST", "/trust/grant", json={"action_type": action_type})
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    old = resp.get("old_level", 0)
    new = resp.get("new_level", 0)
    _LEVEL_UNLOCK = {
        1: ("write_file",  "rawos can now create and edit files autonomously in your project"),
        2: ("bash",        "rawos can now run any shell command in your workdir (30s timeout, path-isolated)"),
        3: ("fetch_url",   "rawos can now fetch external URLs for context"),
        4: ("deploy",      "rawos can now publish your project workspace to the web"),
    }
    console.print(f"\n[green]Granted: {action_type} Level {old} → {new}[/green]")
    if new in _LEVEL_UNLOCK:
        tool_name, desc = _LEVEL_UNLOCK[new]
        console.print(f"  Tool unlocked: [cyan]{tool_name}[/cyan]")
        console.print(f"  Capability:    {desc}")
    else:
        console.print(f"  Level {new} reached.")
    console.print(f"\n[dim]Run 'rawos tools status' to see all active tools.[/dim]")


@trust.command("revoke")
@click.argument("action_type")
def trust_revoke_cmd(action_type: str) -> None:
    """Immediately reset autonomy level for action_type to 0."""
    import click as _click
    from rich.console import Console
    console = Console()

    if not _click.confirm(f"Revoke all autonomy for '{action_type}'? Level resets to 0."):
        return

    resp = _api("POST", "/trust/revoke", json={"action_type": action_type})
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    console.print(f"\n[yellow]Revoked {action_type}: level reset to 0.[/yellow]")



@cli.group()
def calendar() -> None:
    """Calendar connector — connect CalDAV, view upcoming events, NEEDS_ATTENTION briefings."""


@calendar.command("connect")
def calendar_connect_cmd() -> None:
    """Connect a CalDAV calendar (Google, Apple, Fastmail, Nextcloud, etc.)."""
    import getpass
    from rich.console import Console
    console = Console()

    console.print("[bold]rawos Calendar Connect[/bold]")
    console.print("[dim]Supports any CalDAV provider: Google, Apple, Fastmail, Nextcloud ...[/dim]\n")

    console.print("[dim]Google CalDAV URL example: https://www.google.com/calendar/dav/EMAIL/events[/dim]")
    console.print("[dim]Apple CalDAV URL example:  https://caldav.icloud.com[/dim]")
    url      = click.prompt("CalDAV URL")
    username = click.prompt("Username (usually email)")
    password = getpass.getpass("Password / App Password: ")

    console.print("\n[dim]Connecting...[/dim]")
    resp = _api("POST", "/calendar/connect", json={
        "caldav_url": url,
        "username":   username,
        "password":   password,
    })
    if "error" in resp:
        console.print(f"[red]Error: {resp['error']}[/red]")
        return

    synced = resp.get("events_synced", 0)
    console.print(f"\n[green]Connected.[/green] Synced {synced} upcoming events.")
    console.print("[dim]rawos will now include calendar context and NEEDS_ATTENTION briefings.[/dim]")


@calendar.command("status")
def calendar_status_cmd() -> None:
    """Show calendar connection status and upcoming event count."""
    from rich.console import Console
    console = Console()

    resp = _api("GET", "/calendar/status")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    connected   = resp.get("connected", False)
    username    = resp.get("username", "")
    upcoming    = resp.get("upcoming_24h", 0)
    last_sync   = resp.get("last_sync_ts")
    sync_error  = resp.get("sync_error")

    if not connected:
        console.print("[dim]No calendar connected.[/dim]")
        console.print("  Run [bold]rawos calendar connect[/bold] to connect a CalDAV calendar.")
        return

    import datetime as _dt
    sync_str = ""
    if last_sync:
        dt = _dt.datetime.fromtimestamp(last_sync).strftime("%Y-%m-%d %H:%M:%S")
        sync_str = f"  Last sync: {dt}"

    console.print(f"\n[green]Connected[/green] ({username})")
    console.print(f"  Upcoming 24h events: {upcoming}")
    if sync_str:
        console.print(sync_str)
    if sync_error:
        console.print(f"  [yellow]Sync warning: {sync_error}[/yellow]")


@calendar.command("events")
@click.option("--hours", default=24, help="Look-ahead window in hours.")
def calendar_events_cmd(hours: int) -> None:
    """List upcoming calendar events."""
    from rich.console import Console
    from rich.table import Table
    from rich import box
    import datetime as _dt
    console = Console()

    resp = _api("GET", f"/calendar/events?hours={hours}")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    events = resp.get("events", [])
    if not events:
        console.print(f"[dim]No events in the next {hours}h.[/dim]")
        return

    t = Table(title=f"Upcoming events (next {hours}h)", box=box.SIMPLE)
    t.add_column("Time",     style="cyan", min_width=16)
    t.add_column("Title",    max_width=40)
    t.add_column("Location", max_width=24, style="dim")
    t.add_column("Briefed",  justify="center")

    for ev in events:
        start = ev.get("start_ts", 0)
        dt_str = _dt.datetime.fromtimestamp(start).strftime("%a %H:%M") if start else "?"
        title    = ev.get("title", "(no title)")[:40]
        location = (ev.get("location") or "")[:24]
        briefed  = "[green]yes[/green]" if ev.get("briefed_today") else "[dim]—[/dim]"
        t.add_row(dt_str, title, location, briefed)

    console.print()
    console.print(t)


@calendar.command("disconnect")
def calendar_disconnect_cmd() -> None:
    """Disconnect calendar and remove all stored calendar data."""
    import click as _click
    from rich.console import Console
    console = Console()

    if not _click.confirm("Disconnect calendar and delete all stored calendar data?"):
        return

    resp = _api("DELETE", "/calendar/disconnect")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    console.print("[yellow]Calendar disconnected. All calendar data removed.[/yellow]")



@cli.group()
def tools() -> None:
    """Inspect rawos tool access — what it can do and what it has done."""


@tools.command("status")
def tools_status_cmd() -> None:
    """Show every tool rawos knows, the trust level required, and whether active now."""
    from rich.console import Console
    from rich.table import Table
    from rich import box
    console = Console()

    resp = _api("GET", "/trust/tools/status")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    current_level = resp.get("current_level", 0)
    tools_list = resp.get("tools", [])

    t = Table(
        title=f"rawos Tool Access  (analysis trust level: {current_level})",
        box=box.SIMPLE_HEAVY,
    )
    t.add_column("Tool",           style="cyan")
    t.add_column("Level Required", justify="center")
    t.add_column("Status",         justify="center")
    t.add_column("Capability")

    for tool in tools_list:
        avail = tool.get("available", False)
        status_str = "[green]✓ active[/green]" if avail else "[dim]🔒 locked[/dim]"
        t.add_row(
            tool["name"],
            str(tool["level_required"]),
            status_str,
            tool.get("description", ""),
        )

    console.print()
    console.print(t)

    locked = [tool for tool in tools_list if not tool.get("available")]
    if locked:
        nl = locked[0]
        console.print(
            f"\n[dim]Next unlock: [cyan]{nl['name']}[/cyan] at Level {nl['level_required']}"
            f" — run 'rawos trust grant analysis'[/dim]"
        )
    console.print()


@tools.command("history")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of tool calls to show.")
def tools_history_cmd(limit: int) -> None:
    """Show tool calls rawos made autonomously, grouped by the artifact they produced."""
    import datetime as _dt
    from collections import OrderedDict
    from rich.console import Console
    console = Console()

    resp = _api("GET", f"/trust/tools/history?limit={limit}")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    calls = resp.get("calls", [])
    if not calls:
        console.print(
            "[dim]No autonomous tool calls recorded yet. "
            "rawos logs every tool it uses — check back after the next proactive artifact.[/dim]"
        )
        return

    # Group calls by artifact_file, preserving first-seen order
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for c in calls:
        art = c.get("artifact_file") or ""
        key = art.split("/")[-1] if art else "[unlinked]"
        groups.setdefault(key, []).append(c)

    console.print()
    console.print(f"[bold]rawos Autonomous Tool Calls[/bold] — last {limit}")
    console.print()

    for artifact_name, group_calls in groups.items():
        # Artifact header with timestamp of first call in group
        ts = group_calls[0].get("called_at", 0)
        when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
        if artifact_name == "[unlinked]":
            console.print(f"  [dim]▸ [unlinked]  ({when})[/dim]")
        else:
            console.print(f"  [bold cyan]▸ {artifact_name}[/bold cyan]  [dim]({when})[/dim]")

        for idx, c in enumerate(group_calls):
            is_last = idx == len(group_calls) - 1
            branch = "└──" if is_last else "├──"
            tool = c.get("tool_name", "")
            inp  = c.get("input_preview", "")
            ok   = "[green]✓[/green]" if c.get("success") else "[red]✗[/red]"
            ms   = c.get("duration_ms", 0)
            # Format output size
            out_sz = c.get("output_size", 0)
            sz_str = (
                f"{out_sz // 1024:.1f}KB" if out_sz >= 1024
                else f"{out_sz}B"
            ) if out_sz else "—"
            console.print(
                f"    {branch} [cyan]{tool:<14}[/cyan]"
                f" [dim]{inp[:52]:<52}[/dim]"
                f" → [dim]{sz_str:>6}[/dim]  {ok}  [dim]{ms}ms[/dim]"
            )

        console.print()



@cli.command("commits")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of commits to show.")
def commits_cmd(limit: int) -> None:
    """Show git commits rawos made autonomously, newest first."""
    import datetime as _dt
    from rich.console import Console
    from rich.table import Table
    from rich import box
    console = Console()

    resp = _api("GET", f"/trust/commits?limit={limit}")
    if "error" in resp:
        console.print(f"[red]{resp['error']}[/red]")
        return

    commits = resp.get("commits", [])
    if not commits:
        console.print(
            "[dim]No autonomous commits yet. "
            "rawos commits fixes at trust Level 2 — "
            "run 'rawos trust status' to check your level.[/dim]"
        )
        return

    t = Table(
        title=f"rawos Autonomous Commits — last {limit}",
        box=box.SIMPLE_HEAVY,
    )
    t.add_column("When",    style="dim")
    t.add_column("Hash",    style="yellow", no_wrap=True)
    t.add_column("Branch",  style="cyan",   max_width=30)
    t.add_column("Message", max_width=55)
    t.add_column("Project", style="dim",    max_width=20)

    for c in commits:
        ts = c.get("created_at", 0)
        when = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
        t.add_row(
            when,
            c.get("commit_hash", "?")[:8],
            c.get("branch", ""),
            c.get("message", ""),
            c.get("project_name") or "—",
        )

    console.print()
    console.print(t)
    console.print(
        f"[dim]To revert: git revert <hash>  "
        f"  To view: git show <hash>[/dim]"
    )
    console.print()


@cli.command()
@click.argument("message")
def ask(message: str) -> None:
    """Ask rawos a question or give a goal; stream the response live."""
    from rich.console import Console
    console = Console()
    project_id = _resolve_project_id()
    for event in _api_stream("/intent", {"project_id": project_id, "message": message}):
        _render_event(event, console)
    console.print()


def _show_session_digest(console: Any) -> None:
    """Call session_start, print digest if proactive work was done since last chat."""
    try:
        data = _api("post", "/context/session_start")
    except SystemExit:
        return
    artifacts = data.get("artifacts") or []
    if not artifacts:
        return
    console.print("\n[bold cyan]While you were away:[/bold cyan]")
    for item in artifacts:
        goal = item.get("goal", "")
        confidence = item.get("confidence", 0.0)
        console.print(f"  [dim]•[/dim] {goal} [dim](confidence: {confidence:.0%})[/dim]")
    console.print()

@cli.command()
def chat() -> None:
    """Interactive multi-turn REPL.  Type :q or exit to quit."""
    from rich.console import Console
    console = Console()
    project_id = _resolve_project_id()
    _show_session_digest(console)
    while True:
        try:
            message = click.prompt("rawos>", prompt_suffix=" ")
        except (click.Abort, EOFError):
            break
        if message.strip() in (":q", "exit"):
            break
        if not message.strip():
            continue
        for event in _api_stream("/intent", {"project_id": project_id, "message": message}):
            _render_event(event, console)
        console.print()




# ---------------------------------------------------------------------------
# frontdoor command group
# ---------------------------------------------------------------------------

@cli.group()
def frontdoor() -> None:
    """Manage the rawos front-door (login = being).

    When installed, an interactive SSH login to this host launches the rawos
    AI session instead of a raw shell. Any explicit SSH command (scp, rsync,
    git, bash) passes through unchanged — the front-door never breaks tooling.
    """


@frontdoor.command("enter")
def frontdoor_enter() -> None:
    """ForceCommand target — decides and exec()s the entry action.

    Called by sshd for every login after `rawos frontdoor install` has
    activated.  Must not be called manually; has no interactive UI.

    Exit codes mirror standard shell conventions so scp/rsync/git see no
    difference from a normal shell.
    """
    import os
    import sys

    import httpx

    from rawos.kernel.frontdoor import (
        EntryActionKind,
        FrontDoorPolicy,
        decide_entry,
    )

    ssh_cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    creds = _load_creds()
    has_token = bool(creds.get("access_token", ""))

    # Probe /health — fail-open: if probe itself fails, mark unhealthy
    try:
        resp = httpx.get(
            _DEFAULT_URL.rstrip("/") + "/health",
            timeout=2.0,
        )
        rawos_healthy = resp.status_code == 200
    except Exception:
        rawos_healthy = False

    audit_dir = _CONFIG_DIR / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    policy = FrontDoorPolicy(
        fail_open=True,
        health_url=_DEFAULT_URL.rstrip("/") + "/health",
        audit_path=str(audit_dir / "frontdoor.log"),
    )
    ctx = {
        "ssh_original_command": ssh_cmd,
        "rawos_healthy": rawos_healthy,
        "has_token": has_token,
    }
    action = decide_entry(ctx, policy)

    shell = os.environ.get("SHELL", "/bin/bash")

    if action.kind == EntryActionKind.PASSTHROUGH:
        os.execvp(shell, [shell, "-c", action.command])

    elif action.kind == EntryActionKind.LAUNCH_CHAT:
        # exec into rawos chat — replaces this process so the session
        # becomes the chat process (no double shell)
        rawos_bin = sys.argv[0]
        os.execvp(rawos_bin, [rawos_bin, "chat"])

    else:  # FAIL_OPEN_SHELL
        click.echo(
            "\n⚠  rawos is unavailable or not authenticated. "
            "Dropping to raw shell.\n",
            err=True,
        )
        os.execvp(shell, [shell, "--login"])


@frontdoor.command("install")
@click.option(
    "--revert-after",
    default=300,
    show_default=True,
    help="Seconds until the dead-man's-switch auto-reverts if not committed.",
)
def frontdoor_install(revert_after: int) -> None:
    """Install the front-door with an auto-revert safety harness.

    The front-door goes LIVE immediately but will self-revert in --revert-after
    seconds unless you run `rawos frontdoor commit`.

    Verify sequence (do this in a NEW terminal before committing):
    \b
      ssh root@<host>                  → should land in rawos chat
      ssh -t root@<host> bash          → should drop to raw shell (escape)
      scp / rsync / git                → should still work
      systemctl stop rawos && ssh ...  → should drop to shell + notice (fail-open)
      systemctl start rawos

    Once all checks pass:  rawos frontdoor commit
    """
    from rawos.kernel.arch.linux import LinuxFrontDoor
    from rawos.kernel.frontdoor import FrontDoorInstallError, install_with_deadman

    import shutil as _sh
    rawos_bin = _sh.which("rawos") or os.path.abspath(sys.argv[0])
    entry_cmd = f"{rawos_bin} frontdoor enter"

    arch = LinuxFrontDoor()

    click.echo(f"Installing front-door (revert in {revert_after}s if not committed)…")
    try:
        install_with_deadman(arch, entry_cmd, revert_after_s=revert_after)
    except FrontDoorInstallError as exc:
        click.echo(f"✗ {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo("✓ Front-door LIVE and armed.")
    click.echo(f"  Verify in a NEW terminal, then:  rawos frontdoor commit")
    click.echo(f"  Auto-reverts in {revert_after}s if you do nothing.")


@frontdoor.command("commit")
def frontdoor_commit() -> None:
    """Disarm the auto-revert timer after verifying the front-door works.

    Run this only after a new SSH session has confirmed:
    - interactive login lands in the AI, and
    - escape hatch (ssh -t host bash) drops to a raw shell.
    """
    from rawos.kernel.frontdoor import commit

    commit()
    click.echo("✓ Auto-revert disarmed. Front-door is permanent until uninstalled.")


@frontdoor.command("status")
def frontdoor_status() -> None:
    """Show current front-door installation state."""
    from rawos.kernel.arch.linux import LinuxFrontDoor

    state = LinuxFrontDoor().state()
    if state.installed:
        click.echo(f"installed: yes")
        click.echo(f"entry_command: {state.entry_command}")
        click.echo(f"config_path: {state.config_path}")
    else:
        click.echo("installed: no")


@frontdoor.command("uninstall")
def frontdoor_uninstall() -> None:
    """Remove the front-door configuration and reload sshd.

    After this, interactive SSH logins return to the default shell.
    Requires a running SSH session (does not use the dead-man's-switch;
    uninstall is a manual, deliberate action).
    """
    from rawos.kernel.arch.linux import LinuxFrontDoor

    arch = LinuxFrontDoor()
    arch.uninstall()
    if arch.validate():
        arch.reload()
        click.echo("✓ Front-door removed and sshd reloaded.")
    else:
        click.echo(
            "✗ sshd -t failed after uninstall (unexpected). "
            "Manual check required.",
            err=True,
        )
        raise SystemExit(1)


@frontdoor.command("_revert", hidden=True)
@click.argument("snapshot")
def frontdoor_revert(snapshot: str) -> None:
    """Internal: restore a snapshot and reload sshd (used by the dead-man's-switch timer)."""
    from rawos.kernel.arch.linux import LinuxFrontDoor

    arch = LinuxFrontDoor()
    arch.restore(snapshot)
    if arch.validate():
        arch.reload()
    # Intentionally silent — this runs as a systemd transient unit

def main() -> None:
    cli()


if __name__ == "__main__":
    main()
