"""
tools/java_parser_wrapper.py
Lightweight AST-level Java file manipulator using the `javalang` library.
Supports adding methods, fields, annotations, and imports to existing .java files.
"""

from __future__ import annotations

import re
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MethodDef:
    name: str
    return_type: str
    params: list[tuple[str, str]]   # [(type, name), ...]
    body: str
    annotations: list[str] = None
    access: str = "public"
    throws: list[str] = None

    def to_java(self, indent: int = 4) -> str:
        pad = " " * indent
        lines = []
        if self.annotations:
            for ann in self.annotations:
                lines.append(f"{pad}{ann}")
        param_str = ", ".join(f"{t} {n}" for t, n in self.params)
        throws_str = ""
        if self.throws:
            throws_str = " throws " + ", ".join(self.throws)
        lines.append(f"{pad}{self.access} {self.return_type} {self.name}({param_str}){throws_str} {{")
        for body_line in self.body.strip().splitlines():
            lines.append(f"{pad}    {body_line}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)


@dataclass
class FieldDef:
    name: str
    field_type: str
    annotations: list[str] = None
    access: str = "private"
    initial_value: Optional[str] = None

    def to_java(self, indent: int = 4) -> str:
        pad = " " * indent
        lines = []
        if self.annotations:
            for ann in self.annotations:
                lines.append(f"{pad}{ann}")
        init = f" = {self.initial_value}" if self.initial_value else ""
        lines.append(f"{pad}{self.access} {self.field_type} {self.name}{init};")
        return "\n".join(lines)


class JavaFileEditor:
    """
    Text-based (not full AST) Java file editor.
    Works on raw source text, locating class body boundaries with regex.
    """

    def __init__(self, path: Path):
        self.path = path
        self.source = path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _find_class_body_end(self) -> int:
        """Return the index of the last '}' in the file (end of outermost class)."""
        return self.source.rfind("}")

    def _add_import(self, fqn: str) -> bool:
        """Add an import statement if not already present."""
        import_stmt = f"import {fqn};"
        if import_stmt in self.source:
            return False
        # Insert after last existing import or after package declaration
        last_import = list(re.finditer(r"^import .+;", self.source, re.MULTILINE))
        if last_import:
            pos = last_import[-1].end()
            self.source = self.source[:pos] + "\n" + import_stmt + self.source[pos:]
        else:
            pkg_match = re.search(r"^package .+;", self.source, re.MULTILINE)
            if pkg_match:
                pos = pkg_match.end()
                self.source = self.source[:pos] + "\n\n" + import_stmt + self.source[pos:]
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_method(self, method: MethodDef, imports: list[str] = None) -> "JavaFileEditor":
        """Append a method before the last '}' of the class."""
        for imp in (imports or []):
            self._add_import(imp)

        end = self._find_class_body_end()
        method_src = "\n" + method.to_java(indent=4) + "\n"
        self.source = self.source[:end] + method_src + self.source[end:]
        return self

    def add_field(self, field: FieldDef, imports: list[str] = None) -> "JavaFileEditor":
        """Insert a field right after the opening '{' of the class body."""
        for imp in (imports or []):
            self._add_import(imp)

        # Find opening brace of the class
        m = re.search(r"(class|interface|enum)\s+\w[^{]*\{", self.source)
        if not m:
            logger.error("Cannot locate class opening brace")
            return self

        insert_pos = m.end()
        field_src = "\n" + field.to_java(indent=4) + "\n"
        self.source = self.source[:insert_pos] + field_src + self.source[insert_pos:]
        return self

    def add_class_annotation(self, annotation: str) -> "JavaFileEditor":
        """Add annotation before the class declaration."""
        m = re.search(r"^(public\s+)?(abstract\s+)?(class|interface|enum)\s+", self.source, re.MULTILINE)
        if not m:
            return self
        insert_pos = m.start()
        if annotation not in self.source:
            self.source = self.source[:insert_pos] + annotation + "\n" + self.source[insert_pos:]
        return self

    def add_interface_to_implements(self, iface: str) -> "JavaFileEditor":
        """Add interface to implements clause or create one."""
        # If already implements something
        m = re.search(r"implements\s+([\w,\s<>]+)\s*\{", self.source)
        if m:
            current = m.group(1).strip()
            if iface not in current:
                new_impl = current + ", " + iface
                self.source = self.source[:m.start()] + f"implements {new_impl} " + self.source[m.end() - 1:]
        else:
            # Add before opening brace of class
            m2 = re.search(r"(class\s+\w+[^{]*)\{", self.source)
            if m2:
                self.source = self.source[:m2.end() - 1] + f"implements {iface} " + self.source[m2.end() - 1:]
        return self

    def has_method(self, method_name: str) -> bool:
        pattern = rf"\b{re.escape(method_name)}\s*\("
        return bool(re.search(pattern, self.source))

    def has_annotation(self, annotation: str) -> bool:
        return annotation in self.source

    def save(self):
        self.path.write_text(self.source, encoding="utf-8")
        logger.info(f"Saved: {self.path}")

    def diff(self, original_source: str) -> str:
        """Return a unified diff string comparing original_source to current source."""
        import difflib
        diff = difflib.unified_diff(
            original_source.splitlines(keepends=True),
            self.source.splitlines(keepends=True),
            fromfile=str(self.path) + " (original)",
            tofile=str(self.path) + " (modified)",
        )
        return "".join(diff)


def edit_java_file(path: Path) -> JavaFileEditor:
    return JavaFileEditor(path)