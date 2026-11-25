"""Determine which Java version a Maven project actually targets.

Two independent signals are collected and reported separately:

1. Build configuration, read from the *effective POM* (`mvn help:effective-pom`), so
   values inherited from parent POMs (e.g. a corporate or Spring Boot parent) and
   property indirection are already resolved. If the effective POM could not be
   produced, the raw `pom.xml` files are parsed as a weaker fallback (properties are
   resolved only within the same file).
   Per module the effective level is, in Maven's precedence order:
   compiler-plugin <release> > compiler-plugin <source>/<target> >
   property maven.compiler.release > properties maven.compiler.source/target.

2. Bytecode: the class-file major version of every `.class` produced by *this*
   evaluation under target/classes (major 61 == Java 17). Files older than the
   evaluation start are ignored, so stale output from a previous build never counts.

`java17_verified` requires both: at least one module declares level 17, no module
declares a different level, and all freshly compiled main classes have major 61.
"""

from __future__ import annotations

import os
import re
import struct
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

_PROP_RE = re.compile(r"\$\{([^}]+)\}")


def normalize_level(value: str | None) -> int | None:
    """'1.8' -> 8, '8' -> 8, '17' -> 17; unresolved/garbage -> None."""
    if value is None:
        return None
    v = value.strip()
    if v.startswith("1.") and v[2:].isdigit():
        return int(v[2:])
    return int(v) if v.isdigit() else None


def class_major_to_java(major: int) -> int:
    return major - 44


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else None


@dataclass
class ModuleLevel:
    module: str
    level: int | None
    raw: dict[str, str | None]
    source: str                     # "compiler-plugin.release", "properties.source/target", ...
    toolchain_jdk: str | None = None


def _module_level(project: ET.Element, resolve) -> ModuleLevel:
    artifact = _text(project, "artifactId") or "?"
    props = project.find("properties")
    raw: dict[str, str | None] = {}
    for key in ("maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "java.version"):
        raw[f"property:{key}"] = resolve(_text(props, key)) if props is not None else None

    plugin_cfgs: list[ET.Element] = []
    toolchain_jdk = None
    for plugins in (project.findall("build/plugins/plugin")
                    + project.findall("build/pluginManagement/plugins/plugin")):
        aid = _text(plugins, "artifactId")
        if aid == "maven-compiler-plugin":
            for c in [plugins.find("configuration"), *plugins.findall("executions/execution/configuration")]:
                if c is not None:
                    plugin_cfgs.append(c)
        elif aid == "maven-toolchains-plugin":
            toolchain_jdk = toolchain_jdk or resolve(_text(plugins, "configuration/toolchains/jdk/version"))

    def first_plugin(tag: str) -> str | None:
        for c in plugin_cfgs:
            v = resolve(_text(c, tag))
            if v:
                return v
        return None

    raw["plugin:release"] = first_plugin("release")
    raw["plugin:source"] = first_plugin("source")
    raw["plugin:target"] = first_plugin("target")

    candidates = [
        ("compiler-plugin.release", raw["plugin:release"], None),
        ("compiler-plugin.source/target", raw["plugin:target"], raw["plugin:source"]),
        ("properties.release", raw["property:maven.compiler.release"], None),
        ("properties.source/target", raw["property:maven.compiler.target"], raw["property:maven.compiler.source"]),
    ]
    for source, primary, secondary in candidates:
        if primary or secondary:
            lv_t, lv_s = normalize_level(primary), normalize_level(secondary)
            if primary is None:
                level = lv_s
            elif lv_s is None or lv_s == lv_t:
                level = lv_t
            else:
                level = None  # source/target disagree (e.g. source 17, target 1.8): not "17"
            return ModuleLevel(artifact, level, raw, source, toolchain_jdk)
    return ModuleLevel(artifact, None, raw, "none-declared", toolchain_jdk)


def levels_from_effective_pom(path: Path) -> list[ModuleLevel]:
    root = _strip_ns(ET.parse(path).getroot())
    projects = [root] if root.tag == "project" else root.findall("project")
    return [_module_level(p, lambda v: v) for p in projects]


def levels_from_static_poms(repo: Path) -> list[ModuleLevel]:
    result = []
    for pom in sorted(repo.rglob("pom.xml")):
        if "target" in pom.relative_to(repo).parts:
            continue
        try:
            root = _strip_ns(ET.parse(pom).getroot())
        except ET.ParseError:
            continue
        props_el = root.find("properties")
        props = {c.tag: (c.text or "").strip() for c in (props_el if props_el is not None else [])
                 if isinstance(c.tag, str)}

        def resolve(v: str | None, _props=props) -> str | None:
            if v is None:
                return None
            for _ in range(5):
                nv = _PROP_RE.sub(lambda m: _props.get(m.group(1), m.group(0)), v)
                if nv == v:
                    break
                v = nv
            return None if _PROP_RE.search(v) else v

        ml = _module_level(root, resolve)
        ml.module = str(pom.relative_to(repo).parent) or "."
        result.append(ml)
    return result


def class_majors(repo: Path, since_epoch: float, subdir: str = "classes") -> Counter:
    """Count class-file major versions under **/target/<subdir> newer than `since_epoch`.

    Multi-release jar entries (META-INF/versions) are excluded.
    """
    counts: Counter = Counter()
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        p = Path(dirpath)
        rel = p.relative_to(repo).parts
        if "target" not in rel:
            continue
        i = rel.index("target")
        if len(rel) <= i + 1 or rel[i + 1] != subdir or "versions" in rel[i + 2:]:
            continue
        for f in filenames:
            if not f.endswith(".class"):
                continue
            fp = p / f
            try:
                if fp.stat().st_mtime < since_epoch:
                    counts["stale"] += 1
                    continue
                with fp.open("rb") as fh:
                    head = fh.read(8)
                if len(head) == 8 and head[:4] == b"\xca\xfe\xba\xbe":
                    counts[struct.unpack(">H", head[6:8])[0]] += 1
            except OSError:
                continue
    return counts


@dataclass
class JavaTargetReport:
    config_source: str                          # "effective-pom" | "static-pom" | "unavailable"
    modules: list[dict] = field(default_factory=list)
    declared_levels: list[int | None] = field(default_factory=list)
    config_targets_expected: bool = False
    main_class_majors: dict[str, int] = field(default_factory=dict)   # {"61": 120}
    test_class_majors: dict[str, int] = field(default_factory=dict)
    bytecode_targets_expected: bool = False
    toolchain_jdks: list[str] = field(default_factory=list)
    verified: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def analyse(repo: Path, effective_pom: Path | None, since_epoch: float, expected: int) -> JavaTargetReport:
    modules: list[ModuleLevel] = []
    source = "unavailable"
    notes: list[str] = []
    if effective_pom is not None and effective_pom.is_file():
        try:
            modules, source = levels_from_effective_pom(effective_pom), "effective-pom"
        except ET.ParseError as e:
            notes.append(f"effective POM unparsable: {e}")
    if not modules:
        modules = levels_from_static_poms(repo)
        source = "static-pom" if modules else "unavailable"
        if modules:
            notes.append("effective POM unavailable; static pom.xml parsing cannot see inherited parent settings")

    # Aggregator modules (packaging pom) usually compile nothing; they still inherit config.
    declared = [m.level for m in modules]
    known = [lv for lv in declared if lv is not None]
    config_ok = bool(known) and all(lv == expected for lv in known)
    if any(lv is None for lv in declared) and known:
        notes.append("some modules declare no resolvable Java level (compiler-plugin default applies)")

    main = class_majors(repo, since_epoch, "classes")
    test = class_majors(repo, since_epoch, "test-classes")
    stale = main.pop("stale", 0) + test.pop("stale", 0)
    if stale:
        notes.append(f"{stale} stale .class files (older than this evaluation) ignored")
    expected_major = expected + 44
    bytecode_ok = bool(main) and set(main) == {expected_major}
    if not main:
        notes.append("no freshly compiled main classes found (build failed or no sources)")

    return JavaTargetReport(
        config_source=source,
        modules=[asdict(m) for m in modules],
        declared_levels=declared,
        config_targets_expected=config_ok,
        main_class_majors={str(k): v for k, v in sorted(main.items())},
        test_class_majors={str(k): v for k, v in sorted(test.items())},
        bytecode_targets_expected=bytecode_ok,
        toolchain_jdks=sorted({m.toolchain_jdk for m in modules if m.toolchain_jdk}),
        verified=config_ok and bytecode_ok,
        notes=notes,
    )
