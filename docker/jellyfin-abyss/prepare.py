#!/usr/bin/env python3
"""Prepare reviewed Abyss assets offline. --check validates without writing.

vendor/ is an intact snapshot. All local differences live here; regeneration
fails before writes if that snapshot or a required transformation seam changes.
No installer, administrator token, network access or Jellyfin configuration is used.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

COMMIT = "2f809171ed7f3a34b5e78297608b5f2b63dd3f0f"
VENDOR_FILES = {"abyss.css", "spotlight.css", "spotlight.html", "spotlight-loader.js", "LICENSE"}
FONT_FILES = {
    "google-sans-normal-latin.woff2",
    "google-sans-normal-latin-ext.woff2",
    "google-sans-italic-latin.woff2",
    "google-sans-italic-latin-ext.woff2",
    "material-icons-round.woff2",
    "LICENSE-material-icons.txt",
    "OFL.txt",
    "TRADEMARKS-google-sans.md",
    "material-icons.package.json",
    "google-sans.source.css",
}
GOOGLE_CSS_URL = (
    "https://fonts.googleapis.com/css2?family=Google+Sans:ital,opsz,wght@"
    "0,17..18,400..700;1,17..18,400..700&display=swap"
)
MATERIAL_FONT_URL = (
    "https://cdn.jsdelivr.net/npm/material-icons@1.13.12/iconfont/material-icons-round.woff2"
)


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError("integrity: expected exactly one reviewed transformation seam")
    return source.replace(old, new, 1)


def replace_span(source: str, start: str, end: str, replacement: str = "") -> str:
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError("integrity: missing or ambiguous reviewed transformation boundary")
    first, last = source.index(start), source.index(end)
    if first >= last:
        raise ValueError("integrity: reversed transformation boundaries")
    return source[:first] + replacement + source[last:]


def prepare_css(source: str) -> str:
    # Keep all upstream font families, icon ligatures and presentation rules.
    source = replace_once(source, GOOGLE_CSS_URL, "google-sans.css")
    source = replace_once(source, MATERIAL_FONT_URL, "material-icons-round.woff2")
    # Jellyfin 12.2 legacy carousels extend past the root viewport. Clip only
    # the root's horizontal overflow; each carousel keeps its own scrolling.
    return source + (
        "\n/* HomeServer: contain desktop carousel overflow. */\n"
        "html.layout-desktop { overflow-x: clip; }\n"
        "/* Jellyfin 12.2 has no withBackdrop class for the Spotlight overlay. */\n"
        "html.abyss-spotlight-visible .backgroundContainer "
        "{ background: transparent !important; }\n"
        "/* Hide the home/favourites pill; keep library navigation tabs. */\n"
        "body:has(#indexPage.homePage:not(.hide):not([hidden])) "
        ".headerTabs.sectionTabs { display: none !important; }\n"
    )


def prepare_html(source: str) -> str:
    source = replace_span(
        source,
        '\t<link rel="preconnect" href="https://fonts.googleapis.com" />',
        '\t<link rel="stylesheet" href="spotlight.css" />',
        '\t<link rel="stylesheet" href="google-sans.css" />\n',
    )
    source = replace_span(source, "\t<!-- Update notification -->", "\t<script>")
    source = replace_once(
        source,
        "\t\t// Updated automatically by the installer on each install/update.\n"
        "\t\tconst ABYSS_SPOTLIGHT_VERSION = '1.2.3';\n\n",
        "\t\t// HomeServer: pinned, offline assets; updates are reviewed at build time.\n",
    )
    source = replace_span(
        source,
        "\t\t\tfunction semverGt(a, b) {",
        "\t\t\tconst base = getBaseUrl();",
    )
    source = replace_once(source, "\t\t\tcheckForUpdate(base, token, userId);\n", "")
    source = replace_once(
        source,
        "const syncUrl = artwork.backdrop\n"
        "\t\t\t\t\t\t? (artwork.primaryBackdrop ? itemImageUrls(item).primary"
        " : itemImageUrls(item).backdrop)\n"
        "\t\t\t\t\t\t: null;",
        "// Reuse the authenticated image blob; never make an anonymous image request.\n"
        "\t\t\t\t\tconst syncUrl = artwork.backdrop;",
    )
    return source


def prepare_loader(source: str) -> str:
    source = replace_once(
        source,
        "    if (!currentIframe || event.source !== currentIframe.contentWindow) return;",
        "    if (event.origin !== window.location.origin) return;\n"
        "    if (!currentIframe || event.source !== currentIframe.contentWindow) return;",
    )
    source = replace_once(
        source,
        "  function setSpotlightBackdrop(url) {\n    if (!url || !backdropOverlay) return;",
        "  function setSpotlightBackdrop(url) {\n"
        "    if (!url || !backdropOverlay) return;\n"
        "    try {\n"
        "      var parsed = new URL(url);\n"
        '      if (parsed.protocol !== "blob:" || '
        "parsed.origin !== window.location.origin) return;\n"
        "    } catch (error) { return; }",
    )
    source = replace_span(
        source,
        '    var targetOrigin = "*";',
        "    safe(function () {\n      iframe.contentWindow.postMessage",
        "    var targetOrigin = window.location.origin;\n"
        '    if (!targetOrigin || targetOrigin === "null") return;\n',
    )
    return source


def read_checked_files(directory: Path, hashes: dict, expected: set[str]) -> dict[str, bytes]:
    if set(hashes) != expected or {path.name for path in directory.iterdir()} != expected:
        raise ValueError(f"integrity: missing or unlisted file in {directory.name}")
    result = {}
    for name, digest in sorted(hashes.items()):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"integrity: input is not a regular file: {name}")
        content = path.read_bytes()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"integrity: invalid hash: {name}")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"integrity: input SHA-256 mismatch: {name}")
        result[name] = content
    return result


def prepare_fonts(fonts: dict[str, bytes], sources: dict) -> dict[str, bytes]:
    result = {
        name: content
        for name, content in fonts.items()
        if name not in {"google-sans.source.css", "material-icons.package.json"}
    }
    # Older runtime packages served the Google Sans license at this path.
    result["OFL-google-sans.txt"] = fonts["OFL.txt"]
    stylesheet = fonts["google-sans.source.css"].decode("utf-8").replace("\r\n", "\n")
    selected = []
    seen = set()
    for subset, face in re.findall(
        r"/\* (latin(?:-ext)?) \*/\s*(@font-face\s*\{[^}]+\})", stylesheet
    ):
        style_match = re.search(r"font-style:\s*(normal|italic);", face)
        url_match = re.search(r"src:\s*url\((https://fonts\.gstatic\.com/[^)]+)\)", face)
        if not style_match or not url_match:
            raise ValueError("integrity: unexpected Google Sans CSS structure")
        name = f"google-sans-{style_match[1]}-{subset}.woff2"
        if name in seen or sources.get(name) != url_match[1] or name not in fonts:
            raise ValueError("integrity: ambiguous or unreviewed Google Sans font source")
        if "font-weight: 400 700;" not in face or "font-family: 'Google Sans';" not in face:
            raise ValueError("integrity: unexpected Google Sans family or weight range")
        seen.add(name)
        selected.append(f"/* {subset} */\n" + face.replace(url_match[1], name))
    if len(seen) != 4:
        raise ValueError("integrity: expected four Google Sans Latin font faces")
    result["google-sans.css"] = ("\n".join(selected) + "\n").encode("utf-8")
    return result


def build_assets(package: Path) -> dict[str, bytes]:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    if manifest["commit"] != COMMIT:
        raise ValueError("integrity: unexpected upstream manifest")
    vendor = read_checked_files(package / "vendor", manifest["vendor_sha256"], VENDOR_FILES)
    fonts = read_checked_files(package / "fonts", manifest["font_sha256"], FONT_FILES)
    transformers = {
        "abyss.css": prepare_css,
        "spotlight.css": None,
        "spotlight.html": prepare_html,
        "spotlight-loader.js": prepare_loader,
    }
    result = prepare_fonts(fonts, manifest["font_sources"])
    result["abyss-defaults.js"] = (package / "local" / "abyss-defaults.js").read_bytes()
    result["LICENSE"] = vendor["LICENSE"]
    for name, transform in transformers.items():
        source = vendor[name].decode("utf-8").replace("\r\n", "\n")
        content = transform(source) if transform else source
        # SVG namespace identifiers are not requests. Every other absolute URL
        # would reintroduce a remote dependency into these reviewed assets.
        urls = re.findall(r"https?://[^\s\"'<>)]*", content)
        if any(url != "http://www.w3.org/2000/svg" for url in urls):
            raise ValueError(f"integrity: unexpected external URL in runtime: {name}")
        result[name] = content.encode("utf-8")
    checksums = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in sorted(result.items())
    )
    result["SHA256SUMS"] = checksums.encode("ascii")
    return result


def prepare(package: Path, *, check: bool = False) -> None:
    assets = build_assets(package)
    output = package / "runtime"
    if output.is_symlink():
        raise ValueError("integrity: runtime directory must not be a symlink")
    if output.exists():
        if {path.name for path in output.iterdir()} - set(assets):
            raise ValueError("integrity: runtime contains unlisted files; no files changed")
        for name in assets:
            target = output / name
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f"integrity: runtime target is not a regular file: {name}")
    if check:
        for name, content in assets.items():
            target = output / name
            if not target.is_file() or target.read_bytes() != content:
                raise ValueError(
                    f"integrity: runtime missing or differs from prepared assets: {name}"
                )
        return
    output.mkdir(exist_ok=True)
    for name, content in assets.items():
        target = output / name
        temporary = output / (name + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check without modifying files")
    args = parser.parse_args()
    try:
        prepare(Path(__file__).resolve().parent, check=args.check)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Abyss preparation refused: {error}", file=sys.stderr)
        return 1
    print("Abyss runtime verified" if args.check else "Abyss runtime prepared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
