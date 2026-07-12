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
