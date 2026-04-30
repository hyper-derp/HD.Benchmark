"""Minimal YAML reader for the few configs the suite ships.

Supports exactly what we ship in `configs/release.yaml`: flat
top-level keys, one level of indented nesting, scalar values, and
'#'-comments. No flow style, no anchors, no multi-doc. If you need
more, switch to PyYAML — but the design's "minimize external deps"
note means we stay stdlib-only until then.

Scalar parsing:
- bare 'true' / 'false' (case-insensitive) → bool
- bare 'null' / '~' → None
- digits (with optional '.', '-', '+', 'e') → int or float
- everything else → string (no quoting required, but trims
  surrounding whitespace and strips '#'-comments after the value)
"""

import io
import re


def loads(text):
  """Parse `text` as YAML. Returns a dict for our config shape."""
  root = {}
  stack = [(0, root)]    # (indent, node)
  for raw in text.splitlines():
    line = raw.rstrip()
    # Skip pure-comment lines (the regex below only handles
    # trailing comments after a value).
    if line.lstrip().startswith("#"):
      continue
    # Strip trailing comments — only when '#' is preceded by space
    # so 'foo: 5#suffix' stays a single token.
    line = _strip_comment(line)
    if not line.strip():
      continue
    indent = len(line) - len(line.lstrip(" "))
    body = line.strip()
    if ":" not in body:
      raise ValueError(f"yaml_lite: missing ':' in {raw!r}")
    key, _, val = body.partition(":")
    key = key.strip()
    val = val.strip()
    while stack and indent < stack[-1][0]:
      stack.pop()
    if not stack:
      raise ValueError(f"yaml_lite: dedent past root: {raw!r}")
    parent = stack[-1][1]
    if val == "":
      child = {}
      parent[key] = child
      stack.append((indent + 2, child))
    else:
      parent[key] = _scalar(val)
  return root


def load_path(path, *, default=None):
  """Read YAML from `path`. Return `default` if the file is missing."""
  try:
    with open(path) as f:
      text = f.read()
  except OSError:
    if default is None:
      raise
    return default
  return loads(text)


_COMMENT_RE = re.compile(r"(?<![\w])\s+#.*$")


def _strip_comment(line):
  """Remove trailing '#'-comment when prefixed by whitespace."""
  return _COMMENT_RE.sub("", line)


def _scalar(s):
  """Coerce a YAML scalar string into the nearest Python type."""
  low = s.lower()
  if low in ("true", "yes", "on"):
    return True
  if low in ("false", "no", "off"):
    return False
  if low in ("null", "~", ""):
    return None
  # Numbers
  try:
    return int(s)
  except ValueError:
    pass
  try:
    return float(s)
  except ValueError:
    pass
  # Strip a surrounding pair of single or double quotes if present.
  if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
    return s[1:-1]
  return s
