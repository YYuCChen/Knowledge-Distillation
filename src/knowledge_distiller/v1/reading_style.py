"""Explicit, reversible installation of a note-scoped Obsidian snippet."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile

NAME='kd-reading'
# Exact unmodified snippet shipped before the evidence-icon refinement.
PREVIOUS_SHA256 = '43961e0c5ededbf1287c49ac6090499d3c5bf8f2f2376e5e75eb90fc6e50f09a'


def state(vault):
    if not vault:return 'not_installed'
    settings=Path(vault)/'.obsidian'
    try:
        if not (settings/'snippets'/(NAME+'.css')).is_file():return 'not_installed'
        if hashlib.sha256((settings/'snippets'/(NAME+'.css')).read_bytes()).hexdigest() == PREVIOUS_SHA256:
            return 'update_available'
        appearance=settings/'appearance.json'
        preferences=json.loads(appearance.read_bytes()) if appearance.exists() else {}
        if not isinstance(preferences,dict):return 'unavailable'
        enabled=preferences.get('enabledCssSnippets',[])
        if not isinstance(enabled,list):return 'unavailable'
        return 'enabled' if NAME in enabled else 'disabled'
    except (OSError,ValueError):
        return 'unavailable'


def content():
    static=Path(__file__).parent/'static'
    font=base64.b64encode((static/'fonts/NotoSerifSC-wght.woff2').read_bytes()).decode('ascii')
    return ("@font-face{font-family:'KD Reading Serif';src:url(data:font/woff2;base64,"+font+
            ") format('woff2');font-weight:100 900;font-style:normal;font-display:swap;}\n"+
            (static/'kd-reading.css').read_text()).encode('utf-8')


def _replace(path,data):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
        temporary=Path(output.name)
        try:output.write(data)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def install(vault):
    if not vault:raise ValueError('reading_vault_missing')
    vault=Path(vault)
    if not vault.is_dir():raise ValueError('reading_vault_missing')
    settings=vault/'.obsidian';snippets=settings/'snippets'
    if any(path.is_symlink() for path in (settings,snippets)):
        raise ValueError('reading_style_conflict')
    snippets.mkdir(parents=True,exist_ok=True)
    path=snippets/(NAME+'.css')
    generated=content()
    previous = path.read_bytes() if path.exists() and not path.is_symlink() else None
    upgrade = previous is not None and hashlib.sha256(previous).hexdigest() == PREVIOUS_SHA256
    if path.is_symlink() or (previous is not None and previous != generated and not upgrade):
        raise ValueError('reading_style_conflict')
    appearance=settings/'appearance.json'
    if appearance.is_symlink():raise ValueError('reading_style_conflict')
    before=appearance.read_bytes() if appearance.exists() else None
    preferences=json.loads(before) if before is not None else {}
    if not isinstance(preferences,dict):raise ValueError('reading_style_conflict')
    enabled=preferences.get('enabledCssSnippets',[])
    if not isinstance(enabled,list) or not all(isinstance(v,str) for v in enabled):
        raise ValueError('reading_style_conflict')
    if not path.exists():
        # A user-created file winning this race must never be overwritten.
        with path.open('xb') as output:output.write(generated)
    if upgrade:
        if path.read_bytes() != previous:
            raise ValueError('reading_style_conflict')
        _replace(path, generated)
    if NAME not in enabled:
        preferences['enabledCssSnippets']=[*enabled,NAME]
        if (appearance.read_bytes() if appearance.exists() else None)!=before:
            raise ValueError('reading_style_conflict')
        _replace(appearance,json.dumps(preferences,ensure_ascii=False,indent=2).encode())
    return path


def disable(vault):
    if not vault or not Path(vault).is_dir():raise ValueError('reading_vault_missing')
    settings=Path(vault)/'.obsidian';appearance=settings/'appearance.json'
    if settings.is_symlink() or appearance.is_symlink():raise ValueError('reading_style_conflict')
    if not appearance.exists():return
    before=appearance.read_bytes();preferences=json.loads(before)
    if not isinstance(preferences,dict):raise ValueError('reading_style_conflict')
    enabled=preferences.get('enabledCssSnippets',[])
    if not isinstance(enabled,list) or not all(isinstance(v,str) for v in enabled):raise ValueError('reading_style_conflict')
    if NAME not in enabled:return
    preferences['enabledCssSnippets']=[v for v in enabled if v!=NAME]
    if appearance.read_bytes()!=before:raise ValueError('reading_style_conflict')
    _replace(appearance,json.dumps(preferences,ensure_ascii=False,indent=2).encode())
