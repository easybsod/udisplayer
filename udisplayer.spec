# Build with: python -m PyInstaller --clean --noconfirm udisplayer.spec
from importlib.metadata import distribution
from pathlib import Path

root = Path(SPECPATH)
pillow = distribution('Pillow')
pillow_license = next(
    pillow.locate_file(file)
    for file in pillow.files
    if str(file).endswith('.dist-info/licenses/LICENSE')
)
analysis = Analysis(
    [str(root / 'server.py')],
    pathex=[str(root)],
    binaries=[],  # FFmpeg and ffprobe are deliberately external.
    datas=[(str(root / 'index.html'), '.'),
           (str(root / 'web/not-found.html'), 'web'),
           (str(root / 'channels.example.json'), '.'),
           (str(root / 'THIRD_PARTY_NOTICES.md'), '.'),
           (str(root / 'licenses'), 'licenses'),
           (str(pillow_license), 'licenses/pillow')],
    hiddenimports=[],
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
          name='udisplayer', debug=False, strip=False, upx=False, console=True)
