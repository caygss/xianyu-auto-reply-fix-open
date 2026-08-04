import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_delivery_config_frontend_node_suites():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend tests"

    result = subprocess.run(
        [
            node,
            "--test",
            "tests/js/delivery-config-session.test.js",
            "tests/js/app-delivery-config-race.test.js",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
