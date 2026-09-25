import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gcws.app import main  # noqa: E402

sys.exit(main())
