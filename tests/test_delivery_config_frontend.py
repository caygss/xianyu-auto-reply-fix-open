import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_delivery_config_static_load_and_identity_contract():
    index_html = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    coordinator = '<script src="/static/js/delivery-config-session.js?v=1"></script>'
    app = '<script src="/static/js/app.js?v=1.2.10"></script>'

    assert coordinator in index_html
    assert app in index_html
    assert index_html.index(coordinator) < index_html.index(app)
    for identity_id in (
        "deliveryCurrentItemIdentity",
        "deliveryCurrentAccountIdentity",
        "deliveryCurrentTitleIdentity",
        "deliveryCurrentIdIdentity",
    ):
        assert f'id="{identity_id}"' in index_html


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
