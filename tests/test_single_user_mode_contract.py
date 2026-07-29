"""Regression contracts for the one-user Windows distribution mode.

The database checks exercise the real ``DBManager`` against temporary files in
short-lived subprocesses, so its import-time module singleton cannot leak into
the pytest process.
The reply-server checks intentionally inspect route source: importing
``reply_server`` constructs the full application and its runtime services,
which is unnecessary and would make these contracts depend on external
browser/SMTP configuration.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_MANAGER = ROOT / "db_manager.py"
REPLY_SERVER = ROOT / "reply_server.py"
LOGIN_HTML = ROOT / "static" / "login.html"
INDEX_HTML = ROOT / "static" / "index.html"
APP_JS = ROOT / "static" / "js" / "app.js"


_DB_CONTRACT_SCRIPT = r'''
import json
import sys

import db_manager as db_module


def main():
    db_path = sys.argv[1]
    operation = sys.argv[2]
    manager = db_module.DBManager(db_path)
    manager_closed = False
    result = {}
    try:
        if operation == "new":
            result["registration_enabled"] = manager.get_system_setting(
                "registration_enabled"
            )
        elif operation == "migration":
            manager.set_system_setting("registration_enabled", "true")
            manager.save_item_info(
                "cookie-contract",
                "item-contract",
                {"title": "contract item", "description": "existing item"},
            )
            manager.conn.execute(
                """
                INSERT INTO orders (order_id, item_id, buyer_id, order_status)
                VALUES (?, ?, ?, ?)
                """,
                ("order-contract", "item-contract", "buyer-contract", "paid"),
            )
            manager.conn.commit()
            manager_closed = True
            manager.close()

            reopened = db_module.DBManager(db_path)
            reopened_closed = False
            try:
                admin = reopened.get_user_by_username("admin")
                item = reopened.get_item_info("cookie-contract", "item-contract")
                order = reopened.get_order_by_id("order-contract")
                result.update(
                    registration_enabled=reopened.get_system_setting(
                        "registration_enabled"
                    ),
                    admin_exists=bool(admin and admin["username"] == "admin"),
                    item_exists=bool(item),
                    order_exists=bool(
                        order
                        and order["item_id"] == "item-contract"
                        and order["buyer_id"] == "buyer-contract"
                    ),
                )
            finally:
                if not reopened_closed:
                    reopened_closed = True
                    reopened.close()
        else:
            raise ValueError(f"unknown operation: {operation}")
    finally:
        if not manager_closed:
            manager_closed = True
            manager.close()
        db_module.db_manager.close()

    print(json.dumps(result))


main()
'''


def _run_db_contract(tmp_path: Path, db_path: Path, operation: str) -> dict:
    """Run DBManager outside pytest so its import-time singleton cannot leak."""

    environment = os.environ.copy()
    environment["DB_PATH"] = str(tmp_path / "module-singleton.sqlite3")
    environment["SQL_LOG_ENABLED"] = "false"
    completed = subprocess.run(
        [sys.executable, "-c", _DB_CONTRACT_SCRIPT, str(db_path), operation],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "DBManager subprocess failed: "
            f"returncode={completed.returncode}; "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise AssertionError(
            "DBManager subprocess emitted no JSON: "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    try:
        return json.loads(output_lines[-1])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            "DBManager subprocess did not emit JSON on its last non-empty line: "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        ) from exc


def _python_function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"could not extract {name} from {path}"
            return segment
    raise AssertionError(f"function {name!r} not found in {path}")


def _function_node(source: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in source")


def _registration_guard_line(function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Find the closed-mode guard by its registration_enabled comparison AST."""

    for node in ast.walk(function_node):
        if not isinstance(node, ast.If):
            continue
        for comparison in ast.walk(node.test):
            if not isinstance(comparison, ast.Compare):
                continue
            names = [item.id for item in ast.walk(comparison) if isinstance(item, ast.Name)]
            compares_true = any(
                isinstance(value, ast.Constant) and value.value == "true"
                for value in ast.walk(comparison)
            )
            if (
                "registration_enabled" in names
                and any(isinstance(op, ast.NotEq) for op in comparison.ops)
                and compares_true
            ):
                return node.lineno
    raise AssertionError("registration guard comparison was not found")


def _isolated_function(path: Path, name: str, namespace: dict):
    """Execute one route body without FastAPI's Depends/decorator machinery."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            isolated_node = copy.deepcopy(node)
            isolated_node.decorator_list = []
            isolated_node.returns = None
            for argument in (
                isolated_node.args.posonlyargs
                + isolated_node.args.args
                + isolated_node.args.kwonlyargs
            ):
                argument.annotation = None
            isolated_node.args.defaults = []
            isolated_node.args.kw_defaults = [None] * len(isolated_node.args.kwonlyargs)

            module = ast.Module(body=[isolated_node], type_ignores=[])
            ast.fix_missing_locations(module)
            isolated_namespace = dict(namespace)
            exec(compile(module, str(path), "exec"), isolated_namespace)
            return isolated_namespace[name]
    raise AssertionError(f"function {name!r} not found in {path}")


def _javascript_function_source(source: str, name: str) -> str:
    marker = f"async function {name}("
    start = source.index(marker)
    opening_brace = source.index("{", start)
    depth = 0
    state = "code"
    quote = None
    index = opening_brace
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if state == "line_comment":
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            if char == "*" and next_char == "/":
                state = "code"
                index += 1
        elif state in {"string", "template"}:
            if char == "\\":
                index += 1
            elif char == quote:
                state = "code"
                quote = None
        elif char == "/" and next_char == "/":
            state = "line_comment"
            index += 1
        elif char == "/" and next_char == "*":
            state = "block_comment"
            index += 1
        elif char in {"'", '"', "`"}:
            state = "template" if char == "`" else "string"
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    raise AssertionError(f"function {name!r} is not balanced")


def test_new_database_defaults_registration_disabled(tmp_path):
    result = _run_db_contract(tmp_path, tmp_path / "new-install.sqlite3", "new")
    assert result["registration_enabled"] == "false"


def test_existing_database_migration_closes_registration_without_resetting_data(tmp_path):
    result = _run_db_contract(
        tmp_path, tmp_path / "existing-install.sqlite3", "migration"
    )
    assert result["registration_enabled"] == "false"
    assert result["admin_exists"]
    assert result["item_exists"]
    assert result["order_exists"]


def test_registration_migration_failure_is_not_swallowed():
    class FailingCursor:
        def execute(self, *args, **kwargs):
            raise RuntimeError("registration enforcement failed")

    migrate_database = _isolated_function(DB_MANAGER, "_migrate_database", {})
    try:
        migrate_database(object(), FailingCursor())
    except RuntimeError as error:
        assert str(error) == "registration enforcement failed"
    else:
        raise AssertionError("registration enforcement failure must abort migration")


def test_registration_status_contract_fails_closed_for_missing_or_failed_setting_reads():
    source = _python_function_source(REPLY_SERVER, "get_registration_status")
    function_node = _function_node(source, "get_registration_status")

    missing_setting_ifs = [
        node
        for node in ast.walk(function_node)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "enabled_str"
        and any(isinstance(op, ast.Is) for op in node.test.ops)
        and any(
            isinstance(value, ast.Constant) and value.value is None
            for value in node.test.comparators
        )
    ]
    assert missing_setting_ifs, "registration status must handle a missing setting"
    missing_assignment = any(
        isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "enabled_bool"
            for target in statement.targets
        )
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is False
        for statement in ast.walk(missing_setting_ifs[0])
    )
    assert missing_assignment, "missing registration setting must resolve to disabled"

    error_returns_disabled = any(
        isinstance(handler, ast.ExceptHandler)
        and any(
            isinstance(return_node, ast.Return)
            and isinstance(return_node.value, ast.Dict)
            and any(
                isinstance(key, ast.Constant)
                and key.value == "enabled"
                and isinstance(value, ast.Constant)
                and value.value is False
                for key, value in zip(return_node.value.keys, return_node.value.values)
            )
            for return_node in ast.walk(handler)
        )
        for handler in ast.walk(function_node)
    )
    assert error_returns_disabled, "registration status errors must resolve to disabled"


def test_registration_settings_contract_cannot_enable_registration(monkeypatch):
    writes = []

    class FakeDatabase:
        def set_system_setting(self, key, value, description):
            writes.append((key, value, description))
            return True

    fake_db_module = types.ModuleType("db_manager")
    fake_db_module.db_manager = FakeDatabase()
    monkeypatch.setitem(sys.modules, "db_manager", fake_db_module)

    class IsolatedHTTPException(Exception):
        pass

    update_registration_settings = _isolated_function(
        REPLY_SERVER,
        "update_registration_settings",
        {
            "HTTPException": IsolatedHTTPException,
            "logger": types.SimpleNamespace(
                error=lambda *args, **kwargs: None,
                warning=lambda *args, **kwargs: None,
            ),
            "log_with_user": lambda *args, **kwargs: None,
        },
    )
    result = update_registration_settings(
        types.SimpleNamespace(enabled=True), {"username": "admin"}
    )

    assert len(writes) == 1
    key, persisted_value, _description = writes[0]
    assert key == "registration_enabled"
    assert persisted_value == "false"
    assert result["enabled"] is False


def test_registration_routes_keep_closed_response_before_verification():
    module_source = REPLY_SERVER.read_text(encoding="utf-8")
    page_source = _python_function_source(REPLY_SERVER, "register_page")
    register_source = _python_function_source(REPLY_SERVER, "register")

    assert "@app.get('/register.html'" in module_source
    assert "@app.post('/register'" in module_source
    sensitive_calls = (
        "send_verification_email",
        "send_verification_code",
        "verify_email_code",
        "generate_verification_code",
        "generate_captcha",
        "generate_captcha_code",
        "verify_captcha",
    )

    for route_name, function_name, route_source in (
        ("GET /register.html", "register_page", page_source),
        ("POST /register", "register", register_source),
    ):
        # This is intentionally a small source contract: importing reply_server
        # starts the full application, while AST line numbers still verify the
        # user-visible closed-mode ordering without that runtime side effect.
        route_node = _function_node(route_source, function_name)
        guard_line = _registration_guard_line(route_node)
        for call in ast.walk(route_node):
            if not isinstance(call, ast.Call):
                continue
            called_name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else None
            )
            if called_name in sensitive_calls:
                assert guard_line < call.lineno, (
                    f"{route_name} checks registration after {called_name}"
                )

    assert "verify_email_code" in register_source
    assert "send_verification_email" not in register_source
    assert "send_verification_code" not in register_source
    assert "status_code=403" in page_source
    assert re.search(
        r"return\s+RegisterResponse\([\s\S]*?success\s*=\s*False",
        register_source,
    )


def test_login_registration_entry_is_hidden_by_default():
    source = LOGIN_HTML.read_text(encoding="utf-8")
    opening_tags = re.findall(r"<div\b[^>]*>", source, flags=re.IGNORECASE)
    register_tag = next(
        (tag for tag in opening_tags if re.search(r"\bid=[\"']registerSection[\"']", tag)),
        None,
    )
    assert register_tag, "login page must retain the defensive registration section"
    attrs = register_tag
    assert re.search(r"\bhidden\b", attrs) or re.search(
        r"display\s*:\s*none", attrs, flags=re.IGNORECASE
    )


def test_login_registration_status_failures_keep_entry_hidden():
    source = LOGIN_HTML.read_text(encoding="utf-8")
    status_check = _javascript_function_source(source, "checkRegistrationStatus")

    non_ok_branch = re.search(
        r"if\s*\(\s*!\s*response\.ok\s*\)\s*\{(?P<body>[\s\S]*?)\}",
        status_check,
    )
    assert non_ok_branch, "non-OK registration status must have an explicit branch"
    assert re.search(
        r"registerSection[\s\S]*?style\.display\s*=\s*['\"]none['\"]",
        non_ok_branch.group("body"),
    ), "non-OK registration status must hide registerSection"
    assert re.search(
        r"catch[\s\S]*?registerSection[\s\S]*?style\.display\s*=\s*['\"]none['\"]",
        status_check,
    )


def test_admin_registration_control_is_permanently_read_only():
    source = INDEX_HTML.read_text(encoding="utf-8")
    opening_tags = re.findall(r"<input\b[^>]*>", source, flags=re.IGNORECASE)
    control = next(
        (
            tag
            for tag in opening_tags
            if re.search(r"\bid=[\"']registrationEnabled[\"']", tag)
        ),
        None,
    )
    assert control, "admin page must show the registration state"
    attrs = control
    assert re.search(r"\bdisabled\b", attrs) or re.search(
        r'''aria-readonly\s*=\s*["']true["']''', attrs, flags=re.IGNORECASE
    )

def test_admin_javascript_never_reopens_registration():
    source = APP_JS.read_text(encoding="utf-8")
    load_source = _javascript_function_source(source, "loadRegistrationSettings")
    update_source = _javascript_function_source(source, "updateLoginInfoSettings")

    assert re.search(r"checkbox\.checked\s*=\s*false", load_source)
    assert "/registration-settings" not in update_source
