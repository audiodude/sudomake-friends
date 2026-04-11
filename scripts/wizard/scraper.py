import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from wizard.claude import SCRAPE_TIMEOUT
from wizard.platforms import PLATFORMS, detect_platform


def _fetch_page(url: str, timeout: int = 10) -> str | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_links(html: str, base_url: str) -> list[str]:
    base_domain = urlparse(base_url).netloc
    links = []
    for match in re.finditer(r'href=["\']([^"\']+)["\']', html):
        href = match.group(1)
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if (parsed.netloc == base_domain
                and not parsed.path.endswith(('.png', '.jpg', '.gif', '.css', '.js', '.svg', '.pdf', '.zip'))
                and '#' not in full):
            links.append(full.split('#')[0])
    return list(dict.fromkeys(links))


def _strip_html(html: str) -> str:
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def scrape_site(url: str, cache_dir: Path) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(url.encode()).hexdigest()
    cache_file = cache_dir / f"{cache_key}.txt"
    if cache_file.exists():
        print(f"  Using cached scrape for {url}")
        return cache_file.read_text()

    print(f"  Scraping {url} (up to 3 minutes)...")
    start = time.time()
    visited = set()
    to_visit = [url]
    pages = []

    while to_visit and (time.time() - start) < SCRAPE_TIMEOUT:
        current = to_visit.pop(0)
        if current in visited:
            continue
        visited.add(current)

        html = _fetch_page(current)
        if not html:
            continue

        text = _strip_html(html)
        if len(text) > 100:
            pages.append(f"--- Page: {current} ---\n{text[:3000]}")
            print(f"    [{len(pages)}] {current[:60]}...")

        if len(pages) < 15:
            for link in _extract_links(html, url):
                if link not in visited:
                    to_visit.append(link)

    elapsed = time.time() - start
    print(f"  Scraped {len(pages)} pages in {elapsed:.0f}s")

    result = "\n\n".join(pages)[:30000]
    cache_file.write_text(result)
    return result


def _fetch_url(url: str, paths: dict) -> str:
    """Fetch a single URL — auto-detect platform or fall back to scrape."""
    plugin = detect_platform(url)
    if plugin:
        print(f"  Detected: {plugin['name']}")
        return plugin["fetch"](url, paths["scrape_cache"])

    # Generic website scrape
    result = scrape_site(url, paths["scrape_cache"])
    if result:
        return f"Website content from {url}:\n\n{result}"
    return ""


def _show_platform_help():
    """Show supported platforms."""
    print("\n  Supported platforms (auto-detected from URL):\n")
    for p in PLATFORMS:
        print(f"    {p['name']:16s} {p['description']}")
    print(f"\n    {'Any website':16s} Falls back to crawling + scraping")
    print()
    print("  You can provide multiple URLs to build a richer profile.")
    print("  Example: your website, Mastodon, Last.fm, and GitHub.\n")


def _extract_text_from_archive(path: Path, max_bytes: int = 30000) -> str:
    """Extract readable text files from a tar/zip archive."""
    import tarfile
    import zipfile

    text_extensions = {".txt", ".md", ".json", ".csv", ".html", ".htm",
                       ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                       ".py", ".js", ".ts", ".rst", ".log", ".mbox"}
    parts = []
    total = 0

    try:
        if tarfile.is_tarfile(str(path)):
            with tarfile.open(str(path), "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    suffix = Path(member.name).suffix.lower()
                    if suffix not in text_extensions:
                        continue
                    try:
                        f = tf.extractfile(member)
                        if f:
                            chunk = f.read(max_bytes - total).decode("utf-8", errors="replace")
                            parts.append(f"--- {member.name} ---\n{chunk}")
                            total += len(chunk)
                            if total >= max_bytes:
                                break
                    except Exception:
                        continue
        elif zipfile.is_zipfile(str(path)):
            with zipfile.ZipFile(str(path)) as zf:
                for name in zf.namelist():
                    suffix = Path(name).suffix.lower()
                    if suffix not in text_extensions:
                        continue
                    try:
                        chunk = zf.read(name)[:max_bytes - total].decode("utf-8", errors="replace")
                        parts.append(f"--- {name} ---\n{chunk}")
                        total += len(chunk)
                        if total >= max_bytes:
                            break
                    except Exception:
                        continue
    except Exception as e:
        print(f"  Error reading archive: {e}")
        return ""

    print(f"  Extracted {len(parts)} text files from archive")
    return "\n\n".join(parts)


def get_user_context(paths: dict,
                     cached_sources: list[dict] | None = None,
                     on_save_sources=None) -> tuple[str, list[dict]]:
    """Interactive loop to collect user context from multiple sources."""
    all_parts = []
    sources = []

    def _save():
        if on_save_sources:
            on_save_sources(sources)

    # Check for sources.txt in current directory
    sources_file = Path("sources.txt")
    if sources_file.exists() and not cached_sources:
        lines = [l.strip() for l in sources_file.read_text().splitlines() if l.strip()]
        if lines:
            print(f"\n  Found sources.txt with {len(lines)} source(s):")
            for l in lines:
                print(f"    - {l}")
            use_file = input("  Use these sources? [Y/n]: ").strip().lower()
            if use_file in ("", "y", "yes"):
                for line in lines:
                    print(f"\n  Processing: {line}")
                    if line.startswith("http://") or line.startswith("https://"):
                        result = _fetch_url(line, paths)
                        if result:
                            all_parts.append(result)
                            sources.append({"label": line, "content": result})
                            _save()
                            print(f"  Added.")
                        else:
                            print(f"  Got nothing from {line}. Skipping.")
                    else:
                        fpath = Path(line).expanduser()
                        if fpath.exists() and fpath.is_file():
                            try:
                                content = fpath.read_bytes().decode("utf-8", errors="replace")[:15000]
                            except Exception:
                                print(f"  Can't read {fpath.name}. Skipping.")
                                continue
                            if content:
                                all_parts.append(f"File ({fpath.name}):\n{content}")
                                sources.append({"label": fpath.name, "content": f"File ({fpath.name}):\n{content}"})
                                _save()
                                print(f"  Added {fpath.name}.")
                        else:
                            # Treat as free-text description
                            all_parts.append(f"Self-description:\n{line}")
                            sources.append({"label": "description", "content": f"Self-description:\n{line}"})
                            _save()
                            print(f"  Added as description.")
                print(f"\n  Loaded {len(sources)} source(s) from sources.txt.")

    # Offer to reuse cached sources
    if cached_sources:
        print(f"\n  Found {len(cached_sources)} source(s) from last time:")
        for s in cached_sources:
            print(f"    - {s['label']}")
        reuse = input("  Reuse these? [Y/n]: ").strip().lower()
        if reuse in ("", "y", "yes"):
            for s in cached_sources:
                all_parts.append(s["content"])
                sources.append(s)
            print(f"  Loaded {len(sources)} cached source(s).")
        else:
            print("  Starting fresh.")

    if all_parts:
        print(f"\n  You can add more sources, or q to continue with what you have.")
    else:
        print("\n  Tell me about yourself. You can provide as many sources as you want.")
        print("  The more context, the better your friends will match you.")
    print("\n  Enter a URL, file path, or type a description.")
    print("  Type ? for supported platforms, q when done.\n")

    while True:
        prompt = f"  [{len(all_parts)} source{'s' if len(all_parts) != 1 else ''}] > "
        entry = input(prompt).strip()

        if not entry:
            continue
        if entry == "q":
            if all_parts:
                break
            print("  Need at least one source. Enter a URL, file path, or description.")
            continue
        if entry == "?":
            _show_platform_help()
            continue

        # URL?
        if entry.startswith("http://") or entry.startswith("https://"):
            result = _fetch_url(entry, paths)
            if result:
                all_parts.append(result)
                sources.append({"label": entry, "content": result})
                _save()
                print(f"  Added. Enter another, or q to finish.")
            else:
                print("  Got nothing from that URL. Try another?")
            continue

        # File path?
        fpath = Path(entry).expanduser()
        if fpath.exists() and fpath.is_file():
            if fpath.suffix in (".tgz", ".gz", ".tar", ".zip"):
                content = _extract_text_from_archive(fpath)
            else:
                try:
                    content = fpath.read_bytes().decode("utf-8", errors="replace")[:15000]
                except Exception:
                    print(f"  Can't read {fpath.name}. Skipping.")
                    continue
            if content:
                all_parts.append(f"File ({fpath.name}):\n{content}")
                sources.append({"label": fpath.name, "content": f"File ({fpath.name}):\n{content}"})
                _save()
                print(f"  Added {fpath.name}. Enter another, or q to finish.")
            else:
                print(f"  No readable text found in {fpath.name}.")
            continue

        # Check if it looks like a path that doesn't exist
        if "/" in entry or entry.startswith("~") or entry.startswith("."):
            print(f"  File not found: {entry}")
            continue

        # Treat as free-text description
        all_parts.append(f"Self-description:\n{entry}")
        sources.append({"label": "description", "content": f"Self-description:\n{entry}"})
        _save()
        print(f"  Added. Enter another, or q to finish.")

    # Offer to save sources for next time
    source_labels = [s["label"] for s in sources if s["label"] != "description"]
    if source_labels and not sources_file.exists():
        save = input("\n  Save source list to ./sources.txt for next time? [Y/n]: ").strip().lower()
        if save in ("", "y", "yes"):
            sources_file.write_text("\n".join(source_labels) + "\n")
            print(f"  Saved {len(source_labels)} source(s) to sources.txt")

    return "\n\n---\n\n".join(all_parts), sources
