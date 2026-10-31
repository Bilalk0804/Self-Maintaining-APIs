"""Rich console rendering kept outside application and domain services."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from Driftwatch.Mapping.Model import SurfaceMap
from Driftwatch.Watch.Differ import Delta

SEVERITY_STYLE = {
    "breaking": "bold red",
    "deadline": "bold magenta",
    "behavioral": "yellow",
    "deprecating": "cyan",
    "additive": "dim",
}


class ConsoleView:
    """Render use-case results for the command-line controller."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def show_map_summary(
        self,
        surface_map: SurfaceMap,
        provider: str,
        elapsed_seconds: float,
    ) -> None:
        """Render a derived surface-map summary."""
        table = Table(title=f"Surface Map: {provider}", show_header=False)
        table.add_row("bindings", f"{len(surface_map.entries):,}")
        table.add_row("operations", f"{surface_map.operation_count:,}")
        table.add_row("ops with params", f"{len(surface_map.op_params):,}")
        table.add_row("ops with response types", f"{len(surface_map.op_returns):,}")
        table.add_row("elapsed", f"{elapsed_seconds:.2f}s")
        self.console.print(table)

    def show_deltas(self, deltas: list[Delta], provider: str) -> None:
        """Render the most relevant detected changes."""
        table = Table(title="Detected changes")
        table.add_column("severity")
        table.add_column("type")
        table.add_column("surface", overflow="fold")
        table.add_column("detail", overflow="fold")
        for delta in deltas[:40]:
            style = SEVERITY_STYLE.get(delta.severity, "")
            table.add_row(
                f"[{style}]{delta.severity}[/]",
                delta.change_type,
                delta.surface_ref.removeprefix(f"api:{provider}/"),
                delta.detail[:70],
            )
        self.console.print(table)
        if len(deltas) > 40:
            self.console.print(f"... {len(deltas) - 40} more")
