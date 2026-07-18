"""
Java Framework Detection — Python translation of codegraph/src/resolution/frameworks/java.ts.

Detects Spring Boot by checking build files (pom.xml, build.gradle) and
source files for Spring annotations.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def detect_spring(root_dir: str, java_files: list[str]) -> bool:
    """
    Detect Spring Boot framework (java.ts:23-58).

    Checks pom.xml / build.gradle for spring-boot/springframework,
    then falls back to scanning .java files for Spring annotations.
    """
    # Check pom.xml
    pom = os.path.join(root_dir, "pom.xml")
    if os.path.isfile(pom):
        content = Path(pom).read_text(encoding="utf-8", errors="ignore")
        if "spring-boot" in content or "springframework" in content:
            return True

    # Check build.gradle
    gradle = os.path.join(root_dir, "build.gradle")
    if os.path.isfile(gradle):
        content = Path(gradle).read_text(encoding="utf-8", errors="ignore")
        if "spring-boot" in content or "springframework" in content:
            return True

    # Check build.gradle.kts
    gradle_kts = os.path.join(root_dir, "build.gradle.kts")
    if os.path.isfile(gradle_kts):
        content = Path(gradle_kts).read_text(encoding="utf-8", errors="ignore")
        if "spring-boot" in content or "springframework" in content:
            return True

    # Fallback: scan .java files for Spring annotations
    spring_annotations = (
        "@SpringBootApplication",
        "@RestController",
        "@Service",
        "@Repository",
        "@Component",
        "@Configuration",
        "@Autowired",
    )
    for file_path in java_files:
        full_path = os.path.join(root_dir, file_path) if not os.path.isabs(file_path) else file_path
        if not os.path.isfile(full_path):
            continue
        try:
            content = Path(full_path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if any(ann in content for ann in spring_annotations):
            return True

    return False


def detect_frameworks(root_dir: str, java_files: list[str]) -> list[str]:
    """
    Detect all applicable frameworks for a Java project.
    Returns a list of framework names (e.g. ['spring']).
    """
    frameworks: list[str] = []
    if detect_spring(root_dir, java_files):
        frameworks.append("spring")
    return frameworks


# Conventional directory patterns (codegraph-aligned java.ts:505-510)
_SERVICE_DIRS = ["/service/", "/services/"]
_REPO_DIRS = ["/repository/", "/repositories/"]
_CONTROLLER_DIRS = ["/controller/", "/controllers/"]
_ENTITY_DIRS = ["/entity/", "/entities/", "/model/", "/models/", "/domain/"]
_COMPONENT_DIRS = ["/component/", "/components/", "/config/"]

_CLASS_KINDS = {"class"}
_SERVICE_KINDS = {"class", "interface"}


def resolve_spring(ref, store: "GraphStore"):
    """
    Spring conventional name resolution (aligned with codegraph java.ts:129-192).

    Resolves unresolved references using Spring naming conventions:
    - *Service → class/interface in /service/ dirs
    - *Repository → class/interface in /repository/ dirs
    - *Controller → class in /controller/ dirs
    - *Component/*Config → class in /component/|/config/ dirs
    - PascalCase entities → class in /entity/|/model/|/domain/ dirs
    """
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from codewiki.db.store import GraphStore

    name = ref.reference_name

    # Determine suffix and expected dirs/kinds
    suffix_dirs_kinds: list[tuple[str, list[str], set[str]]] = [
        ("Service", _SERVICE_DIRS, _SERVICE_KINDS),
        ("Repository", _REPO_DIRS, _SERVICE_KINDS),
        ("Controller", _CONTROLLER_DIRS, _CLASS_KINDS),
        ("Component", _COMPONENT_DIRS, _CLASS_KINDS),
        ("Config", _COMPONENT_DIRS, _CLASS_KINDS),
    ]

    target_name = name
    for suffix, dirs, kinds in suffix_dirs_kinds:
        if name.endswith(suffix):
            rows = store.conn.execute(
                """SELECT n.id FROM nodes n
                   WHERE n.name = ? AND n.kind IN ({})
                   AND ({}) LIMIT 1""".format(
                    ",".join(f"'{k}'" for k in kinds),
                    " OR ".join(f"n.file_path LIKE '%{d}%'" for d in dirs),
                ),
                (target_name,),
            ).fetchall()
            if rows:
                return {
                    "target_node_id": rows[0]["id"],
                    "confidence": 0.85,
                    "resolved_by": "spring-convention",
                }

    # PascalCase conversion: receiver variables in DI are often camelCase
    # versions of the class name (e.g. "scriptExecutionService" → "ScriptExecutionService")
    if "." in name:
        receiver, method = name.rsplit(".", 1)
        pascal = receiver[0].upper() + receiver[1:] if receiver else ""
        if pascal and pascal != receiver and pascal != ref.reference_name:
            for dirs, kinds in [
                (_SERVICE_DIRS, _SERVICE_KINDS),
                (_REPO_DIRS, _SERVICE_KINDS),
                (_CONTROLLER_DIRS, _CLASS_KINDS),
                (_COMPONENT_DIRS, _CLASS_KINDS),
                (_ENTITY_DIRS, _CLASS_KINDS),
            ]:
                rows = store.conn.execute(
                    """SELECT n.id FROM nodes n
                       WHERE n.name = ? AND n.kind IN ({})
                       AND ({}) LIMIT 1""".format(
                        ",".join(f"'{k}'" for k in kinds),
                        " OR ".join(f"n.file_path LIKE '%{d}%'" for d in dirs),
                    ),
                    (pascal,),
                ).fetchall()
                if rows:
                    # Found the class — now find the method on it
                    method_rows = store.conn.execute(
                        """SELECT n2.id FROM nodes n2
                           JOIN edges e ON e.target = n2.id
                           WHERE n2.kind = 'method' AND n2.name = ?
                           AND e.kind = 'contains' AND e.source = ? LIMIT 1""",
                        (method, rows[0]["id"]),
                    ).fetchall()
                    if method_rows:
                        return {
                            "target_node_id": method_rows[0]["id"],
                            "confidence": 0.75,
                            "resolved_by": "spring-di-convention",
                        }

    return None
