"""Generate Windows changed-file payload from two complete release directories."""
import argparse
import json
from pathlib import Path
from knowledge_distiller.v1.windows_delta import build_payload

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build_payload(args.target,args.output,args.base),indent=2))
