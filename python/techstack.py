"""Deterministic tech-stack detection and guideline-pack matching.

One scan of the cloned repository parses build files, dependency coordinates,
app-server configuration and source imports, then evaluates the `detect`
rules in each knowledge-base pack's YAML front matter against what it found.
No model guesses: versions come from the poms, applicability comes from the
packs' own rules, and every match carries its evidence.

File selection (which files a pack is applied to) is documented in
docs/file-selection.md; each stage of a run has a page under docs/stages/.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter

import yaml
from langchain_core.tools import tool

import settings
import workdir

SKIP_DIRS = {".git", "target", "build", "node_modules", ".idea", ".venv", "venv", "out", "dist"}
MAX_CONTENT_BYTES = 400_000

# (groupId regex, artifactId regex, framework name)
FRAMEWORK_RULES = [
    (r"^org\.springframework\.boot$", r".*", "Spring Boot"),
    (r"^org\.springframework\.security$", r"^spring-security-(core|web|config|bom)$", "Spring Security"),
    (r"^org\.springframework\.security\.oauth$", r"^spring-security-oauth2$", "Spring Security OAuth (legacy)"),
    (r"^org\.springframework$", r"^spring-(core|context|webmvc|web|beans|framework-bom)$", "Spring Framework"),
    (r"^org\.apache\.struts$", r"^struts2-core$", "Struts 2"),
    (r"^(struts|org\.apache\.struts)$", r"^struts(-core)?$", "Struts 1"),
    (r"^org\.hibernate(\.orm)?$", r"^hibernate-core$", "Hibernate"),
    (r"^(javax|jakarta)\.persistence$", r".*persistence-api$", "JPA API"),
    (r"^(org\.apache\.ibatis|com\.ibatis)$", r"^ibatis.*", "iBatis"),
    (r"^org\.mybatis$", r"^mybatis(-spring)?$", "MyBatis"),
    (r"^junit$", r"^junit$", "JUnit 4"),
    (r"^org\.junit(\.jupiter)?$", r"^(junit-jupiter.*|junit-bom)$", "JUnit 5"),
    (r"^org\.testng$", r"^testng$", "TestNG"),
    (r"^org\.mockito$", r"^mockito-(core|junit-jupiter)$", "Mockito"),
    (r"^(javax|jakarta)\.faces$", r".*", "JSF / Faces"),
    (r"^(org\.glassfish|com\.sun\.faces)$", r"^(jakarta\.faces|javax\.faces|jsf-api|jsf-impl)$", "JSF / Faces"),
    (r"^(javax|jakarta)\.ws\.rs$", r".*", "JAX-RS API"),
    (r"^(org\.glassfish\.jersey(\..*)?|com\.sun\.jersey)$", r".*", "JAX-RS (Jersey)"),
    (r"^org\.jboss\.resteasy$", r".*", "JAX-RS (RESTEasy)"),
    (r"^(javax|jakarta)\.jms$", r".*", "JMS API"),
    (r"^org\.apache\.activemq$", r".*", "JMS (ActiveMQ)"),
    (r"^(javax|jakarta)\.ejb$", r".*", "EJB API"),
    (r"^(javax|jakarta)\.servlet$", r"^(javax|jakarta)\.servlet-api$", "Servlet API"),
    (r"^(javax|jakarta)\.servlet\.jsp$", r".*jsp-api$", "JSP API"),
    (r"^(javax|jakarta)\.servlet\.jsp\.jstl$|^org\.glassfish\.web$|^jstl$|^javax\.servlet$", r".*jstl.*", "JSTL"),
    (r"^log4j$", r"^log4j$", "Log4j 1"),
    (r"^org\.apache\.logging\.log4j$", r"^log4j-(core|api)$", "Log4j 2"),
]

# (groupId regex, artifactId regex, label, optional version predicate)
LEGACY_RULES = [
    (r"^commons-lang$", r"^commons-lang$", "commons-lang 2 (superseded by commons-lang3)", None),
    (r"^commons-collections$", r"^commons-collections$", "commons-collections 3 (superseded by collections4)", None),
    (r"^net\.sf\.json-lib$", r"^json-lib$", "json-lib (unmaintained)", None),
    (r"^net\.sf\.ezmorph$", r"^ezmorph$", "ezmorph (unmaintained)", None),
    (r"^com\.h2database$", r"^h2$", "h2 1.x (current is 2.x)", lambda v: v.startswith("1.")),
    (r"^org\.springframework\.security\.oauth$", r"^spring-security-oauth2$", "spring-security-oauth2 (end of life)", None),
    (r"^log4j$", r"^log4j$", "log4j 1.x (end of life)", None),
    (r"^junit$", r"^junit$", "JUnit 4 (JUnit 5 is current)", None),
    (r"^javax\.(servlet|persistence|ws\.rs|faces|jms|ejb|annotation|inject|validation|transaction|mail|xml\.bind)$", r".*", "javax.* API artifact (Jakarta EE 9+ uses jakarta.*)", None),
]

JAVAX_SPECS = r"(servlet|persistence|ws\.rs|faces|jms|ejb|annotation|inject|validation|transaction|mail|xml\.bind|enterprise|json|websocket|security\.auth\.message)"
IMPORT_JAVAX = re.compile(r"^\s*import\s+javax\." + JAVAX_SPECS + r"\b", re.M)
IMPORT_JAKARTA = re.compile(r"^\s*import\s+jakarta\." + JAVAX_SPECS + r"\b", re.M)
IMPORT_ANY = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.M)


# --- repository scan ---------------------------------------------------------

class Repo:
    """Everything detection needs from one walk, with lazy, cached file reads."""

    def __init__(self, root):
        self.root = os.path.realpath(root)
        self.files = []        # posix relpaths
        self._content = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in filenames:
                self.files.append(os.path.relpath(os.path.join(dirpath, name), self.root).replace(os.sep, "/"))
        self.files.sort()

    def read(self, rel, limit=MAX_CONTENT_BYTES):
        """The first *limit* characters of *rel*.

        Cached per (file, limit) bucket: a short read (a 4 KB binary sniff) must
        never be served to a later full read - that silently limited content
        matching, inventory and acceptance to the first 4 KB of every file.
        """
        cached = self._content.get(rel)
        if cached is None or (len(cached[0]) >= cached[1] and cached[1] < limit):
            try:
                with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as handle:
                    text = handle.read(max(limit, MAX_CONTENT_BYTES) if limit <= MAX_CONTENT_BYTES else limit)
            except OSError:
                text = ""
            cached = (text, max(limit, MAX_CONTENT_BYTES) if limit <= MAX_CONTENT_BYTES else limit)
            self._content[rel] = cached
        return cached[0][:limit]

    def glob(self, pattern):
        rx = _glob_to_regex(pattern)
        return [f for f in self.files if rx.match(f)]


def _looks_binary(sample):
    return "\0" in sample


def _glob_to_regex(pattern):
    """'**/struts*.xml' -> regex over posix relpaths ('**/' = any depth incl. none)."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += r"(?:.*/)?"; i += 3
        elif pattern.startswith("**", i):
            out += r".*"; i += 2
        elif c == "*":
            out += r"[^/]*"; i += 1
        elif c == "?":
            out += r"[^/]"; i += 1
        else:
            out += re.escape(c); i += 1
    return re.compile("^" + out + "$")


def _tag(el):
    return el.tag.split("}")[-1]


def _child(el, name):
    if el is None:
        return None
    for c in el:
        if _tag(c) == name:
            return c
    return None


def _text(el, name):
    c = _child(el, name)
    return (c.text or "").strip() if c is not None and c.text else ""


def _parse_poms(repo):
    poms = []
    for rel in repo.glob("**/pom.xml"):
        try:
            el = ET.fromstring(repo.read(rel))
        except ET.ParseError:
            continue
        parent = _child(el, "parent")
        props = {_tag(p): p.text.strip() for p in (_child(el, "properties") or []) if p.text and p.text.strip()}
        deps = []
        direct = _child(el, "dependencies")
        managed = _child(_child(el, "dependencyManagement"), "dependencies")
        for section, is_managed in ((direct, False), (managed, True)):
            for d in (section or []):
                if _tag(d) == "dependency":
                    deps.append({"group": _text(d, "groupId"), "artifact": _text(d, "artifactId"),
                                 "version": _text(d, "version"), "managed": is_managed})
        compiler = {}
        for pl in (_child(_child(el, "build"), "plugins") or []):
            if _text(pl, "artifactId") == "maven-compiler-plugin":
                cfg = _child(pl, "configuration")
                for key in ("release", "source", "target"):
                    if _text(cfg, key):
                        compiler[key] = _text(cfg, key)
        poms.append({
            "path": rel,
            "dir": os.path.dirname(rel) or ".",
            "artifact": _text(el, "artifactId"),
            "group": _text(el, "groupId") or _text(parent, "groupId"),
            "packaging": _text(el, "packaging") or "jar",
            "parent": _text(parent, "artifactId"),
            "modules": [m.text.strip() for m in (_child(el, "modules") or []) if m.text],
            "properties": props,
            "dependencies": deps,
            "compiler": compiler,
        })
    poms.sort(key=lambda p: (p["dir"].count("/"), p["dir"]))
    return poms


def _resolve(value, props):
    for _ in range(5):
        new = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def _version_key(v):
    """Comparable key: numeric parts, then a flag so '7.0.0-M1' < '7.0.0'."""
    nums = [int(x) for x in re.findall(r"\d+", v.split("-")[0])]
    qualifier = 0 if re.search(r"-(?!RELEASE|Final|GA)\w", v, re.I) and not v.split("-")[0] == v else 1
    return (nums, qualifier)


def _version_lt(v, limit):
    try:
        return _version_key(v) < _version_key(limit)
    except Exception:
        return False


# --- facts ---------------------------------------------------------------------

def _java_versions(poms, props, repo):
    declared = {}
    for key in ("maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "java.version"):
        if key in props:
            declared[key] = _resolve(props[key], props)
    for pom in poms:
        for key, val in pom["compiler"].items():
            declared.setdefault(f"compiler-plugin.{key} ({pom['artifact']})", _resolve(val, props))
    hints = {}
    for name in (".java-version", ".sdkmanrc"):
        if name in repo.files:
            hints[name] = repo.read(name, 200).strip()[:60]
    return declared, hints


def _dependency_index(poms, props):
    """'group:artifact' -> {versions, modules} across every pom (direct + managed)."""
    index = {}
    for pom in poms:
        for dep in pom["dependencies"]:
            if not dep["group"] or not dep["artifact"]:
                continue
            coord = f"{dep['group']}:{dep['artifact']}"
            entry = index.setdefault(coord, {"versions": set(), "modules": set()})
            if dep["version"]:
                entry["versions"].add(_resolve(dep["version"], props))
            entry["modules"].add(pom["artifact"] or pom["dir"])
    return index


def _frameworks(dep_index):
    found, legacy = {}, {}
    for coord, info in dep_index.items():
        g, a = coord.split(":", 1)
        for g_re, a_re, name in FRAMEWORK_RULES:
            if re.match(g_re, g) and re.match(a_re, a):
                e = found.setdefault(name, {"name": name, "artifacts": set(), "versions": set(), "modules": set()})
                e["artifacts"].add(coord); e["versions"] |= info["versions"]; e["modules"] |= info["modules"]
        for g_re, a_re, label, pred in LEGACY_RULES:
            vs = info["versions"]
            if re.match(g_re, g) and re.match(a_re, a) and (pred is None or any(pred(v) for v in vs)):
                e = legacy.setdefault(label, {"label": label, "artifacts": set(), "versions": set(), "modules": set()})
                e["artifacts"].add(coord); e["versions"] |= vs; e["modules"] |= info["modules"]

    def finish(d):
        out = []
        for e in d.values():
            e = dict(e)
            for k in ("artifacts", "versions", "modules"):
                e[k] = sorted(e[k])
            out.append(e)
        return sorted(out, key=lambda e: e.get("name", e.get("label", "")))
    return finish(found), finish(legacy)


def _imports(repo, poms):
    """javax/jakarta counts per module plus the set of every imported package path."""
    module_dirs = sorted((p["dir"] for p in poms if p["dir"] != "."), key=len, reverse=True)

    def module_of(rel):
        for d in module_dirs:
            if rel.startswith(d + "/"):
                return d
        return "."

    per_module, total, all_imports = {}, Counter(), set()
    for rel in repo.files:
        if not rel.endswith(".java"):
            continue
        src = repo.read(rel, 200_000)
        all_imports.update(IMPORT_ANY.findall(src))
        jx, jk = len(IMPORT_JAVAX.findall(src)), len(IMPORT_JAKARTA.findall(src))
        if jx or jk:
            c = per_module.setdefault(module_of(rel), Counter())
            c["javax"] += jx; c["jakarta"] += jk
            total["javax"] += jx; total["jakarta"] += jk

    def verdict(c):
        if c["javax"] and c["jakarta"]:
            return "mixed"
        return "javax" if c["javax"] else "jakarta" if c["jakarta"] else "none"

    namespace = {
        "overall": verdict(total),
        "javax_imports": total["javax"],
        "jakarta_imports": total["jakarta"],
        "modules": {m: {"javax": c["javax"], "jakarta": c["jakarta"], "verdict": verdict(c)} for m, c in sorted(per_module.items())},
    }
    return namespace, all_imports


def _app_server(repo):
    info = {"liberty_server_xml": [], "liberty_features": [], "tomcat_context_xml": [], "web_xml": [], "docker_images": [], "scripts": []}
    for rel in repo.files:
        name = os.path.basename(rel)
        if name == "server.xml" and "liberty" in rel:
            info["liberty_server_xml"].append(rel)
            info["liberty_features"] += re.findall(r"<feature>\s*([^<\s]+)\s*</feature>", repo.read(rel))
        elif name == "context.xml" and rel.endswith("META-INF/context.xml"):
            info["tomcat_context_xml"].append(rel)
        elif name == "web.xml":
            m = re.search(r'<web-app[^>]*\sversion="([^"]+)"', repo.read(rel))
            info["web_xml"].append({"path": rel, "version": m.group(1) if m else "unknown"})
        elif name.startswith("Dockerfile"):
            info["docker_images"] += [f"{rel}: {img}" for img in re.findall(r"^\s*FROM\s+(\S+)", repo.read(rel), re.M)]
        elif name in ("docker-compose.yml", "docker-compose.yaml"):
            info["docker_images"] += [f"{rel}: {img}" for img in re.findall(r"^\s*image:\s*(\S+)", repo.read(rel), re.M)]
        elif "/" not in rel and name.endswith(".sh") and re.search(r"tomcat|liberty|wlp|catalina", name + repo.read(rel, 5000), re.I):
            info["scripts"].append(name)
    info["liberty_features"] = sorted(set(info["liberty_features"]))
    candidates = []
    if info["liberty_server_xml"]:
        candidates.append("WebSphere Liberty (server.xml present)")
    if info["tomcat_context_xml"] or any("tomcat" in s for s in info["scripts"]) or any("tomcat" in d for d in info["docker_images"]):
        candidates.append("Tomcat (context.xml / scripts / Docker image)")
    if any(re.search(r"liberty|wlp", d, re.I) for d in info["docker_images"]) and not info["liberty_server_xml"]:
        candidates.append("WebSphere Liberty (Docker image)")
    info["candidates"] = candidates
    # More than one candidate usually means a migration is half done.
    info["server"] = candidates[0] if len(candidates) == 1 else ("mixed: " + "; ".join(candidates) if candidates else "unknown")
    return info


def _docs(repo):
    docs = {}
    for name in ("TECH_STACK.md", "README.md", "readme.md"):
        if name in repo.files:
            docs[name] = [l.strip() for l in repo.read(name, 20_000).splitlines() if l.strip()][:12]
    return docs


def _gradle_properties(repo):
    props = {}
    for rel in repo.glob("**/gradle.properties") + repo.glob("**/build.gradle") + repo.glob("**/build.gradle.kts"):
        for m in re.finditer(r"^\s*(sourceCompatibility|targetCompatibility|java\.version)\s*[=:]?\s*['\"]?(?:JavaVersion\.VERSION_)?([\w.]+)", repo.read(rel), re.M):
            props.setdefault(m.group(1), m.group(2).replace("_", "."))
    return props


# --- guideline packs ------------------------------------------------------------

def load_packs(directory=None):
    """Parse the YAML front matter of every pack in the knowledge-base directory."""
    directory = directory or settings.resolve_path(settings.KNOWLEDGE_BASE_DIRECTORY)
    packs = []
    if not os.path.isdir(directory):
        return packs
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(meta, dict) or "id" not in meta:
            continue
        meta["file"] = name
        meta.setdefault("depends_on", [])
        meta.setdefault("decisions", [])
        packs.append(meta)
    return packs


def _secret_findings(repo, severity):
    """{relpath: [Finding]} of the given severity, one scan per Repo (cached).

    Uses forge's own scanner (guardrails.scan_text), so packs that talk about
    secrets share its patterns, placeholder handling and allowlist.
    """
    import guardrails
    cache = repo.__dict__.setdefault("_secret_cache", {})
    if severity not in cache:
        found = {}
        for rel in repo.files:
            sample = repo.read(rel, 4096)
            if not sample or _looks_binary(sample):
                continue
            hits = guardrails.scan_text(repo.read(rel), rel, severities=(severity,))
            if hits:
                found[rel] = hits
        cache[severity] = found
    return cache[severity]


def _eval_rule(rule, facts, decisions):
    """One `detect` rule -> (matched, evidence, gate). gate is set for decision rules."""
    if not isinstance(rule, dict) or len(rule) != 1:
        return False, "", None
    kind, arg = next(iter(rule.items()))
    repo, deps, props, imports = facts["repo"], facts["deps"], facts["props"], facts["imports"]
    # Rule arguments are either a scalar or a small mapping; a pack author's
    # typo must degrade to "did not match", never break detection.
    arg_map = arg if isinstance(arg, dict) else {}
    arg_str = str(arg) if not isinstance(arg, dict) else ""
    if kind == "dependency":
        if arg in deps:
            return True, f"dependency {arg} {'/'.join(sorted(deps[arg]['versions'])) or ''}".strip(), None
    elif kind == "dependency_lt":
        coord, limit = arg_map.get("coord", ""), str(arg_map.get("value", ""))
        for v in deps.get(coord, {}).get("versions", []):
            if _version_lt(v, limit):
                return True, f"dependency {coord} {v} < {limit}", None
    elif kind == "import_prefix":
        hits = [i for i in imports if i.startswith(arg)]
        if hits:
            return True, f"imports {arg}.* ({len(hits)} distinct)", None
    elif kind == "file_glob":
        hits = repo.glob(arg)
        if hits:
            return True, f"files match {arg} ({len(hits)}: {', '.join(hits[:3])}{'…' if len(hits) > 3 else ''})", None
    elif kind == "content_match":
        # Object form {glob, pattern}; string form = pattern over every text file.
        glob, pat = (arg_map.get("glob", "**/*"), arg_map.get("pattern", "")) if arg_map else ("**/*", arg_str)
        try:
            pattern = re.compile(pat, re.M)
        except re.error:
            return False, "", None
        hits = [f for f in repo.glob(glob) if not _looks_binary(repo.read(f, 4096)) and pattern.search(repo.read(f))]
        if hits:
            return True, f"content matches {pat!r} in {len(hits)} file(s) ({', '.join(hits[:3])})", None
    elif kind == "property_lt":
        name, limit = arg_map.get("name", ""), str(arg_map.get("value", ""))
        if name in props and _version_lt(_resolve(props[name], props), limit):
            return True, f"property {name}={_resolve(props[name], props)} < {limit}", None
    elif kind == "gradle_property_lt":
        name, limit = arg_map.get("name", ""), str(arg_map.get("value", ""))
        gp = facts["gradle_props"]
        if name in gp and _version_lt(gp[name], limit):
            return True, f"gradle {name}={gp[name]} < {limit}", None
    elif kind == "xml_element":
        uri, _, local = str(arg).rpartition(":")
        for f in repo.glob("**/*.xml"):
            content = repo.read(f)
            if uri in content and re.search(r"<(?:\w+:)?" + re.escape(local) + r"[\s>/]", content):
                return True, f"XML element {{{uri}}}{local} in {f}", None
    elif kind == "secret_scan":
        found = _secret_findings(repo, arg_str or "secret")
        if found:
            total = sum(len(v) for v in found.values())
            label = "public key(s)" if arg_str == "public" else "hardcoded secret(s)"
            return True, f"{total} {label} in {len(found)} file(s)", None
    elif kind == "decision_equals":
        key, value = arg_map.get("key", ""), str(arg_map.get("value", ""))
        if decisions.get(key) == value:
            return True, f"decision {key}={value}", None
        return False, "", f"{key}={value}"
    return False, "", None


def match_packs(facts, packs, decisions=None):
    decisions = decisions or {}
    results = []
    for pack in packs:
        detect = pack.get("detect") or {}
        rules = detect.get("any") or []
        all_rules = detect.get("all") or []
        evidence, gates = [], []
        for rule in rules:
            ok, why, gate = _eval_rule(rule, facts, decisions)
            if ok:
                evidence.append(why)
            if gate:
                gates.append(gate)
        matched = bool(evidence)
        if all_rules:
            all_ok = []
            for rule in all_rules:
                ok, why, gate = _eval_rule(rule, facts, decisions)
                all_ok.append(ok)
                if ok:
                    evidence.append(why)
                if gate:
                    gates.append(gate)
            matched = all(all_ok) and (matched or not rules)
        results.append({
            "id": pack["id"], "title": pack.get("title", ""), "tier": pack.get("tier", ""),
            "status": pack.get("status", "ready"), "matched": matched,
            "evidence": evidence, "gated_on": gates,
            "depends_on": pack.get("depends_on", []), "decisions": pack.get("decisions", []),
        })
    return results


def _ordered(matched):
    """Topological order of matched packs by depends_on (deps first); unmatched deps ignored."""
    by_id = {m["id"]: m for m in matched}
    order, seen = [], set()

    def visit(pid, stack=()):
        if pid in seen or pid not in by_id or pid in stack:
            return
        for dep in by_id[pid]["depends_on"]:
            visit(dep, stack + (pid,))
        seen.add(pid)
        order.append(by_id[pid])
    for m in matched:
        visit(m["id"])
    return order


# --- file inventory and acceptance ----------------------------------------------
# Which files each approved pack applies to (applies_to), which of them still
# need work (acceptance patterns hit), and whether a pack is finished. See
# docs/file-selection.md.

_TEST_PATH = re.compile(r"(^|/)src/test/")


def _anchor_glob(pattern):
    """Pack scopes are written repo-relative ("src/**/*.java") but must also match
    inside modules ("ams-internal/X/src/..."): anchor them at any depth."""
    pattern = str(pattern or "**/*").strip()
    return pattern if pattern.startswith("**/") or pattern.startswith("/") else "**/" + pattern


def _keep_tests(pack, glob):
    return bool(pack.get("include_tests")) or "test" in str(glob).lower()


def _filter_tests(paths, pack, glob):
    return paths if _keep_tests(pack, glob) else [p for p in paths if not _TEST_PATH.search(p)]


def _text_files(repo, glob):
    return [f for f in repo.glob(_anchor_glob(glob)) if not _looks_binary(repo.read(f, 4096))]


def select_files(repo, pack):
    """Files a pack's applies_to selects, plus the selector names code cannot evaluate."""
    selected, unevaluated = set(), []
    for rule in pack.get("applies_to") or []:
        if not isinstance(rule, dict) or len(rule) != 1:
            continue
        kind, arg = next(iter(rule.items()))
        if kind == "file_glob":
            selected |= set(_filter_tests(repo.glob(_anchor_glob(arg)), pack, arg))
        elif kind == "secret_scan":
            selected |= set(_filter_tests(list(_secret_findings(repo, str(arg))), pack, ""))
        elif kind == "content_match":
            glob, pat = (arg.get("glob", "**/*"), arg.get("pattern", "")) if isinstance(arg, dict) else ("**/*", str(arg))
            try:
                rx = re.compile(pat, re.M)
            except re.error:
                unevaluated.append(f"content_match with an invalid pattern in {pack['id']}")
                continue
            selected |= {f for f in _filter_tests(_text_files(repo, glob), pack, glob) if rx.search(repo.read(f))}
        else:
            unevaluated.append(f"{kind}: {arg}")
    return sorted(selected), unevaluated


def _acceptance_rules(pack, decisions=None):
    """Split acceptance into evaluable rules and the ones listed for a human."""
    decisions = decisions or {}
    rules, manual = [], []
    for raw in pack.get("acceptance") or []:
        if not isinstance(raw, dict):
            continue
        when = raw.get("when") or {}
        unmet = {k: v for k, v in when.items() if str(decisions.get(k)) != str(v)} if isinstance(when, dict) else {}
        kind = next((k for k in ("no_match", "count_unchanged") if k in raw), None)
        if kind and unmet:
            manual.append(f"{kind} {raw[kind]!r} applies only when " + ", ".join(f"{k}={v}" for k, v in unmet.items()) + " (not evaluated)")
            continue
        if kind:
            spec = raw[kind]
            pattern = spec.get("pattern", "") if isinstance(spec, dict) else str(spec)
            scope = (spec.get("scope") if isinstance(spec, dict) else None) or raw.get("scope") or "**/*"
            try:
                rules.append({"kind": kind, "pattern": pattern, "rx": re.compile(pattern, re.M), "scope": scope})
            except re.error:
                manual.append(f"{kind} with an invalid pattern {pattern!r} (not evaluated)")
        elif "test_parity" in raw:
            rules.append({"kind": "test_parity", "pattern": "test parity", "scope": "**/src/test/**"})
        elif "no_secrets" in raw:
            rules.append({"kind": "no_secrets", "severity": str(raw["no_secrets"]), "pattern": "secret scanner", "scope": "**/*"})
        elif "build" in raw:
            manual.append(f"build `{raw['build']}`: covered by the Maven install/test stage")
        else:
            for key, value in raw.items():
                if key != "when":
                    manual.append(f"{key}{'' if value is True else ' ' + str(value)}: for the human reviewer")
    return rules, manual


def _scoped(repo, pack, rule):
    return _filter_tests(_text_files(repo, rule["scope"]), pack, rule["scope"])


def _hit_lines(text, rx, limit=3):
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        if rx.search(line):
            hits.append((number, line.strip()))
            if len(hits) >= limit:
                break
    return hits


def build_inventory(repo, packs):
    """{file: [{"pack", "must_change"}]} for the given packs (already in depends_on order).

    must_change is True when one of the pack's no_match patterns hits the file
    (it still contains what the migration removes). A file no pack must change
    is verify-only: selected, but nothing known to be wrong in it.
    """
    inventory, notes = {}, []
    for pack in packs:
        files, unevaluated = select_files(repo, pack)
        notes += [f"{pack['id']}: applies_to {u} is not evaluated by code" for u in unevaluated]
        rules, _ = _acceptance_rules(pack)
        no_match = [(r["rx"], set(repo.glob(_anchor_glob(r["scope"])))) for r in rules if r["kind"] == "no_match"]
        secret_files = set()
        for r in rules:
            if r["kind"] == "no_secrets":
                secret_files |= set(_secret_findings(repo, r["severity"]))
        for f in files:
            must = f in secret_files or any(f in scope and rx.search(repo.read(f)) for rx, scope in no_match)
            inventory.setdefault(f, []).append({"pack": pack["id"], "must_change": bool(must), "detect_only": pack.get("status") == "detect-only"})
    return inventory, notes


def _source_texts(root, paths):
    """Contents of *paths* at origin/<source branch> (the pre-migration state)."""
    import subprocess
    ref = f"origin/{settings.GIT_SOURCE_BRANCH}" if settings.GIT_SOURCE_BRANCH else "origin/HEAD"
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if listed.returncode != 0:
        return None, ref
    present = set(listed.stdout.split("\n"))
    wanted = [p for p in paths if p in present]
    if not wanted:
        return {}, ref
    batch = subprocess.run(["git", "cat-file", "--batch"], cwd=root, input="".join(f"{ref}:{p}\n" for p in wanted).encode(), capture_output=True)
    out, texts, pos = batch.stdout, {}, 0
    for path in wanted:
        header_end = out.index(b"\n", pos)
        parts = out[pos:header_end].split()
        if len(parts) < 3 or parts[1] == b"missing":
            pos = header_end + 1
            continue
        size = int(parts[2])
        texts[path] = out[header_end + 1: header_end + 1 + size].decode("utf-8", "replace")
        pos = header_end + 1 + size + 1
    return texts, ref


def _filtered_secret_findings(repo, pack, severity):
    found = _secret_findings(repo, severity)
    keep = set(_filter_tests(list(found), pack, ""))
    return {k: v for k, v in found.items() if k in keep}


def check_acceptance(repo, pack, decisions=None, max_leftovers=30):
    """Evaluate a pack's acceptance: leftovers of no_match rules, count_unchanged
    against the source branch, and the checks listed for a human."""
    from guardrails import redact
    rules, manual = _acceptance_rules(pack, decisions)
    leftovers, total, counts = [], 0, []
    for rule in rules:
        if rule["kind"] == "test_parity":
            import ui_events
            approved = ui_events.approved_files()
            for finding in test_parity(repo.root):
                if finding["path"] in approved:
                    continue
                total += 1
                if len(leftovers) < max_leftovers:
                    leftovers.append(f"{finding['path']}: test parity - {finding['summary']}")
            continue
        if rule["kind"] == "no_secrets":
            for rel, hits in sorted(_filtered_secret_findings(repo, pack, rule["severity"]).items()):
                for h in hits:
                    total += 1
                    if len(leftovers) < max_leftovers:
                        leftovers.append(f"{rel}:{h.line}: {h.kind} (value redacted)")
            continue
        files = _scoped(repo, pack, rule)
        if rule["kind"] == "no_match":
            for f in files:
                for number, line in _hit_lines(repo.read(f), rule["rx"]):
                    total += 1
                    if len(leftovers) < max_leftovers:
                        leftovers.append(f"{f}:{number}: {redact(line)[:140]}")
        else:  # count_unchanged
            now = sum(len(rule["rx"].findall(repo.read(f))) for f in files)
            before_texts, ref = _source_texts(repo.root, files)
            if before_texts is None:
                counts.append(f"count_unchanged {rule['pattern'][:60]!r}: {now} now; {ref} unavailable, not compared")
                continue
            before = sum(len(rule["rx"].findall(t)) for t in before_texts.values())
            state = "unchanged" if before == now else "CHANGED"
            counts.append(f"count_unchanged {rule['pattern'][:60]!r}: {before} on {ref}, {now} now ({state})")
    changed = any("CHANGED" in c for c in counts)
    return {"clean": total == 0 and not changed, "leftover_count": total, "leftovers": leftovers,
            "count_changes": counts, "manual_checks": manual}


def pack_guidance(pack, limit=6000):
    """The pack's '## transform' section (what the migration must do), for the reviewer."""
    path = os.path.join(settings.resolve_path(settings.KNOWLEDGE_BASE_DIRECTORY), pack.get("file", ""))
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    m = re.search(r"^## transform\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    body = (m.group(1) if m else re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)).strip()
    return body if len(body) <= limit else body[:limit] + "\n... [guidance truncated]"


def packs_for_files(repo, packs):
    """{file: [pack ids]} - which of the given packs select each file (applies_to)."""
    owners = {}
    for pack in packs:
        for f in select_files(repo, pack)[0]:
            owners.setdefault(f, []).append(pack["id"])
    return owners


# --- test parity ---------------------------------------------------------------
# A migration may change HOW a test is written (JUnit 4 -> 5 syntax, Mockito API),
# never WHAT it checks. Compared with the source branch, per changed test file:
# test methods must not disappear, assertions must not decrease, and the
# literals a test expects must not change.

_TEST_METHOD = re.compile(
    r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory)\b[^\n]*\n(?:\s*@[^\n]*\n)*"
    r"\s*(?:public\s+|protected\s+|private\s+)?(?:static\s+)?(?:final\s+)?\w[\w<>\[\], ]*?\s+(\w+)\s*\(")
_ASSERTION = re.compile(r"(?<![\w.])(?:Assertions\.|Assert\.|Mockito\.)?(assert\w*|verify\w*|fail)\s*\(")
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


_CODE_TOKEN = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|/\*.*?\*/|//[^\n]*', re.S)


def _strip_comments(text):
    """Remove // and /* */ comments but leave string literals intact ("/*" in a string is not a comment)."""
    return _CODE_TOKEN.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else "", text)


def _statement_end(text, start):
    """Index just past the ';' that ends the statement starting at *start* (strings skipped)."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth <= 0:
            return i + 1
        i += 1
    return n


def _assertion_literals(text):
    """String literals inside whole assertion statements (expected values and messages).

    Whole statements, not lines: JUnit 5 moves the message to the last argument,
    often onto a continuation line, and that must not read as a changed expectation.
    """
    found = set()
    for m in _ASSERTION.finditer(text):
        statement = text[m.start():_statement_end(text, m.start())]
        found.update(" ".join(s.split()) for s in _STRING.findall(statement) if s.strip())
    return found


def _test_profile(text):
    body = "\n".join(l for l in _strip_comments(text).splitlines() if not l.lstrip().startswith("import "))
    calls = [m.group(1) for m in _ASSERTION.finditer(body)]
    return {"tests": _TEST_METHOD.findall(body), "assertions": len(calls), "literals": _assertion_literals(body)}


def test_parity(root):
    """Findings for test files whose meaning changed versus origin/<source branch>.

    Each finding: {"path", "summary", "removed_tests", "added_tests",
    "assertions_before", "assertions_after", "removed_literals"}.
    """
    import subprocess
    ref = f"origin/{settings.GIT_SOURCE_BRANCH}" if settings.GIT_SOURCE_BRANCH else "origin/HEAD"
    diff = subprocess.run(["git", "diff", "--name-only", "--diff-filter=MD", ref, "--"], cwd=root,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if diff.returncode != 0:
        return []
    changed = [p for p in diff.stdout.split() if _TEST_PATH.search(p) and p.endswith(".java")]
    before_texts, _ = _source_texts(root, changed)
    findings = []
    for rel in changed:
        before = (before_texts or {}).get(rel)
        if before is None:
            continue
        path = os.path.join(root, rel)
        after = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""
        b, a = _test_profile(before), _test_profile(after)
        removed = [t for t in b["tests"] if t not in a["tests"]]
        added = [t for t in a["tests"] if t not in b["tests"]]
        lost_literals = sorted(b["literals"] - a["literals"])
        parts = []
        if not after:
            parts.append("test file deleted")
        if removed:
            parts.append(f"removed or renamed tests: {', '.join(removed[:8])}" + (" ..." if len(removed) > 8 else ""))
        if a["assertions"] < b["assertions"]:
            parts.append(f"assertions {b['assertions']} -> {a['assertions']}")
        if lost_literals:
            shown = [l if len(l) <= 40 else l[:37] + "..." for l in lost_literals[:5]]
            parts.append("changed expectations: " + ", ".join(repr(l) for l in shown) + (" ..." if len(lost_literals) > 5 else ""))
        if parts:
            if added:
                parts.append(f"new tests: {', '.join(added[:5])}")
            findings.append({"path": rel, "summary": "; ".join(parts), "removed_tests": removed, "added_tests": added,
                             "assertions_before": b["assertions"], "assertions_after": a["assertions"],
                             "removed_literals": lost_literals})
    return findings


def resolve_packs(pack_ids):
    """Pack dicts for *pack_ids* in depends_on order; unknown ids are returned separately."""
    by_id = {p["id"]: p for p in load_packs()}
    wanted = [i for i in dict.fromkeys(pack_ids) if i]
    known = [by_id[i] for i in wanted if i in by_id]
    unknown = [i for i in wanted if i not in by_id]
    ordered = _ordered([dict(p, depends_on=p.get("depends_on") or []) for p in known])
    return [by_id[p["id"]] for p in ordered], unknown


# --- entry points -----------------------------------------------------------------

def detect(repo_path, decisions=None):
    repo = Repo(repo_path)
    poms = _parse_poms(repo)
    props = {}
    for pom in poms:
        props.update(pom["properties"])
    gradle = repo.glob("**/build.gradle") + repo.glob("**/build.gradle.kts")
    ant = repo.glob("**/build.xml")
    reactors = [p for p in poms if p["modules"]]
    parents = [p for p in poms if not p["modules"] and p["packaging"] == "pom"]
    declared, hints = _java_versions(poms, props, repo)
    dep_index = _dependency_index(poms, props)
    frameworks, legacy = _frameworks(dep_index)
    namespace, imports = _imports(repo, poms)
    facts = {"repo": repo, "deps": dep_index, "props": props, "imports": imports, "gradle_props": _gradle_properties(repo)}
    pack_results = match_packs(facts, load_packs(), decisions)
    applicable = _ordered([r for r in pack_results if r["matched"]])
    return {
        "build": {
            "tool": "maven" if poms else "gradle" if gradle else "ant" if ant else "unknown",
            "reactors": [{"dir": p["dir"], "artifact": p["artifact"], "modules": p["modules"]} for p in reactors],
            # Parent/BOM poms (packaging pom, no modules): install these first.
            "parents": [{"dir": p["dir"], "artifact": p["artifact"]} for p in parents],
            "modules": [{"dir": p["dir"], "artifact": p["artifact"], "packaging": p["packaging"], "parent": p["parent"]} for p in poms if not p["modules"] and p["packaging"] != "pom"],
            "gradle_files": gradle,
            "ant_files": ant,
        },
        "java": {"declared": declared, "toolchain_hints": hints},
        "frameworks": frameworks,
        "legacy_libraries": legacy,
        "namespace": namespace,
        "app_server": _app_server(repo),
        "docs": _docs(repo),
        "packs": {
            # In dependency order: migrate earlier entries first.
            "applicable": [{k: r[k] for k in ("id", "title", "tier", "status", "evidence", "gated_on", "depends_on", "decisions")} for r in applicable],
            "not_applicable": [r["id"] for r in pack_results if not r["matched"]],
            "gated": [{"id": r["id"], "gated_on": r["gated_on"]} for r in pack_results if r["gated_on"] and not r["matched"]],
        },
    }


def summarize(stack):
    """One paragraph a human can read, derived from detect()'s output."""
    b = stack["build"]
    java = ", ".join(f"{k}={v}" for k, v in stack["java"]["declared"].items()) or "not declared"
    fw = ", ".join(f"{f['name']} {'/'.join(f['versions']) or '?'}" for f in stack["frameworks"]) or "none detected"
    leg = ", ".join(l["label"] for l in stack["legacy_libraries"]) or "none"
    ns = stack["namespace"].get("overall", "none")
    srv = stack["app_server"].get("server", "unknown")
    feats = " ".join(stack["app_server"].get("liberty_features", []))
    packs = ", ".join(f"{p['id']}{' (detect-only)' if p['status'] == 'detect-only' else ''}" for p in stack["packs"]["applicable"]) or "none"
    return (f"Build: {b['tool']} ({len(b['reactors'])} reactor(s), {len(b.get('parents', []))} parent/BOM pom(s), {len(b['modules'])} module(s)). "
            f"Java: {java}. Frameworks: {fw}. Namespace: {ns} (javax imports {stack['namespace'].get('javax_imports', 0)}, "
            f"jakarta imports {stack['namespace'].get('jakarta_imports', 0)}). App server: {srv}{' [' + feats + ']' if feats else ''}. "
            f"Legacy libraries: {leg}. Applicable packs in order: {packs}.")


@tool
def detect_tech_stack(repo_dir: str) -> str:
    """Detect the repository's tech stack and which guideline packs apply, as JSON.

    Deterministic (no guessing): build tool, reactors, parent/BOM poms and
    modules; declared Java versions; every framework with versions and the
    modules using it; legacy libraries; javax vs jakarta import counts per
    module; the app server (Liberty features, Tomcat config, web.xml, Docker
    images); TECH_STACK/README hints. `packs.applicable` lists the guideline
    packs whose own detect rules matched, with the evidence, in dependency
    order; `status: detect-only` packs have no transform guidance yet.
    Trust these versions over what documents say.

    Args:
        repo_dir: The cloned repo, relative to the working directory (e.g. "repo").
    """
    try:
        root = workdir.resolve(repo_dir)
    except ValueError as e:
        return f"ERROR: {e}"
    if not os.path.isdir(root):
        return f"ERROR: {repo_dir} is not a directory."
    stack = detect(root)
    stack["summary"] = summarize(stack)
    return json.dumps(stack, indent=1)
