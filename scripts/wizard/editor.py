import os
import subprocess
import tempfile

from wizard.friends import _validate_timezone


def check_editor() -> str | None:
    """Check if an editor is available. Returns the editor command or None."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if editor:
        return editor
    for candidate in ["nano", "vim", "vi"]:
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            return candidate
    return None


def edit_with_editor(text: str, label: str = "") -> str:
    suffix = f"-{label}.md" if label else ".md"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        f.write(text)
        f.flush()
        tmppath = f.name

    editor = check_editor()
    if editor:
        subprocess.call([editor, tmppath])
    else:
        print(f"\n  No $EDITOR set. Edit this file manually:")
        print(f"    {tmppath}")
        input("  Press ENTER when done editing...")

    with open(tmppath) as f:
        result = f.read()
    os.unlink(tmppath)
    return result


def candidate_to_text(c: dict) -> str:
    lines = [
        f"Name: {c['name']}",
        f"Age: {c['age']}",
        f"Location: {c['location']}",
        f"Occupation: {c['occupation']}",
        "",
        f"Vibe: {c.get('vibe', '')}",
        "",
        f"Why: {c.get('why', '')}",
        f"Timezone: {c.get('timezone', '')}",
    ]
    return "\n".join(lines)


def text_to_candidate(text: str, original: dict) -> dict:
    c = dict(original)
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "name":
            c["name"] = value
        elif key == "age":
            try:
                c["age"] = int(value)
            except ValueError:
                pass
        elif key == "location":
            c["location"] = value
        elif key == "occupation":
            c["occupation"] = value
        elif key == "vibe":
            c["vibe"] = value
        elif key == "why":
            c["why"] = value
        elif key == "timezone":
            c["timezone"] = value
    # Validate timezone after all fields are parsed (location may help)
    c["timezone"] = _validate_timezone(c.get("timezone", ""), c.get("location", ""))
    return c
