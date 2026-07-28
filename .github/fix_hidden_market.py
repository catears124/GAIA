from pathlib import Path

path = Path("src/gaia/employer_census.py")
text = path.read_text(encoding="utf-8")
old = '        values = re.split(r"[,;|]", str(value or ""))\n'
new = '        values = [item.strip() for item in re.split(r"[,;|]", str(value or ""))]\n'
if old not in text:
    raise SystemExit("expected keyword split not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
