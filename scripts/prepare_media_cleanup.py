"""Read-only historical cleanup proposal with exact backups and note diffs."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def prepare(database,vault,output):
    from knowledge_distiller.v1.media_lifecycle import preview,preview_vault
    database,vault,output=database.resolve(),vault.resolve(),output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    backup=output/'backup';backup.mkdir()
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(backup/'knowledge.sqlite3') as destination:source.backup(destination)
        publications=dict(source.execute('SELECT k.published_path,m.canonical_url FROM knowledge_results k JOIN source_facts sf USING(source_fact_id) JOIN materials m USING(material_id) WHERE k.published_vault=?',(str(vault),)))
    proposal={'database':str(database),'vault':str(vault),'backup_database':str(backup/'knowledge.sqlite3'),
              'backup_database_sha256':digest(backup/'knowledge.sqlite3'),'media':preview(database),'files':[],'notes':[],
              'status':'review_only_not_applied'}
    patches={}
    for entry in preview_vault(database,vault):
        if entry['disposition']!='known_copy':continue
        path=Path(entry['path']);name=path.name
        changes={}
        for reference in entry['references']:
            if reference not in publications:break
            note=vault/reference
            before=note.read_bytes().decode("utf-8")
            lines=before.splitlines(keepends=True)
            matches=[i for i,line in enumerate(lines) if name in line]
            if not matches or any(not (lines[i].strip().startswith('![[') and lines[i].strip().endswith(']]')) for i in matches):break
            url=publications[reference]
            if not url.startswith(('https://','http://')):break
            for i in matches:lines[i]='[原平台查看素材](<'+url+'>)\n'
            changes[reference]=(''.join(lines),before)
        else:
            if not entry['references']:continue
            relative=path.relative_to(vault)
            target=backup/'vault'/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
            if digest(target)!=entry['sha256']:raise ValueError('media changed while preparing backup')
            proposal['files'].append({**entry,'backup':str(target)})
            for reference,(after,before) in changes.items():
                if reference in patches:raise ValueError('multiple attachments in one note need combined review')
                patches[reference]=(before,after)
    for reference,(before,after) in patches.items():
        original=backup/'vault'/reference;original.parent.mkdir(parents=True,exist_ok=True);original.write_text(before)
        proposed=output/'proposed-vault'/reference;proposed.parent.mkdir(parents=True,exist_ok=True);proposed.write_text(after)
        proposal['notes'].append({'path':str(vault/reference),'backup':str(original),'proposed':str(proposed),
                                 'before_sha256':digest(original),'after_sha256':digest(proposed)})
        with (output/'note-changes.diff').open('a') as diff:
            diff.writelines(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=reference,tofile=reference))
    (output/'plan.json').write_text(json.dumps(proposal,ensure_ascii=False,indent=2))
    return proposal


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database',type=Path,required=True);p.add_argument('--vault',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();r=prepare(a.database,a.vault,a.output)
    print(json.dumps({'status':r['status'],'backed_up_media_files':len(r['files']),'proposed_notes':len(r['notes'])}))
