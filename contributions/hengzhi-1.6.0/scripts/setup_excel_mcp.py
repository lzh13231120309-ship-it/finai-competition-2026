"""Compatibility entry point: prepare and verify all bundled extensions."""
from bootstrap import ensure_main
from pathlib import Path
import subprocess
python=ensure_main()
subprocess.run([str(python),str(Path(__file__).with_name('bootstrap.py'))],check=True)
