#!/usr/bin/env python3
"""
e3_2_parsers.py — deterministic, network-free, DB-free manifest parsers for the
Foundry E3.2 surface-extraction step.

Each parser turns ALREADY-FETCHED manifest text (package.json / pyproject.toml /
setup.py / setup.cfg / go.mod / Cargo.toml) into a structural object carrying:

  - provides_skeleton : the public surface the repo exports
                        (exports map / bin / main / module / [project.scripts] /
                         entry-points / crate lib + bins / module path / __all__)
  - requires          : runtime + peer deps and version floors
                        (dependencies / peerDependencies / engines.node /
                         requires-python / go require block / [dependencies])
  - recipe            : {"install": "<the canonical install command>"}

Pure stdlib only: json, re, and tomllib (native on py>=3.11; this box is 3.14).
A regex fallback is kept for pyproject in case tomllib is unavailable, but it is
effectively dead code on the run box.

The functions are PURE: no network, no DB, no eval/exec of setup.py, no imports
of the parsed project. On unparseable input they return a stable object (never
raising), and the dispatcher returns NO_PARSEABLE_SURFACE.

The canonical output object returned by parse_manifest():
{
  "surface_kind": "js"|"py"|"go"|"rust"|"none",
  "manifest_path": str,
  "pkg_name": str,
  "provides_skeleton": {... language-specific keys ...},
  "requires":          {... language-specific keys ...},
  "recipe":            {"install": str},
  "flags":             {"monorepo": bool, "fallback": str, "parse_errors": [str]},
}
"""

from __future__ import annotations

import json
import re

try:
    import tomllib  # py>=3.11
    _HAVE_TOMLLIB = True
except Exception:  # pragma: no cover - dead on py3.14
    tomllib = None
    _HAVE_TOMLLIB = False


# ---------------------------------------------------------------------------
# Sentinel: returned by parse_manifest() when nothing parses.
# Caller test: result["surface_kind"] == "none".
# ---------------------------------------------------------------------------
def NO_PARSEABLE_SURFACE(reasons=None):
    return {
        "surface_kind": "none",
        "manifest_path": "",
        "pkg_name": "",
        "provides_skeleton": {
            "exports": [],
            "bin": [],
            "entry_points": [],
            "public_names": [],
            "types": "",
        },
        "requires": {
            "runtime": [],
            "peer": [],
            "engine_node": "",
            "requires_python": "",
        },
        "recipe": {"install": ""},
        "flags": {
            "monorepo": False,
            "fallback": "",
            "parse_errors": list(reasons or []),
        },
    }


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
_PEP508_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")


def _dedup(seq):
    """Order-preserving, case-insensitive de-dup of a list of strings."""
    out, seen = [], set()
    for s in seq:
        if s is None:
            continue
        s = str(s)
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _pep508_name(spec: str) -> str:
    """'requests>=2.0,<3 ; python_version<\"3.9\"' -> 'requests' (lower-cased)."""
    m = _PEP508_NAME.match((spec or "").strip())
    return m.group(0).lower() if m else ""


def _collect_exports_keys(exports):
    """package.json 'exports' can be a string, a dict of subpaths, or a
    conditions dict. Return the list of export subpath keys (['.', './x'])."""
    if isinstance(exports, str):
        return ["."]
    if isinstance(exports, dict):
        keys = []
        for k in exports.keys():
            # subpath keys start with '.'; condition keys ('import','require',
            # 'default','node','types') are NOT subpaths — treat the whole map
            # as the '.' export in that case.
            if isinstance(k, str) and k.startswith("."):
                keys.append(k)
        if keys:
            return keys
        # conditions-only map (no subpaths) => the root export
        return ["."]
    return []


# ---------------------------------------------------------------------------
# JavaScript / TypeScript: package.json
# ---------------------------------------------------------------------------
def parse_package_json(text: str) -> dict:
    """
    Parse package.json text into:
      {entry_points, requires, install, runtime_floor, monorepo}

    - entry_points : public surface -> {exports, bin, main, module, types, public_names}
        exports = keys of the exports map (or ['.'] when only main/module present)
        bin     = bin command names (string bin => [pkg name])
    - requires     : {runtime:[...], peer:[...], engine_node:str}
        runtime = dependencies keys ; peer = peerDependencies keys
    - install      : canonical install command string
    - runtime_floor: engines.node specifier (raw), '' if absent
    - monorepo     : private==true AND workspaces present  (also true if
                     workspaces declared at all — orchestrator root)
    """
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("package.json is not a JSON object")
    except Exception as e:
        return {
            "entry_points": {"exports": [], "bin": [], "main": "", "module": "",
                             "types": "", "public_names": []},
            "requires": {"runtime": [], "peer": [], "engine_node": ""},
            "install": "",
            "runtime_floor": "",
            "monorepo": False,
            "parse_error": f"package.json: {type(e).__name__}",
        }

    name = data.get("name") or ""

    # ---- entry points / public surface -----------------------------------
    exports = _collect_exports_keys(data.get("exports"))
    main = data.get("main") if isinstance(data.get("main"), str) else ""
    module = data.get("module") if isinstance(data.get("module"), str) else ""
    if not exports and (main or module):
        exports = ["."]

    types_field = data.get("types") or data.get("typings") or ""
    if not isinstance(types_field, str):
        types_field = ""

    bin_field = data.get("bin")
    bins = []
    if isinstance(bin_field, str):
        # string bin => exposed under the (unscoped) package name
        bins = [name.split("/")[-1]] if name else []
    elif isinstance(bin_field, dict):
        bins = [str(k) for k in bin_field.keys()]

    entry_points = {
        "exports": _dedup(exports),
        "bin": _dedup(bins),
        "main": main,
        "module": module,
        "types": types_field,
        "public_names": _dedup([name] if name else []),
    }

    # ---- requires --------------------------------------------------------
    deps = data.get("dependencies")
    runtime = list(deps.keys()) if isinstance(deps, dict) else []
    peerdeps = data.get("peerDependencies")
    peer = list(peerdeps.keys()) if isinstance(peerdeps, dict) else []
    engines = data.get("engines")
    engine_node = ""
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        engine_node = engines["node"]

    requires = {
        "runtime": _dedup(runtime),
        "peer": _dedup(peer),
        "engine_node": engine_node,
    }

    # ---- monorepo detection ---------------------------------------------
    workspaces = data.get("workspaces")
    has_workspaces = bool(workspaces) and isinstance(workspaces, (list, dict))
    private = data.get("private") is True
    monorepo = bool(has_workspaces and private) or has_workspaces

    # ---- install recipe --------------------------------------------------
    install = "pnpm install" if monorepo else "npm install"

    return {
        "entry_points": entry_points,
        "requires": requires,
        "install": install,
        "runtime_floor": engine_node,
        "monorepo": monorepo,
    }


# ---------------------------------------------------------------------------
# Python: pyproject.toml
# ---------------------------------------------------------------------------
_TOML_KV_NAME = re.compile(r'(?m)^\s*name\s*=\s*["\']([^"\']+)["\']')


def parse_pyproject(text: str) -> dict:
    """
    Parse pyproject.toml into:
      {entry_points, requires, install, runtime_floor, monorepo}

    Precedence:
      - pkg_name = [project].name ; fallback [tool.poetry].name (poetry fallback).
      - runtime  = [project].dependencies (PEP508 -> bare names) ;
                   fallback [tool.poetry].dependencies keys minus 'python'.
      - requires_python = [project].requires-python ;
                   fallback [tool.poetry].dependencies.python.
      - bin (scripts) = [project.scripts] keys ; fallback [tool.poetry].scripts.
      - entry_points  = [project.entry-points.<group>] -> 'group/name' ;
                   fallback [tool.poetry].plugins.
      - public_names  = [pkg_name normalized] + top-level __all__ via regex.
    """
    flags_fallback = ""
    data = None
    parse_error = ""
    if _HAVE_TOMLLIB:
        try:
            data = tomllib.loads(text)
        except Exception as e:
            parse_error = f"pyproject.toml: {type(e).__name__}"
            data = None

    if data is None:
        # Regex fallback (tomllib missing or TOML invalid): grab only a name.
        m = _TOML_KV_NAME.search(text or "")
        name = m.group(1) if m else ""
        pub = _all_names_regex(text)
        return {
            "entry_points": {"exports": _dedup(_module_names(name) + pub),
                             "bin": [], "entry_points": [],
                             "public_names": _dedup(([name] if name else []) + pub)},
            "requires": {"runtime": [], "requires_python": ""},
            "install": "pip install .",
            "runtime_floor": "",
            "monorepo": False,
            "fallback": "regex",
            "parse_error": parse_error,
        }

    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}

    poetry_fallback = False

    # ---- name -----------------------------------------------------------
    name = project.get("name") or ""
    if not name and poetry:
        name = poetry.get("name") or ""
        if name:
            poetry_fallback = True
            flags_fallback = "poetry"

    # ---- runtime deps + requires_python ---------------------------------
    runtime = []
    requires_python = ""
    if project.get("dependencies") is not None or project.get("name") is not None:
        deps = project.get("dependencies")
        if isinstance(deps, list):
            runtime = [_pep508_name(d) for d in deps if isinstance(d, str)]
            runtime = [r for r in runtime if r]
        rp = project.get("requires-python")
        requires_python = rp if isinstance(rp, str) else ""

    if not runtime and poetry:
        pdeps = poetry.get("dependencies")
        if isinstance(pdeps, dict):
            poetry_fallback = True
            flags_fallback = flags_fallback or "poetry"
            for k, v in pdeps.items():
                if k.lower() == "python":
                    if not requires_python and isinstance(v, str):
                        requires_python = v
                    continue
                runtime.append(str(k).lower())

    # ---- scripts (bin) --------------------------------------------------
    bins = []
    scripts = project.get("scripts")
    if isinstance(scripts, dict):
        bins = [str(k) for k in scripts.keys()]
    if not bins and poetry:
        pscripts = poetry.get("scripts")
        if isinstance(pscripts, dict):
            bins = [str(k) for k in pscripts.keys()]
            if bins:
                poetry_fallback = True
                flags_fallback = flags_fallback or "poetry"

    # ---- entry-points / plugins -----------------------------------------
    eps = []
    ep_tbl = project.get("entry-points")
    if isinstance(ep_tbl, dict):
        for group, mapping in ep_tbl.items():
            if isinstance(mapping, dict):
                for nm, target in mapping.items():
                    eps.append(f"{group}/{nm}={target}")
    if not eps and poetry:
        plugins = poetry.get("plugins")
        if isinstance(plugins, dict):
            for group, mapping in plugins.items():
                if isinstance(mapping, dict):
                    for nm, target in mapping.items():
                        eps.append(f"{group}/{nm}={target}")
            if eps:
                poetry_fallback = True
                flags_fallback = flags_fallback or "poetry"

    # ---- public names: pkg_name normalized + top-level __all__ ----------
    pub = _all_names_regex(text)
    exports = _module_names(name) + pub
    public_names = ([name] if name else []) + pub

    install = "poetry install" if poetry_fallback else "pip install ."

    return {
        "entry_points": {
            "exports": _dedup(exports),
            "bin": _dedup(bins),
            "entry_points": _dedup(eps),
            "public_names": _dedup(public_names),
        },
        "requires": {
            "runtime": _dedup(runtime),
            "requires_python": requires_python,
        },
        "install": install,
        "runtime_floor": requires_python,
        "monorepo": False,
        "fallback": flags_fallback,
    }


_ALL_BLOCK = re.compile(r"__all__\s*=\s*[\[\(]([^\]\)]*)[\]\)]", re.S)
_QUOTED = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")


def _all_names_regex(source: str) -> list:
    """Best-effort: pull names out of a module-level __all__ = [...] / (...).
    Works on either a pyproject (won't match) or a real source snippet."""
    if not source:
        return []
    names = []
    for block in _ALL_BLOCK.findall(source):
        names.extend(_QUOTED.findall(block))
    return _dedup(names)[:40]


def _module_names(pkg_name: str) -> list:
    """Distribution name -> importable top-level module name (dashes->underscores)."""
    if not pkg_name:
        return []
    return [pkg_name.replace("-", "_").lower()]


# ---------------------------------------------------------------------------
# Python: setup.py / setup.cfg  (NEVER eval setup.py)
# ---------------------------------------------------------------------------
_SETUP_NAME = re.compile(r'''name\s*=\s*["']([^"']+)["']''')
_SETUP_INSTALL_REQ = re.compile(r"install_requires\s*=\s*\[([^\]]*)\]", re.S)
_SETUP_PY_REQ = re.compile(r'''python_requires\s*=\s*["']([^"']+)["']''')


def parse_setup(text: str) -> dict:
    """
    Static, eval-free parse of setup.py OR setup.cfg into:
      {entry_points, requires, install, runtime_floor, monorepo}

    setup.cfg ([metadata]/[options]) is genuinely static — parse it.
    setup.py is parsed by regex ONLY (name=, install_requires=[...],
    python_requires=). If neither yields a name+deps, returns dynamic_setup.
    """
    is_cfg = ("[metadata]" in (text or "")) or ("[options]" in (text or ""))

    name = ""
    runtime = []
    requires_python = ""
    pub = _all_names_regex(text)

    if is_cfg:
        import configparser
        cp = configparser.ConfigParser()
        dynamic = False
        try:
            cp.read_string(text)
        except Exception:
            dynamic = True
        if not dynamic:
            if cp.has_option("metadata", "name"):
                name = cp.get("metadata", "name").strip()
            if cp.has_option("options", "install_requires"):
                raw = cp.get("options", "install_requires")
                for line in raw.splitlines():
                    nm = _pep508_name(line)
                    if nm:
                        runtime.append(nm)
            if cp.has_option("options", "python_requires"):
                requires_python = cp.get("options", "python_requires").strip()
    else:
        # setup.py — regex only, never exec.
        m = _SETUP_NAME.search(text or "")
        name = m.group(1) if m else ""
        mr = _SETUP_INSTALL_REQ.search(text or "")
        if mr:
            for q in _QUOTED_DEP.findall(mr.group(1)):
                nm = _pep508_name(q)
                if nm:
                    runtime.append(nm)
        mp = _SETUP_PY_REQ.search(text or "")
        if mp:
            requires_python = mp.group(1)

    dynamic_setup = (not name) and (not runtime)

    return {
        "entry_points": {
            "exports": _dedup(_module_names(name) + pub),
            "bin": [],
            "entry_points": [],
            "public_names": _dedup(([name] if name else []) + pub),
        },
        "requires": {
            "runtime": _dedup(runtime),
            "requires_python": requires_python,
        },
        "install": "pip install .",
        "runtime_floor": requires_python,
        "monorepo": False,
        "dynamic_setup": dynamic_setup,
    }


_QUOTED_DEP = re.compile(r"""['"]([^'"]+)['"]""")


# ---------------------------------------------------------------------------
# Go: go.mod
# ---------------------------------------------------------------------------
_GO_MODULE = re.compile(r"(?m)^\s*module\s+(\S+)")
_GO_DIRECTIVE = re.compile(r"(?m)^\s*go\s+([0-9]+(?:\.[0-9]+){0,2})")
_GO_REQUIRE_BLOCK = re.compile(r"(?ms)^\s*require\s*\((.*?)^\s*\)")
_GO_REQUIRE_LINE = re.compile(r"(?m)^\s*require\s+(\S+)\s+(\S+)\s*$")


def parse_go_mod(text: str) -> dict:
    """
    Parse go.mod into:
      {entry_points, requires, install, runtime_floor, monorepo}

    - module path  -> entry_points.module / public_names
    - go directive -> runtime_floor
    - require block(s) -> requires.runtime  (strip replace/exclude — those are
      separate directives and never appear inside `require (...)`)
    """
    text = text or ""
    mm = _GO_MODULE.search(text)
    module_path = mm.group(1) if mm else ""

    gm = _GO_DIRECTIVE.search(text)
    go_version = gm.group(1) if gm else ""

    requires = []
    # block form: require ( a v1; b v2 )
    for block in _GO_REQUIRE_BLOCK.findall(text):
        for line in block.splitlines():
            line = line.split("//", 1)[0].strip()  # drop // indirect comments
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                requires.append(parts[0])
    # single-line form: require a v1
    for path, _ver in _GO_REQUIRE_LINE.findall(text):
        requires.append(path)

    # replace/exclude are NOT inside require(...) so they're never collected
    # above; nothing extra to strip. (Guard kept explicit for the contract.)
    requires = [r for r in requires if r]

    last = module_path.rstrip("/").split("/")[-1] if module_path else ""

    return {
        "entry_points": {
            "module": module_path,
            "exports": _dedup([module_path] if module_path else []),
            "bin": [],
            "public_names": _dedup([last] if last else []),
        },
        "requires": {
            "runtime": _dedup(requires),
            "go": go_version,
        },
        "install": "go build ./...",
        "runtime_floor": go_version,
        "monorepo": False,
    }


# ---------------------------------------------------------------------------
# Rust: Cargo.toml
# ---------------------------------------------------------------------------
def parse_cargo_toml(text: str) -> dict:
    """
    Parse Cargo.toml into:
      {entry_points, requires, install, runtime_floor, monorepo}

    - [package].name + edition
    - [lib] (name, or the package name -> a library export)
    - [[bin]] array of tables -> bin names
    - [dependencies] keys -> requires.runtime
    - virtual workspace ([workspace] with no [package]) -> monorepo=True,
      workspace_members from workspace.members.
    """
    text = text or ""
    data = None
    parse_error = ""
    if _HAVE_TOMLLIB:
        try:
            data = tomllib.loads(text)
        except Exception as e:
            parse_error = f"Cargo.toml: {type(e).__name__}"
            data = None
    if data is None:
        data = {}

    package = data.get("package") if isinstance(data.get("package"), dict) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}

    name = package.get("name") or ""
    edition = package.get("edition") or ""
    if not isinstance(edition, str):
        edition = ""

    is_virtual_workspace = bool(workspace) and not package
    members = []
    if isinstance(workspace.get("members"), list):
        members = [str(m) for m in workspace["members"]]

    # ---- bins -----------------------------------------------------------
    bins = []
    bin_tbl = data.get("bin")
    if isinstance(bin_tbl, list):  # [[bin]] -> list of tables
        for b in bin_tbl:
            if isinstance(b, dict) and b.get("name"):
                bins.append(str(b["name"]))

    # ---- lib export -----------------------------------------------------
    exports = []
    lib = data.get("lib")
    if isinstance(lib, dict):
        lib_name = lib.get("name") or name
        if lib_name:
            exports.append(str(lib_name))
    elif name:
        # default lib target = the crate (snake_cased) unless explicitly bin-only
        exports.append(name.replace("-", "_"))

    # ---- dependencies ---------------------------------------------------
    deps = data.get("dependencies")
    runtime = list(deps.keys()) if isinstance(deps, dict) else []

    pub = name.replace("-", "_") if name else ""

    result = {
        "entry_points": {
            "exports": _dedup(exports),
            "bin": _dedup(bins),
            "public_names": _dedup([pub] if pub else []),
        },
        "requires": {
            "runtime": _dedup(runtime),
            "edition": edition,
        },
        "install": "cargo build",
        "runtime_floor": edition,
        "monorepo": is_virtual_workspace or bool(members),
    }
    if is_virtual_workspace or members:
        result["workspace_members"] = _dedup(members)
    if parse_error:
        result["parse_error"] = parse_error
    return result


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def _kind_for(language: str, filename: str):
    """Decide which parser fires from filename (primary) then language hint.
    Returns (surface_kind, parser_callable) or (None, None)."""
    fn = (filename or "").lower().rsplit("/", 1)[-1]
    lang = (language or "").lower()

    if fn == "package.json":
        return "js", parse_package_json
    if fn == "pyproject.toml":
        return "py", parse_pyproject
    if fn in ("setup.py", "setup.cfg"):
        return "py", parse_setup
    if fn == "go.mod":
        return "go", parse_go_mod
    if fn == "cargo.toml":
        return "rust", parse_cargo_toml

    # filename didn't match — fall back to the language hint
    if lang in ("javascript", "typescript", "js", "ts", "node"):
        return "js", parse_package_json
    if lang in ("python", "py"):
        return "py", parse_pyproject
    if lang == "go":
        return "go", parse_go_mod
    if lang == "rust":
        return "rust", parse_cargo_toml
    return None, None


def parse_manifest(language: str, filename: str, text: str) -> dict:
    """
    Dispatch to the right per-language parser by filename (then language hint)
    and normalize into the canonical surface object. Returns
    NO_PARSEABLE_SURFACE([...]) when no parser matches OR the matched parser
    found no surface at all.
    """
    if not text or not text.strip():
        return NO_PARSEABLE_SURFACE([f"{filename or language}: empty manifest"])

    surface_kind, parser = _kind_for(language, filename)
    if parser is None:
        return NO_PARSEABLE_SURFACE(
            [f"no parser for language={language!r} filename={filename!r}"]
        )

    raw = parser(text)

    parse_errors = []
    if raw.get("parse_error"):
        parse_errors.append(raw["parse_error"])

    ep = raw.get("entry_points", {})
    req = raw.get("requires", {})

    provides_skeleton = {
        "exports": ep.get("exports", []),
        "bin": ep.get("bin", []),
        "entry_points": ep.get("entry_points", []),
        "public_names": ep.get("public_names", []),
        "types": ep.get("types", ""),
    }
    # language-specific extras preserved when present
    if "module" in ep:
        provides_skeleton["module"] = ep["module"]
    if "main" in ep:
        provides_skeleton["main"] = ep["main"]

    requires = {
        "runtime": req.get("runtime", []),
        "peer": req.get("peer", []),
        "engine_node": req.get("engine_node", ""),
        "requires_python": req.get("requires_python", ""),
    }
    # carry go/edition floors through too
    if req.get("go"):
        requires["go"] = req["go"]
    if req.get("edition"):
        requires["edition"] = req["edition"]

    pkg_name = ""
    pubs = provides_skeleton["public_names"]
    if pubs:
        pkg_name = pubs[0]

    # "no surface at all" => sentinel (caller relies on README-only path)
    has_surface = any([
        provides_skeleton["exports"],
        provides_skeleton["bin"],
        provides_skeleton["entry_points"],
        provides_skeleton.get("module"),
        requires["runtime"],
        pkg_name,
        raw.get("monorepo"),
    ])
    if not has_surface:
        reasons = parse_errors or [f"{filename}: nothing parseable"]
        return NO_PARSEABLE_SURFACE(reasons)

    fallback = raw.get("fallback", "")
    if raw.get("dynamic_setup"):
        fallback = fallback or "dynamic_setup"
        parse_errors.append(f"{filename}: dynamic setup, static metadata only")

    return {
        "surface_kind": surface_kind,
        "manifest_path": filename or "",
        "pkg_name": pkg_name,
        "provides_skeleton": provides_skeleton,
        "requires": requires,
        "recipe": {"install": raw.get("install", "")},
        "flags": {
            "monorepo": bool(raw.get("monorepo")),
            "fallback": fallback,
            "parse_errors": parse_errors[:5],
        },
        "workspace_members": raw.get("workspace_members", []),
    }


# ===========================================================================
# INLINE TESTS — real manifest snippets + assertions.
# Run: python3 e3_2_parsers.py
# ===========================================================================
if __name__ == "__main__":

    # ---- 1. JS: nanoid exports map + bin + peer + engines ----------------
    NANOID_PKG = r"""
    {
      "name": "nanoid",
      "version": "5.0.7",
      "type": "module",
      "main": "./index.cjs",
      "module": "./index.js",
      "types": "./index.d.ts",
      "bin": { "nanoid": "bin/nanoid.cjs" },
      "exports": {
        ".":            { "import": "./index.js", "require": "./index.cjs" },
        "./async":      { "import": "./async/index.js" },
        "./non-secure": { "import": "./non-secure/index.js" }
      },
      "dependencies": {},
      "peerDependencies": { "react": "^18" },
      "engines": { "node": "^18 || >=20" }
    }
    """
    js = parse_package_json(NANOID_PKG)
    assert js["entry_points"]["exports"] == [".", "./async", "./non-secure"], js["entry_points"]["exports"]
    assert js["entry_points"]["bin"] == ["nanoid"], js["entry_points"]["bin"]
    assert js["entry_points"]["types"] == "./index.d.ts"
    assert js["entry_points"]["module"] == "./index.js"
    assert js["requires"]["peer"] == ["react"], js["requires"]["peer"]
    assert js["requires"]["engine_node"] == "^18 || >=20"
    assert js["runtime_floor"] == "^18 || >=20"
    assert js["monorepo"] is False
    assert js["install"] == "npm install"

    # ---- 1b. JS monorepo: private + workspaces => monorepo ---------------
    MONOREPO_PKG = r"""
    {
      "name": "my-monorepo",
      "private": true,
      "workspaces": ["packages/*", "apps/*"]
    }
    """
    jsm = parse_package_json(MONOREPO_PKG)
    assert jsm["monorepo"] is True, jsm
    assert jsm["install"] == "pnpm install"

    # ---- 1c. JS string bin => package name ------------------------------
    STRINGBIN_PKG = r"""
    {"name":"@scope/tool","bin":"./cli.js","dependencies":{"chalk":"^5"}}
    """
    jsb = parse_package_json(STRINGBIN_PKG)
    assert jsb["entry_points"]["bin"] == ["tool"], jsb["entry_points"]["bin"]
    assert jsb["requires"]["runtime"] == ["chalk"]

    # ---- 2. PY: [project.scripts] pyproject -----------------------------
    PYPROJECT_PEP621 = r"""
    [project]
    name = "awesome-cli"
    version = "1.2.0"
    requires-python = ">=3.8"
    dependencies = [
        "requests>=2.28,<3",
        "click >= 8.0 ; python_version >= '3.7'",
        "rich",
    ]

    [project.scripts]
    awesome = "awesome_cli.main:run"
    awesome-debug = "awesome_cli.main:debug"

    [project.entry-points."flask.commands"]
    routes = "awesome_cli.web:routes"
    """
    py = parse_pyproject(PYPROJECT_PEP621)
    assert py["entry_points"]["bin"] == ["awesome", "awesome-debug"], py["entry_points"]["bin"]
    assert py["requires"]["runtime"] == ["requests", "click", "rich"], py["requires"]["runtime"]
    assert py["requires"]["requires_python"] == ">=3.8"
    assert py["entry_points"]["entry_points"] == ["flask.commands/routes=awesome_cli.web:routes"], py["entry_points"]["entry_points"]
    assert "awesome_cli" in py["entry_points"]["exports"], py["entry_points"]["exports"]
    assert py["install"] == "pip install ."
    assert py.get("fallback", "") == ""

    # ---- 3. PY: poetry fallback ([tool.poetry], no [project]) -----------
    PYPROJECT_POETRY = r"""
    [tool.poetry]
    name = "legacy-lib"
    version = "0.3.1"

    [tool.poetry.dependencies]
    python = "^3.9"
    httpx = "^0.27"
    pydantic = "^2"

    [tool.poetry.scripts]
    legacy = "legacy_lib.cli:app"
    """
    pp = parse_pyproject(PYPROJECT_POETRY)
    assert pp["entry_points"]["public_names"][0] == "legacy-lib", pp["entry_points"]["public_names"]
    assert pp["requires"]["runtime"] == ["httpx", "pydantic"], pp["requires"]["runtime"]
    assert pp["requires"]["requires_python"] == "^3.9", pp["requires"]["requires_python"]
    assert pp["entry_points"]["bin"] == ["legacy"], pp["entry_points"]["bin"]
    assert pp["fallback"] == "poetry", pp["fallback"]
    assert pp["install"] == "poetry install"

    # ---- 3b. PY: setup.py regex + __all__ -------------------------------
    SETUP_PY = r"""
import setuptools

__all__ = ["Foo", "Bar", "make_thing"]

setuptools.setup(
    name="oldskool",
    version="0.1",
    install_requires=["six", "requests>=2.0"],
    python_requires=">=2.7",
)
    """
    sp = parse_setup(SETUP_PY)
    assert sp["entry_points"]["public_names"][0] == "oldskool"
    assert "Foo" in sp["entry_points"]["public_names"], sp["entry_points"]["public_names"]
    assert sp["requires"]["runtime"] == ["six", "requests"], sp["requires"]["runtime"]
    assert sp["requires"]["requires_python"] == ">=2.7"
    assert sp["dynamic_setup"] is False

    # ---- 3c. PY: setup.cfg ----------------------------------------------
    SETUP_CFG = """
[metadata]
name = cfgpkg

[options]
python_requires = >=3.7
install_requires =
    numpy
    pandas>=1.0
"""
    sc = parse_setup(SETUP_CFG)
    assert sc["entry_points"]["public_names"][0] == "cfgpkg"
    assert sc["requires"]["runtime"] == ["numpy", "pandas"], sc["requires"]["runtime"]
    assert sc["requires"]["requires_python"] == ">=3.7"

    # ---- 4. GO: go.mod with replace + exclude (must be stripped) --------
    GO_MOD = """
module github.com/spf13/viper

go 1.21

require (
    github.com/fsnotify/fsnotify v1.7.0
    github.com/spf13/pflag v1.0.5 // indirect
    github.com/mitchellh/mapstructure v1.5.0
)

require github.com/stretchr/testify v1.9.0

replace github.com/fsnotify/fsnotify => github.com/fsnotify/fsnotify v1.6.0

exclude github.com/bad/dependency v0.0.1
"""
    go = parse_go_mod(GO_MOD)
    assert go["entry_points"]["module"] == "github.com/spf13/viper", go["entry_points"]["module"]
    assert go["requires"]["go"] == "1.21", go["requires"]["go"]
    assert go["runtime_floor"] == "1.21"
    rt = go["requires"]["runtime"]
    assert "github.com/fsnotify/fsnotify" in rt
    assert "github.com/spf13/pflag" in rt
    assert "github.com/mitchellh/mapstructure" in rt
    assert "github.com/stretchr/testify" in rt
    # replace/exclude targets must NOT appear as require entries
    assert "github.com/bad/dependency" not in rt, rt
    assert go["entry_points"]["public_names"] == ["viper"], go["entry_points"]["public_names"]

    # ---- 5. RUST: Cargo.toml with [[bin]] -------------------------------
    CARGO_TOML = """
[package]
name = "ripgrep-clone"
version = "0.1.0"
edition = "2021"

[lib]
name = "rgclone"

[[bin]]
name = "rgc"
path = "src/main.rs"

[[bin]]
name = "rgc-helper"
path = "src/helper.rs"

[dependencies]
regex = "1.10"
clap = { version = "4", features = ["derive"] }
"""
    rust = parse_cargo_toml(CARGO_TOML)
    assert rust["entry_points"]["bin"] == ["rgc", "rgc-helper"], rust["entry_points"]["bin"]
    assert rust["entry_points"]["exports"] == ["rgclone"], rust["entry_points"]["exports"]
    assert rust["requires"]["runtime"] == ["regex", "clap"], rust["requires"]["runtime"]
    assert rust["requires"]["edition"] == "2021"
    assert rust["runtime_floor"] == "2021"
    assert rust["monorepo"] is False

    # ---- 5b. RUST: virtual workspace ------------------------------------
    CARGO_VWS = """
[workspace]
members = ["crates/core", "crates/cli", "crates/macros"]
resolver = "2"
"""
    vws = parse_cargo_toml(CARGO_VWS)
    assert vws["monorepo"] is True, vws
    assert vws["workspace_members"] == ["crates/core", "crates/cli", "crates/macros"], vws["workspace_members"]
    assert vws["entry_points"]["bin"] == []

    # ---- 6. DISPATCHER + sentinel ---------------------------------------
    d_js = parse_manifest("JavaScript", "package.json", NANOID_PKG)
    assert d_js["surface_kind"] == "js"
    assert d_js["pkg_name"] == "nanoid"
    assert d_js["recipe"]["install"] == "npm install"
    assert d_js["provides_skeleton"]["exports"] == [".", "./async", "./non-secure"]

    d_py = parse_manifest("Python", "pyproject.toml", PYPROJECT_PEP621)
    assert d_py["surface_kind"] == "py"
    assert d_py["recipe"]["install"] == "pip install ."

    d_go = parse_manifest("Go", "go.mod", GO_MOD)
    assert d_go["surface_kind"] == "go"
    assert d_go["recipe"]["install"] == "go build ./..."

    d_rust = parse_manifest("Rust", "Cargo.toml", CARGO_TOML)
    assert d_rust["surface_kind"] == "rust"
    assert d_rust["recipe"]["install"] == "cargo build"

    # sentinel: unknown manifest
    sent = parse_manifest("Ruby", "Gemfile", "source 'https://rubygems.org'")
    assert sent["surface_kind"] == "none", sent
    assert sent["flags"]["parse_errors"], sent

    # sentinel: empty manifest
    sent2 = parse_manifest("Python", "pyproject.toml", "   ")
    assert sent2["surface_kind"] == "none", sent2

    # sentinel: garbage JSON
    sent3 = parse_manifest("JavaScript", "package.json", "{ not valid json ")
    assert sent3["surface_kind"] == "none", sent3
    assert any("package.json" in e for e in sent3["flags"]["parse_errors"]), sent3

    print("ALL ASSERTIONS PASSED")
    print()
    print("=== one parsed example per language (via parse_manifest) ===")
    for label, res in [
        ("JS  ", parse_manifest("JavaScript", "package.json", NANOID_PKG)),
        ("PY  ", parse_manifest("Python", "pyproject.toml", PYPROJECT_PEP621)),
        ("GO  ", parse_manifest("Go", "go.mod", GO_MOD)),
        ("RUST", parse_manifest("Rust", "Cargo.toml", CARGO_TOML)),
    ]:
        print(f"\n[{label}] {res['surface_kind']}  pkg={res['pkg_name']!r}  install={res['recipe']['install']!r}")
        print("  provides_skeleton:", json.dumps(res["provides_skeleton"], ensure_ascii=False))
        print("  requires:         ", json.dumps(res["requires"], ensure_ascii=False))
        print("  flags:            ", json.dumps(res["flags"], ensure_ascii=False))
