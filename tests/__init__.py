import sys
from pathlib import Path

# Make `import changeguard` work without installing the package (pytest also gets this via pyproject).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
