"""Run explicitly during an approved upgrade, never from normal startup."""
import argparse
import json
from pathlib import Path
from knowledge_distiller.v1.credential_migration import migrate

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='集中迁移旧凭据；默认仅预览，不读取钥匙串。')
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--credentials',type=Path,required=True)
    parser.add_argument('--execute',action='store_true',help='执行一次性导入；系统可能要求旧凭据访问授权')
    args=parser.parse_args()
    result=migrate(args.database,args.credentials,execute=args.execute)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(1 if result['failed'] else 0)
