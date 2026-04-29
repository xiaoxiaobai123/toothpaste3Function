# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the toothpaste3Function aarch64 build.
# Bundles the company logo and includes all subpackages.

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('company_name.png', '.'),
    ],
    hiddenimports=[
        # Subpackage modules sometimes missed by static analysis.
        'core.config_manager',
        'core.log_config',
        'core.license_utils',
        'core.task_manager',
        'core.version',
        'camera.base',
        'camera.environment',
        'camera.manager',
        'plc.base',
        'plc.codec',
        'plc.enums',
        'plc.manager',
        'processing.algorithms',
        'processing.base',
        'processing.brush_head',
        'processing.display_utils',
        'processing.height_check',
        'processing.registry',
        'processing.result',
        'processing.toothpaste_frontback',
        # Legacy fronback compatibility (selected via plc_protocol).
        'legacy',
        'legacy.fronback_algorithms',
        'legacy.fronback_orchestrator',
        'legacy.fronback_protocol',
        # Hikvision MVS SDK — installed at /opt/MVS during CI build, picked
        # up via PYTHONPATH=/opt/MVS/Samples/aarch64/Python/MvImport.
        'MvCameraControl_class',
        'CameraParams_const',
        'CameraParams_header',
        'MvErrorDefine_const',
        'PixelType_header',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='main',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
