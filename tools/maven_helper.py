"""
tools/maven_helper.py
Reads and modifies pom.xml files (parent and child modules).
"""

from __future__ import annotations

import logging
from pathlib import Path
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

ET.register_namespace("", "http://maven.apache.org/POM/4.0.0")
MAVEN_NS = "http://maven.apache.org/POM/4.0.0"
NS = {"m": MAVEN_NS}


def _tag(name: str) -> str:
    return f"{{{MAVEN_NS}}}{name}"


class PomEditor:
    def __init__(self, pom_path: Path):
        self.path = pom_path
        self.tree = ET.parse(str(pom_path))
        self.root = self.tree.getroot()

    def _get_or_create(self, parent: ET.Element, tag: str) -> ET.Element:
        el = parent.find(f"m:{tag}", NS)
        if el is None:
            el = ET.SubElement(parent, _tag(tag))
        return el

    def add_module(self, module_name: str) -> bool:
        """Add <module>module_name</module> to <modules> section."""
        modules_el = self._get_or_create(self.root, "modules")
        for m in modules_el.findall(f"m:module", NS):
            if m.text == module_name:
                return False  # already exists
        mod_el = ET.SubElement(modules_el, _tag("module"))
        mod_el.text = module_name
        logger.info(f"Added module '{module_name}' to pom.xml")
        return True

    def add_dependency(self, group_id: str, artifact_id: str, version: str = None,
                       scope: str = None) -> bool:
        """Add dependency to <dependencies> section if not already present."""
        deps_el = self._get_or_create(self.root, "dependencies")
        for dep in deps_el.findall("m:dependency", NS):
            g = dep.find("m:groupId", NS)
            a = dep.find("m:artifactId", NS)
            if g is not None and a is not None and g.text == group_id and a.text == artifact_id:
                return False  # already exists

        dep_el = ET.SubElement(deps_el, _tag("dependency"))
        g_el = ET.SubElement(dep_el, _tag("groupId"))
        g_el.text = group_id
        a_el = ET.SubElement(dep_el, _tag("artifactId"))
        a_el.text = artifact_id
        if version:
            v_el = ET.SubElement(dep_el, _tag("version"))
            v_el.text = version
        if scope:
            s_el = ET.SubElement(dep_el, _tag("scope"))
            s_el.text = scope
        logger.info(f"Added dependency {group_id}:{artifact_id}")
        return True

    def add_dependency_management(self, group_id: str, artifact_id: str,
                                   version: str, dep_type: str = None) -> bool:
        dm_el = self._get_or_create(self.root, "dependencyManagement")
        deps_el = self._get_or_create(dm_el, "dependencies")
        for dep in deps_el.findall("m:dependency", NS):
            g = dep.find("m:groupId", NS)
            a = dep.find("m:artifactId", NS)
            if g is not None and a is not None and g.text == group_id and a.text == artifact_id:
                return False
        dep_el = ET.SubElement(deps_el, _tag("dependency"))
        ET.SubElement(dep_el, _tag("groupId")).text = group_id
        ET.SubElement(dep_el, _tag("artifactId")).text = artifact_id
        ET.SubElement(dep_el, _tag("version")).text = version
        if dep_type:
            ET.SubElement(dep_el, _tag("type")).text = dep_type
        return True

    def set_property(self, name: str, value: str):
        props_el = self._get_or_create(self.root, "properties")
        prop_el = props_el.find(f"m:{name}", NS)
        if prop_el is None:
            prop_el = ET.SubElement(props_el, _tag(name))
        prop_el.text = value

    def get_artifact_id(self) -> str:
        el = self.root.find("m:artifactId", NS)
        return el.text.strip() if el is not None else ""

    def save(self):
        self._indent(self.root)
        self.tree.write(str(self.path), encoding="utf-8", xml_declaration=True)
        logger.info(f"Saved pom.xml: {self.path}")

    def _indent(self, elem: ET.Element, level: int = 0):
        """Add pretty-print indentation."""
        pad = "\n" + "    " * level
        if len(elem):
            if not elem.text or not elem.text.strip():
                elem.text = pad + "    "
            if not elem.tail or not elem.tail.strip():
                elem.tail = pad
            for child in elem:
                self._indent(child, level + 1)
            if not child.tail or not child.tail.strip():  # noqa
                child.tail = pad
        else:
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = pad