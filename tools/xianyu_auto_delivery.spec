# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "global_config.yml"), "."),
    (str(ROOT / "announcement.json"), "."),
    (str(ROOT / "utils" / "gen_tfstk.js"), "utils"),
    (str(ROOT / "utils" / "et_f.js"), "utils"),
]
datas += collect_data_files("playwright")
datas += collect_data_files("DrissionPage")

hiddenimports = [
    "reply_server",
    "runtime_paths",
    "api_captcha_remote",
    "XianyuAutoAsync",
    "playwright.sync_api",
    "playwright.async_api",
    "playwright._impl._driver",
    "playwright._impl._path_utils",
    "utils.qr_login",
    "utils.qr_login_lite",
    "utils.xianyu_utils",
    "utils.image_utils",
    "utils.time_utils",
    "utils.notification_dispatcher",
    "utils.item_publisher",
    "utils.item_search",
    "utils.order_history_sync",
    "utils.order_detail_fetcher",
    "utils.rate_service",
    "utils.red_flower_service",
    "utils.slider_orchestrator",
    "utils.refresh_util",
    "utils.captcha_remote_control",
    "utils.xianyu_slider_stealth",
    "aiohttp_socks",
    "python_socks.async_.asyncio.v2",
]


a = Analysis(
    [str(ROOT / "Start.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="XianyuAutoDelivery",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="XianyuAutoDelivery",
)
