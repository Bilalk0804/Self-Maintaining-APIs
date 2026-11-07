"""Command-line entry points for the Driftwatch spike pipeline."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import psycopg
import typer
from rich.console import Console
from rich.table import Table

from Driftwatch.Fix import Codemods as codemods_mod
from Driftwatch.Fix.Splice import apply_edits, diff_stats, unified_diff, verify_patch
from Driftwatch.Http.ReviewApp import serve_review
from Driftwatch.Mapping.Model import SurfaceMap
from Driftwatch.Match.Scorer import decide
from Driftwatch.Services.PayPalDemo import run_paypal_demo
from Driftwatch.Services.Pipeline import PipelineService
from Driftwatch.Surfaces import SurfaceDescriptor
from Driftwatch.Views.Console import ConsoleView
from Driftwatch.Watch.Differ import Delta
from Driftwatch.Watch.OpenapiIndexer import build_index, load_openapi

app = typer.Typer(
    add_completion=False,
    help="Detect upstream API drift and patch the code it breaks.",
)
console = Console()
service = PipelineService()
view = ConsoleView(console)


@app.command("paypal-demo")
def paypal_demo(
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        envvar="DRIFTWATCH_DATABASE_URL",
        help="PostgreSQL connection string.",
    ),
    force_refresh: bool = typer.Option(
        False,
        "--force-refresh",
        help="Clear PayPal watermarks and fetch fresh repository archives.",
    ),
) -> None:
    """Run the real PayPal repository -> snapshot -> index demo."""
    configured_url = database_url or os.getenv("DRIFTWATCH_TEST_DATABASE_URL")
    if not configured_url:
        raise typer.BadParameter("provide --database-url or set DRIFTWATCH_DATABASE_URL")
    with psycopg.connect(configured_url) as connection:
        results = run_paypal_demo(connection, force_refresh=force_refresh)
    table = Table(title="PayPal pipeline demo")
    table.add_column("source")
    table.add_column("status")
    table.add_column("indexed operations", justify="right")
    table.add_column("detected changes", justify="right")
    for result in results:
        table.add_row(
            result.kind,
            result.status,
            str(result.indexed_entries),
            "baseline" if result.change_count is None else str(result.change_count),
        )
    console.print(table)


@app.command("derive-map")
def derive_map(
    sdk_root: Path = typer.Option(..., help="Checkout of the provider SDK."),
    provider: str = typer.Option("stripe"),
    out: Path = typer.Option(Path("surface_map.json")),
    spec: Path | None = typer.Option(None, help="Optional spec to cross-validate against."),
) -> None:
    """Derive a Surface Map from generated SDK source."""
    result = service.derive_map(sdk_root, provider=provider)
    smap = result.value
    smap.save(out)
    view.show_map_summary(smap, provider, result.elapsed_seconds)

    if spec is not None:
        index = build_index(load_openapi(spec.read_bytes()), provider, "validation")
        spec_ops = {r for r in index.entries if "/op:" in r and "/param:" not in r}
        map_ops = {e.surface_ref for e in smap.entries}
        hit = map_ops & spec_ops
        console.print(
            f"cross-validation: [bold]{100 * len(hit) / max(len(map_ops), 1):.1f}%[/] of derived "
            f"operations exist in the spec; "
            f"[bold]{100 * len(hit) / max(len(spec_ops), 1):.1f}%[/] of spec operations covered"
        )
    console.print(f"written to [bold]{out}[/]")


@app.command("diff-spec")
def diff_spec(
    before: Path = typer.Option(..., help="Earlier OpenAPI document."),
    after: Path = typer.Option(..., help="Later OpenAPI document."),
    provider: str = typer.Option("stripe"),
    out: Path = typer.Option(Path("deltas.json")),
    include_additive: bool = typer.Option(False, help="Keep purely additive changes."),
) -> None:
    """Diff two revisions of an OpenAPI document into classified changes."""
    result = service.diff_specs(
        before,
        after,
        provider=provider,
        include_additive=include_additive,
    )
    index_before, index_after, deltas = result.value
    console.print(
        f"indexed {len(index_before):,} -> {len(index_after):,} surfaces "
        f"in {result.elapsed_seconds:.2f}s; {len(deltas)} changes"
    )
    view.show_deltas(deltas, provider)

    out.write_text(json.dumps([_delta_to_json(d) for d in deltas], indent=1), encoding="utf-8")
    console.print(f"written to [bold]{out}[/]")


@app.command("scan")
def scan(
    repo: Path = typer.Option(..., help="Repository to scan."),
    changes: Path = typer.Option(..., help="Changes produced by diff-spec."),
    surface_map: Path = typer.Option(..., help="Surface Map produced by derive-map."),
    mode: str = typer.Option("pr_ready", help="Repository action ceiling."),
    out: Path = typer.Option(Path("findings.json")),
) -> None:
    """Scan a repository for code affected by a set of changes."""
    smap = SurfaceMap.load(surface_map)
    deltas = [_delta_from_json(d) for d in json.loads(changes.read_text(encoding="utf-8"))]
    scan_result = service.scan(repo, deltas, smap)
    result = scan_result.value

    if result.skipped_reason:
        console.print(f"[yellow]skipped:[/] {result.skipped_reason}")
        return

    console.print(
        f"parsed {result.files_parsed}/{result.files_considered} candidate files in "
        f"{scan_result.elapsed_seconds:.2f}s; test runner: {result.test_runner or 'none'}"
    )
    table = Table(title=f"Findings in {repo}")
    table.add_column("conf", justify="right")
    table.add_column("action")
    table.add_column("type")
    table.add_column("location", overflow="fold")
    table.add_column("match")
    for finding in result.findings:
        action, _why = decide(
            finding,
            repo_mode=mode,  # type: ignore[arg-type]
            has_tests=result.has_tests,
            tests_passed=True,
            patch_from_codemod=finding.auto_fixable,
            diff_lines=2,
            diff_files=1,
        )
        table.add_row(
            f"{finding.confidence:.3f}",
            action,
            finding.change_type,
            f"{finding.usage.file_path}:{finding.usage.start_line}",
            finding.usage.match_kind,
        )
    console.print(table)

    payload = [
        {
            "fingerprint": f.fingerprint,
            "change_type": f.change_type,
            "severity": f.severity,
            "surface_ref": f.surface_ref,
            "renamed_to": f.renamed_to,
            "confidence": f.confidence,
            "score_factors": f.score_factors,
            "detail": f.detail,
            "change_fingerprint": f.change_fingerprint,
            "usage": asdict(f.usage),
        }
        for f in result.findings
    ]
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    console.print(f"written to [bold]{out}[/] ({len(payload)} findings)")


@app.command("review")
def review(
    root: Path = typer.Option(
        Path(".cache/reviews"),
        help="Directory of one findings JSON per repository.",
    ),
    host: str = typer.Option("127.0.0.1", help="Loopback address to bind."),
    port: int = typer.Option(8765, min=1, max=65535),
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        envvar="DRIFTWATCH_DATABASE_URL",
        help="Postgres URL. When set, the page searches provider sources on their schedule.",
    ),
) -> None:
    """Open the desk and search provider sources without further input."""
    try:
        serve_review(root, host=host, port=port, database_url=database_url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("fix")
def fix(
    repo: Path = typer.Option(..., help="Repository the findings came from."),
    findings: Path = typer.Option(..., help="Findings produced by scan."),
    out: Path = typer.Option(Path("patch.diff")),
    replacement: list[str] = typer.Option([], help="Literal swap as old=new, repeatable."),
) -> None:
    """Generate a byte-splice patch for every auto-fixable finding."""
    from Driftwatch.Match.Resolver import Usage
    from Driftwatch.Match.Scorer import Finding

    replacements = dict(r.split("=", 1) for r in replacement if "=" in r)
    registry = codemods_mod.default_registry(replacements)

    raw = json.loads(findings.read_text(encoding="utf-8"))
    edits_by_file: dict[str, list] = {}
    applied: list[tuple[str, str, str]] = []

    repo_root = repo.resolve()
    for item in raw:
        finding = Finding(
            change_fingerprint=item["change_fingerprint"],
            change_type=item["change_type"],
            severity=item["severity"],
            surface_ref=item["surface_ref"],
            usage=Usage(**item["usage"]),
            confidence=item["confidence"],
            renamed_to=item.get("renamed_to"),
            detail=item.get("detail", ""),
        )
        candidate = repo / finding.usage.file_path
        if candidate.is_symlink():
            continue
        path = candidate.resolve()
        if not path.is_relative_to(repo_root) or not path.is_file():
            continue
        name, edits = codemods_mod.plan_edits(finding, path.read_bytes(), registry)
        if not edits:
            continue
        edits_by_file.setdefault(finding.usage.file_path, []).extend(edits)
        applied.append((finding.usage.file_path, name, edits[0].rationale))

    if not applied:
        console.print("[yellow]no codemod applied to any finding[/]")
        return

    diff_text = ""
    total_added = total_removed = 0
    for rel, edits in sorted(edits_by_file.items()):
        original = (repo / rel).read_bytes()
        patched = apply_edits(original, edits)
        verify_patch(patched, len(edits))
        file_diff = unified_diff(original, patched, rel)
        added, removed = diff_stats(file_diff)
        total_added += added
        total_removed += removed
        diff_text += file_diff

    table = Table(title="Applied codemods")
    table.add_column("file")
    table.add_column("codemod")
    table.add_column("rationale", overflow="fold")
    for rel, name, rationale in applied:
        table.add_row(rel, name, rationale)
    console.print(table)
    console.print(
        f"{len(edits_by_file)} file(s), [green]+{total_added}[/] / [red]-{total_removed}[/] lines; "
        "patched sources reparse cleanly"
    )
    out.write_text(diff_text, encoding="utf-8")
    console.print(f"written to [bold]{out}[/]")


def _delta_to_json(delta: Delta) -> dict:
    """Serialise a delta, including its computed fingerprint and probes."""
    return {
        "surface_ref": delta.surface_ref,
        "change_type": delta.change_type,
        "severity": delta.severity,
        "op": delta.op,
        "before": asdict(delta.before) if delta.before else None,
        "after": asdict(delta.after) if delta.after else None,
        "changed_attrs": list(delta.changed_attrs),
        "renamed_to": delta.renamed_to,
        "detail": delta.detail,
        "fingerprint": delta.fingerprint,
        "code_probes": delta.code_probes(),
    }


def _delta_from_json(raw: dict) -> Delta:
    """Rebuild a delta from its serialised form."""
    return Delta(
        surface_ref=raw["surface_ref"],
        change_type=raw["change_type"],
        severity=raw["severity"],
        op=raw["op"],
        before=SurfaceDescriptor(**raw["before"]) if raw.get("before") else None,
        after=SurfaceDescriptor(**raw["after"]) if raw.get("after") else None,
        changed_attrs=tuple(raw.get("changed_attrs") or ()),
        renamed_to=raw.get("renamed_to"),
        detail=raw.get("detail", ""),
    )


if __name__ == "__main__":
    app()
