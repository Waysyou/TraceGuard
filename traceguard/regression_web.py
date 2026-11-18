"""Local problem library UI, served by the workbench."""
from pathlib import Path

PAGE = (Path(__file__).parent / "web" / "regression.html").read_text(encoding="utf-8")
