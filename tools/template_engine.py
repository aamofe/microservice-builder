"""
tools/template_engine.py
Jinja2-based code generator that renders templates from the templates/ directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import logging

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


class TemplateEngine:
    def __init__(self, templates_dir: Path = TEMPLATES_DIR):
        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_path: str, context: dict) -> str:
        """
        Render a Jinja2 template.
        template_path is relative to templates/, e.g. 'spring-boot-module/pom.xml.j2'
        """
        try:
            tmpl = self.env.get_template(template_path)
            return tmpl.render(**context)
        except Exception as e:
            logger.error(f"Template rendering failed for {template_path}: {e}")
            raise

    def render_string(self, source: str, context: dict) -> str:
        """Render an inline template string."""
        tmpl = self.env.from_string(source)
        return tmpl.render(**context)

    def write_rendered(self, template_path: str, context: dict, output_path: Path, overwrite: bool = False) -> bool:
        """Render template and write to output_path. Returns True if written."""
        if output_path.exists() and not overwrite:
            logger.warning(f"Skipping existing file: {output_path}")
            return False
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.render(template_path, context)
        output_path.write_text(content, encoding="utf-8")
        logger.info(f"Written: {output_path}")
        return True


# Singleton
_engine: TemplateEngine | None = None


def get_engine() -> TemplateEngine:
    global _engine
    if _engine is None:
        _engine = TemplateEngine()
    return _engine