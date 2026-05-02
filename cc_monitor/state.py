"""UI state — collapse/expand and sort. Independent of parser/view."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ViewState:
    expanded: dict[str, bool] = field(
        default_factory=lambda: {"mcp": False, "files": False, "results": False}
    )
    sort_mode: str = "size"  # "size" | "name"

    def toggle(self, key: str) -> None:
        if key in self.expanded:
            self.expanded[key] = not self.expanded[key]

    def toggle_sort(self) -> None:
        self.sort_mode = "name" if self.sort_mode == "size" else "size"

    def is_expanded(self, key: str) -> bool:
        return self.expanded.get(key, False)
