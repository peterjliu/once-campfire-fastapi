"""Compile the pinned Propshaft/importmap assets without invoking Ruby.

Adapted from once-campfire-django's bin/build-assets (originally the Go port's algorithm).
"""

import hashlib
import html
import json
import posixpath
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "assets/sources"
RAILS = ROOT / "reference"
OUT = ROOT / "assets/generated"
WS = r"[ \t\n\x0b\x0c\r]"
PATTERNS: dict[str, re.Pattern[str]] = {
    "css": re.compile(
        r"url\("
        + WS
        + r"""*["']?(?!(?:\#|%23|data:|http:|https:|//))([^"' \t\n\x0b\x0c\r?#)]+)([#?][^"')]+)?"""
        + WS
        + r"""*["']?\)"""
    ),
    "js": re.compile(
        r"RAILS_ASSET_URL\("
        + WS
        + r"""*["']?(?!(?:\#|%23|data|http|//))([^"' \t\n\x0b\x0c\r?#)]+)([#?][^"')]+)?"""
        + WS
        + r"""*["']?\)"""
    ),
}
SOURCE_MAP = re.compile(
    r"(//|/\*)# sourceMappingURL=(.+\.map)(" + WS + r"*?\*/)?" + WS + r"*?(?=\n?\Z)"
)


def plus(left: str, right: str) -> str:
    prefix = [p for p in left.split("/") if p]
    suffix = [p for p in right.split("/") if p]
    while True:
        while suffix and suffix[0] == ".":
            suffix.pop(0)
        if not prefix:
            break
        last = prefix.pop()
        if last == ".":
            continue
        if last == ".." or not suffix or suffix[0] != "..":
            prefix.append(last)
            break
        suffix.pop(0)
    return "/".join(prefix + suffix) or "."


def resolve(directory: str, url: str) -> str:
    if url.startswith("../"):
        return posixpath.normpath(plus(directory, url))
    if url.startswith("/"):
        return url[1:]
    return plus(directory, url.removeprefix("./"))


def directory_of(name: str) -> str:
    return name.rsplit("/", 1)[0] if "/" in name else "."


def kind_of(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def load_assets() -> dict[str, bytes]:
    paths = [ROOT / "assets/overrides", SOURCES / "overrides"]
    for line in (SOURCES / "vendor/LOAD_PATH").read_text().splitlines():
        if line:
            kind, p = line.split(":", 1)
            paths.append((RAILS if kind == "reference" else SOURCES / "vendor") / p)
    assets: dict[str, bytes] = {}
    for folder in paths:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                assets.setdefault(p.relative_to(folder).as_posix(), p.read_bytes())
    return assets


def references(assets: dict[str, bytes], name: str) -> list[str]:
    result: list[str] = []
    pattern = PATTERNS.get(kind_of(name))
    if pattern is None:
        return result

    def walk(current: str) -> None:
        for m in pattern.finditer(assets[current].decode("latin1")):
            url = m[1].encode("latin1").decode("utf8", "replace")
            found = resolve(directory_of(current), url)
            if found in assets and found not in result:
                result.append(found)
                walk(found)

    walk(name)
    return result


def build() -> None:
    assets = load_assets()
    initializer = (RAILS / "config/initializers/assets.rb").read_text()
    match = re.search(
        r"""^\s*Rails\.application\.config\.assets\.version\s*=\s*["']([^"']*)""",
        initializer,
        re.M,
    )
    version = (match.group(1) if match else "1").encode()

    manifest: dict[str, str] = {}
    for name, data in assets.items():
        digest = hashlib.sha1(
            data + b"".join(assets[p] for p in references(assets, name)) + version
        ).hexdigest()[:8]
        manifest[name] = (
            name
            if re.search(r"-[0-9a-zA-Z_-]{7,128}\.digested", name)
            else re.sub(r"\.(\w+(?:\.map)?)$", lambda m, digest=digest: "-" + digest + m[0], name)
        )

    def url(name: str) -> str:
        return "/assets/" + manifest[name]

    def compile_asset(name: str, data: bytes) -> bytes:
        pattern = PATTERNS.get(kind_of(name))
        if pattern is None:
            return data

        def replacement(m: re.Match[str]) -> str:
            original = m[1].encode("latin1").decode("utf8", "replace")
            found = resolve(directory_of(name), original)
            value = '"' + (url(found) + (m[2] or "") if found in assets else original) + '"'
            if kind_of(name) == "css":
                value = "url(" + value + ")"
            return value.encode().decode("latin1")

        text = pattern.sub(replacement, data.decode("latin1"))

        def mapping(m: re.Match[str]) -> str:
            value = re.sub(r"^(.+/)?/assets/", "", m[2], flags=re.M)
            found = plus(directory_of(name), value) if directory_of(name) != "." else value
            return (
                m[1]
                + ("# sourceMappingURL=" + url(found) if found in assets else "")
                + (m[3] or "")
            )

        return SOURCE_MAP.sub(mapping, text).encode("latin1")

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "public/assets").mkdir(parents=True)
    for p in sorted((RAILS / "public").rglob("*")):
        if p.is_file() and "assets" not in p.relative_to(RAILS / "public").parts:
            target = OUT / "public" / p.relative_to(RAILS / "public")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
    for name, data in assets.items():
        target = OUT / "public/assets" / manifest[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(compile_asset(name, data))
    dumped = json.dumps(
        {name: {"digested_path": path, "integrity": None} for name, path in manifest.items()},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    (OUT / "manifest.json").write_text(dumped)
    (OUT / "public/assets/.manifest.json").write_text(dumped)
    write_importmap(manifest)
    styles = [
        '<link rel="stylesheet" href="' + url(p) + '" data-turbo-track="reload" />'
        for p in sorted(assets)
        if p.endswith(".css")
    ]
    (OUT / "stylesheets.html").write_text("\n".join(styles))
    print(f"Compiled {len(assets)} assets")


def write_importmap(manifest: dict[str, str]) -> None:
    def link(name: str) -> str:
        return "/assets/" + manifest[name]

    packages: dict[str, tuple[str, bool]] = {}
    directories: list[tuple[str, dict[str, str], bool]] = []
    for raw in (RAILS / "config/importmap.rb").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"""(pin|pin_all_from)\s+["']([^"']+)["'](.*)""", line)
        if not match:
            raise ValueError("unsupported importmap: " + line)
        command, name, tail = match.groups()
        options = dict(re.findall(r"""(to|under):\s*["']([^"']*)["']""", tail))
        preload = "preload: false" not in tail
        if command == "pin":
            packages[name] = (options.get("to", name + ".js"), preload)
        else:
            directories.append((name, options, preload))
    for name, options, preload in directories:
        folder = RAILS / name
        for p in sorted(folder.rglob("*")):
            if p.suffix not in (".js", ".jsm") or any(
                x.startswith(".") for x in p.relative_to(folder).parts
            ):
                continue
            filename = p.relative_to(folder).as_posix()
            stem = filename[: -len(p.suffix)]
            stem = "" if stem == "index" else stem.removesuffix("/index")
            package = "/".join(filter(None, [options.get("under"), stem]))
            logical = "/".join(filter(None, [options.get("to", options.get("under")), filename]))
            packages[package] = (logical, preload)
    imports = {name: link(p) for name, (p, _) in packages.items() if p in manifest}
    preloads = list(
        dict.fromkeys(link(p) for p, preload in packages.values() if preload and p in manifest)
    )
    tags = (
        '<script type="importmap" data-turbo-track="reload">'
        + json.dumps({"imports": imports}, ensure_ascii=False, indent=2)
        + "</script>\n"
    )
    tags += "\n".join(
        '<link rel="modulepreload" href="' + html.escape(p, quote=True) + '">' for p in preloads
    )
    tags += '\n<script type="module">import "application"</script>'
    (OUT / "importmap.html").write_text(tags)


if __name__ == "__main__":
    build()
